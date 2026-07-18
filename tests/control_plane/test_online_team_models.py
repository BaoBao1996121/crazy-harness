from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import httpx

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.events import Event
from crazy_harness.core.models import DeepSeekOpenAIProvider, FakeModelProvider
from crazy_harness.taskpacks import ResidentDemoTeamTaskPack


def test_deepseek_team_routes_every_child_loop_through_bound_model_factory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    pack = ResidentDemoTeamTaskPack()
    bindings = []
    model_messages = []

    def factory(binding):
        bindings.append(binding)
        responses = (
            pack.scripted_peer_responses()
            if binding.agent_run_kind == "peer"
            else pack.scripted_assignment_responses(binding.stage_id)
        )
        provider = FakeModelProvider(responses)
        complete = provider.complete

        def record(messages, **kwargs):
            model_messages.append(messages)
            response = complete(messages, **kwargs)
            return response.model_copy(
                update={
                    "usage": {
                        "prompt_tokens": 12,
                        "prompt_cache_hit_tokens": 2,
                        "prompt_cache_miss_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                    }
                }
            )

        provider.complete = record
        return provider

    runtime = ResidentRuntime(tmp_path, team_model_factory=factory)
    created = runtime.submit_task(
        TaskRequest(
            title="Online Team routing",
            brief="Route every cognitive worker through the configured model.",
            model_mode="deepseek",
        )
    )
    runtime.run_until_idle(max_steps=180)

    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert {binding.agent_run_kind for binding in bindings} == {"assignment", "peer"}
    assert {binding.stage_id for binding in bindings if binding.stage_id} == {
        "evidence",
        "risk",
        "artifact",
        "review",
    }
    assert all(binding.model_mode == "deepseek" for binding in bindings)
    assert all(binding.run_id == created.run_id for binding in bindings)
    assert model_messages
    assert all('"mode": "deepseek"' in messages[0].content for messages in model_messages)
    assert all(
        '"provider_mode": "deepseek"' in messages[0].content
        for messages in model_messages
    )
    snapshot = runtime.snapshot(created.run_id)
    assert snapshot["model_budget"]["completed_calls"] == len(model_messages)
    assert snapshot["model_budget"]["spent_tokens"] == len(model_messages) * 16
    assert snapshot["model_budget"]["active_calls"] == 0
    assert snapshot["model_budget"]["unknown_calls"] == 0
    assert len(snapshot["model_calls"]) == len(model_messages)


def test_terminal_model_error_does_not_multiply_through_scheduler_redelivery(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    transport_calls = []

    def reject(request):
        transport_calls.append(request)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    runtime = ResidentRuntime(
        tmp_path,
        team_model_factory=lambda _: DeepSeekOpenAIProvider(
            api_key="bad-key", transport=httpx.MockTransport(reject)
        ),
    )
    created = runtime.submit_task(
        TaskRequest(
            title="Terminal model failure",
            brief="A governed model error must not look like a Worker crash.",
            model_mode="deepseek",
        )
    )
    runtime.run_until_idle(max_steps=8)
    events = runtime.store.read_all(run_id=created.run_id)

    assert runtime.store.projection("run", created.run_id)["status"] == "failed"
    assert not runtime.scheduler.has_pending()
    assert len(transport_calls) == 1
    assert any(event.type == "model.call.failed" for event in events)
    assert not any(event.type == "runtime.agent.crashed" for event in events)
    assert all(
        call["attempt_count"] == 1
        for call in runtime.store.list_model_calls(run_id=created.run_id)
    )


def test_persisted_model_run_failure_request_is_finalized_after_restart(tmp_path):
    first = ResidentRuntime(tmp_path)
    created = first.submit_task(
        TaskRequest(title="Recover terminal failure", brief="Do not revive this Run.")
    )
    identity = next(
        event
        for event in first.store.read_all(run_id=created.run_id)
        if event.type == "run.created"
    )
    first.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.failure.requested",
            source="runtime.model-governance",
            payload={
                "reason": "persisted terminal model failure",
                "failure_class": "terminal_model_call",
                "failure_scope": "run",
            },
            causation_id=identity.id,
        )
    )

    recovered = ResidentRuntime(tmp_path)

    assert recovered.store.projection("run", created.run_id)["status"] == "failed"
    assert not recovered.scheduler.has_pending()
    assert any(
        event.type == "run.failed"
        for event in recovered.store.read_all(run_id=created.run_id)
    )


def test_failed_run_reclaims_a_remote_claimed_mailbox_delivery(tmp_path):
    data_dir = tmp_path / "resident"
    first = ResidentRuntime(data_dir)
    created = first.submit_task(
        TaskRequest(title="Reclaim failed delivery", brief="Fence the dead Runtime.")
    )
    mailbox = first.mailboxes["coordinator"]
    delivery = mailbox.peek()
    assert delivery is not None
    claim_keys = first.scheduler.claim_keys_for("coordinator", mailbox, delivery)
    assert first.store.claim_work(
        claim_keys=claim_keys,
        owner_id="dead-runtime",
        ttl_seconds=60,
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    ) is not None
    identity = next(
        event
        for event in first.store.read_all(run_id=created.run_id)
        if event.type == "run.created"
    )
    first.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.failure.requested",
            source="runtime.model-governance",
            payload={
                "reason": "remote model call failed",
                "failure_class": "terminal_model_call",
                "failure_scope": "run",
            },
            causation_id=identity.id,
        )
    )
    first.scheduler.shutdown()

    recovered = ResidentRuntime(data_dir)

    assert recovered.store.projection("run", created.run_id)["status"] == "failed"
    assert recovered.mailboxes["coordinator"].peek() is None
    assert all(
        recovered.store.work_claim(claim_key)["state"] != "active"
        for claim_key in claim_keys
    )


def test_concurrent_terminal_model_failures_create_distinct_requests(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Concurrent failures", brief="First failure wins safely.")
    )
    identities = runtime.store.read_all(run_id=created.run_id)[:2]
    original_append = runtime._append_deterministic
    rendezvous = Barrier(2)

    def synchronized_append(identity, key, event_type, payload, **kwargs):
        if event_type == "run.failure.requested":
            rendezvous.wait()
        return original_append(identity, key, event_type, payload, **kwargs)

    monkeypatch.setattr(runtime, "_append_deterministic", synchronized_append)

    def fail(index):
        try:
            runtime._fail_team_run_from_model(
                identities[index], f"model failure from worker {index}"
            )
        except Exception as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        errors = list(pool.map(fail, range(2)))

    assert errors == [None, None]
    requests = [
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "run.failure.requested"
    ]
    assert len(requests) == 2
    assert len({event.id for event in requests}) == 2

    runtime._reconcile_routes()
    assert runtime.store.projection("run", created.run_id)["status"] == "failed"
