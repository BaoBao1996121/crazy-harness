from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import httpx
import pytest

from crazy_harness.control_plane.model_governance import (
    ModelBudgetConfig,
    ModelCallFailed,
    PersistentModelCallAuthority,
)
from crazy_harness.control_plane.store import (
    ModelBudgetExceeded,
    ModelCallRejected,
    SQLiteEventStore,
)
from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import (
    DeepSeekOpenAIProvider,
    ModelMessage,
    ModelResponse,
)
from crazy_harness.core.tools import ToolRegistry


class CountingProvider:
    model = "deepseek-v4-flash"
    max_tokens = 4096

    def __init__(self) -> None:
        self.call_count = 0

    def complete(self, messages, *, tools=None, response_schema=None):
        self.call_count += 1
        return ModelResponse(
            content='{"type":"continue","reason":"observed"}',
            usage={
                "prompt_tokens": 12,
                "prompt_cache_hit_tokens": 2,
                "prompt_cache_miss_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
        )


def _store_with_budget(path, **overrides):
    values = {
        "max_total_tokens": 100_000,
        "max_cost_usd": "1.00",
        "max_concurrent_calls": 1,
        **overrides,
    }
    config = ModelBudgetConfig(**values)
    store = SQLiteEventStore(path)
    store.append(
        Event(
            run_id="run-1",
            task_id="root",
            type="run.created",
            source="test",
            payload={
                "model_mode": "deepseek",
                "model_budget": config.model_dump(mode="json"),
            },
        )
    )
    return store


def _request(store, task_id):
    return store.append(
        Event(
            run_id="run-1",
            task_id=task_id,
            type="model.requested",
            source=task_id,
            payload={"turn_id": "turn_1", "prompt_hash": task_id},
        )
    )


def _complete(authority, request, provider):
    return authority.complete(
        request_event=request,
        provider=provider,
        messages=[ModelMessage(role="user", content="inspect")],
        tools=None,
        response_schema=None,
    )


def test_concurrent_model_calls_share_one_persistent_run_budget(tmp_path):
    first = _store_with_budget(tmp_path / "control.db")
    second = SQLiteEventStore(first.path)
    requests = (_request(first, "agent-a"), _request(first, "agent-b"))
    barrier = Barrier(2)

    def invoke(index):
        authority = PersistentModelCallAuthority(
            (first, second)[index], before_reserve=barrier.wait
        )
        provider = CountingProvider()
        try:
            _complete(authority, requests[index], provider)
        except ModelBudgetExceeded:
            return "rejected", provider.call_count
        return "started", provider.call_count

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(invoke, range(2)))

    assert results == [("rejected", 0), ("started", 1)]
    assert first.model_budget_status("run-1")["active_calls"] == 1


def test_budget_exhaustion_rejects_before_provider_call(tmp_path):
    store = _store_with_budget(
        tmp_path / "control.db",
        max_total_tokens=1,
        max_concurrent_calls=2,
    )
    provider = CountingProvider()

    with pytest.raises(ModelBudgetExceeded):
        _complete(
            PersistentModelCallAuthority(store),
            _request(store, "agent-a"),
            provider,
        )

    assert provider.call_count == 0


def test_usage_reconciliation_is_idempotent_by_completion_event(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    authority = PersistentModelCallAuthority(store)
    request = _request(store, "agent-a")
    response = _complete(authority, request, CountingProvider())
    completed = store.append(
        Event(
            run_id="run-1",
            task_id="agent-a",
            type="model.completed",
            source="agent-a",
            payload={"turn_id": "turn_1", "usage": response.usage},
            causation_id=request.id,
        )
    )

    authority.reconcile(request_event=request, completion_event=completed)
    authority.reconcile(request_event=request, completion_event=completed)
    status = store.model_budget_status("run-1")

    assert status["completed_calls"] == 1
    assert status["spent_tokens"] == 16
    assert status["estimated_spent_microusd"] == 3


def test_usage_audit_event_is_repaired_after_ledger_commit_crash(tmp_path, monkeypatch):
    store = _store_with_budget(tmp_path / "control.db")
    authority = PersistentModelCallAuthority(store)
    request = _request(store, "agent-a")
    response = _complete(authority, request, CountingProvider())
    completed = store.append(
        Event(
            run_id="run-1",
            task_id="agent-a",
            type="model.completed",
            source="agent-a",
            payload={"turn_id": "turn_1", "usage": response.usage},
            causation_id=request.id,
        )
    )
    original_append = authority._append

    def lose_first_audit(identity, key, event_type, payload):
        if event_type == "model.usage.recorded":
            raise RuntimeError("crash after accounting commit")
        return original_append(identity, key, event_type, payload)

    monkeypatch.setattr(authority, "_append", lose_first_audit)
    with pytest.raises(RuntimeError, match="accounting commit"):
        authority.reconcile(request_event=request, completion_event=completed)
    assert store.model_call(request.id)["state"] == "completed"

    monkeypatch.setattr(authority, "_append", original_append)
    assert authority.reconcile(
        request_event=request, completion_event=completed
    ) is False
    assert len(
        [
            event
            for event in store.read_all(run_id="run-1")
            if event.type == "model.usage.recorded"
        ]
    ) == 1


def test_invalid_provider_usage_falls_back_to_the_pessimistic_reservation(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    authority = PersistentModelCallAuthority(store)
    request = _request(store, "agent-a")
    _complete(authority, request, CountingProvider())
    reserved = store.model_call(request.id)
    completed = store.append(
        Event(
            run_id="run-1",
            task_id="agent-a",
            type="model.completed",
            source="agent-a",
            payload={
                "turn_id": "turn_1",
                "usage": {
                    "prompt_tokens": -10,
                    "completion_tokens": "not-a-number",
                    "total_tokens": -1,
                },
            },
            causation_id=request.id,
        )
    )

    authority.reconcile(request_event=request, completion_event=completed)
    call = store.model_call(request.id)

    assert call["prompt_tokens"] == reserved["reserved_input_tokens"]
    assert call["prompt_cache_hit_tokens"] == 0
    assert call["prompt_cache_miss_tokens"] == reserved["reserved_input_tokens"]
    assert call["completion_tokens"] == reserved["reserved_output_tokens"]
    assert call["total_tokens"] == (
        reserved["reserved_input_tokens"] + reserved["reserved_output_tokens"]
    )
    assert call["actual_cost_microusd"] == reserved["reserved_cost_microusd"]
    usage_event = next(
        event
        for event in store.read_all(run_id="run-1")
        if event.type == "model.usage.recorded"
    )
    assert usage_event.payload["usage_quality"] == "pessimistic_fallback"


def test_retryable_provider_errors_use_bounded_backoff(tmp_path):
    store = _store_with_budget(
        tmp_path / "control.db", max_retries_per_call=2
    )
    statuses = iter((429, 503, 200))
    requests = []

    def respond(request):
        requests.append(request)
        status = next(statuses)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "retry"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    delays = []
    provider = DeepSeekOpenAIProvider(
        api_key="test-key", transport=httpx.MockTransport(respond)
    )
    authority = PersistentModelCallAuthority(store, sleep=delays.append)
    response = _complete(authority, _request(store, "agent-a"), provider)

    assert response.content == "done"
    assert len(requests) == 3
    assert delays == [0.25, 0.5]
    assert store.list_model_calls(run_id="run-1")[0]["attempt_count"] == 3
    retry_events = [
        event
        for event in store.read_all(run_id="run-1")
        if event.type == "model.call.retry.scheduled"
    ]
    assert len(retry_events) == 2


def test_authentication_error_is_not_retried_and_releases_budget(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = DeepSeekOpenAIProvider(
        api_key="bad-key", transport=httpx.MockTransport(reject)
    )
    with pytest.raises(ModelCallFailed) as captured:
        _complete(
            PersistentModelCallAuthority(store, sleep=lambda _: None),
            _request(store, "agent-a"),
            provider,
        )

    assert captured.value.state == "failed"
    assert isinstance(captured.value.__cause__, httpx.HTTPStatusError)
    assert len(calls) == 1
    assert store.list_model_calls(run_id="run-1")[0]["state"] == "failed"
    assert store.model_budget_status("run-1")["committed_tokens"] == 0


def test_read_timeout_is_unknown_and_keeps_pessimistic_budget(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")

    def lose_response(request):
        raise httpx.ReadTimeout("response lost", request=request)

    provider = DeepSeekOpenAIProvider(
        api_key="test-key", transport=httpx.MockTransport(lose_response)
    )
    with pytest.raises(ModelCallFailed) as captured:
        _complete(
            PersistentModelCallAuthority(store, sleep=lambda _: None),
            _request(store, "agent-a"),
            provider,
        )

    assert captured.value.state == "unknown"
    assert isinstance(captured.value.__cause__, httpx.ReadTimeout)
    status = store.model_budget_status("run-1")
    assert store.list_model_calls(run_id="run-1")[0]["state"] == "unknown"
    assert status["unknown_calls"] == 1
    assert status["committed_tokens"] > 0


def test_cancelled_run_rejects_before_provider_call(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    store.append(
        Event(
            run_id="run-1",
            task_id="root",
            type="run.cancel.requested",
            source="operator",
        )
    )
    provider = CountingProvider()

    with pytest.raises(ModelCallRejected):
        _complete(
            PersistentModelCallAuthority(store),
            _request(store, "agent-a"),
            provider,
        )

    assert provider.call_count == 0
    assert store.list_model_calls(run_id="run-1") == []


def test_stale_inflight_call_recovers_as_unknown_without_freeing_budget(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    authority = PersistentModelCallAuthority(store)
    _complete(authority, _request(store, "agent-a"), CountingProvider())

    recovered = store.recover_stale_model_calls(
        run_id="run-1",
        stale_before=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    status = store.model_budget_status("run-1")

    assert recovered == {"failed": 0, "unknown": 1}
    assert status["active_calls"] == 0
    assert status["unknown_calls"] == 1
    assert status["committed_tokens"] > 0


def test_unresolved_inflight_model_attempt_is_not_sampled_again(tmp_path):
    store = _store_with_budget(
        tmp_path / "control.db",
        max_concurrent_calls=2,
    )
    authority = PersistentModelCallAuthority(store)
    provider = CountingProvider()
    request = _request(store, "agent-a")
    _complete(authority, request, provider)
    loop = AgentLoop(
        agent_id="agent-a",
        model=provider,
        event_log=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=ToolRegistry(),
        model_call_authority=authority,
    )

    with pytest.raises(ModelCallFailed) as captured:
        loop.run_once()

    assert captured.value.state == "unknown"
    assert provider.call_count == 1
    assert store.model_call(request.id)["state"] == "unknown"


def test_unattempted_reservation_is_released_before_a_new_turn_retries(tmp_path):
    store = _store_with_budget(tmp_path / "control.db")
    authority = PersistentModelCallAuthority(store)
    provider = CountingProvider()
    request = _request(store, "agent-a")
    store.reserve_model_call(
        call_id=request.id,
        run_id=request.run_id,
        task_id=request.task_id,
        agent_id=request.source,
        provider="CountingProvider",
        model=provider.model,
        reserved_input_tokens=10,
        reserved_output_tokens=10,
        reserved_cost_microusd=10,
        max_total_tokens=100_000,
        max_cost_microusd=1_000_000,
        max_concurrent_calls=1,
    )
    loop = AgentLoop(
        agent_id="agent-a",
        model=provider,
        event_log=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=ToolRegistry(),
        model_call_authority=authority,
    )

    # A second crash after releasing the reservation but before creating the
    # replacement Turn must remain safe to retry.
    authority.recover_unresolved(request_event=request)
    loop.run_once()

    assert provider.call_count == 1
    assert store.model_call(request.id)["state"] == "failed"
    assert [call["state"] for call in store.list_model_calls(run_id="run-1")] == [
        "failed",
        "completed",
    ]
