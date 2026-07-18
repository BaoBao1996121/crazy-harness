from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.store import SQLiteEventStore, WorkClaimLost
from crazy_harness.core.events import Event
from crazy_harness.core.models import ModelMessage, ModelProvider, ModelResponse


class ModelBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(default=250_000, ge=1)
    max_cost_usd: Decimal = Field(default=Decimal("0.10"), gt=0)
    max_concurrent_calls: int = Field(default=2, ge=1, le=64)
    max_output_tokens_per_call: int = Field(default=4096, ge=1, le=384_000)
    max_retries_per_call: int = Field(default=2, ge=0, le=5)


class DeepSeekPriceCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    effective_date: str
    input_cache_hit_usd_per_million: Decimal
    input_cache_miss_usd_per_million: Decimal
    output_usd_per_million: Decimal
    source_url: str

    def estimate_microusd(
        self, *, input_tokens: int, output_tokens: int
    ) -> int:
        return _ceil_microusd(
            Decimal(input_tokens) * self.input_cache_miss_usd_per_million
            + Decimal(output_tokens) * self.output_usd_per_million
        )

    def actual_microusd(self, usage: dict[str, int]) -> int:
        return _ceil_microusd(
            Decimal(usage["prompt_cache_hit_tokens"])
            * self.input_cache_hit_usd_per_million
            + Decimal(usage["prompt_cache_miss_tokens"])
            * self.input_cache_miss_usd_per_million
            + Decimal(usage["completion_tokens"])
            * self.output_usd_per_million
        )


_PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
_PRICE_CARDS = {
    "deepseek-v4-flash": DeepSeekPriceCard(
        model="deepseek-v4-flash",
        effective_date="2026-07-18",
        input_cache_hit_usd_per_million=Decimal("0.0028"),
        input_cache_miss_usd_per_million=Decimal("0.14"),
        output_usd_per_million=Decimal("0.28"),
        source_url=_PRICE_SOURCE,
    ),
    "deepseek-v4-pro": DeepSeekPriceCard(
        model="deepseek-v4-pro",
        effective_date="2026-07-18",
        input_cache_hit_usd_per_million=Decimal("0.003625"),
        input_cache_miss_usd_per_million=Decimal("0.435"),
        output_usd_per_million=Decimal("0.87"),
        source_url=_PRICE_SOURCE,
    ),
}


class ModelCallFailed(RuntimeError):
    """The model authority exhausted its own policy; Scheduler must not retry it."""

    def __init__(self, *, state: str, error: Exception) -> None:
        self.state = state
        self.error_type = type(error).__name__
        super().__init__(f"model call {state}: {self.error_type}: {error}")


class PersistentModelCallAuthority:
    """Reserve, invoke, retry, and reconcile model calls against SQLite facts."""

    STALE_CALL_SECONDS = 240

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        before_reserve: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.sleep = sleep
        self.before_reserve = before_reserve

    def recover_unresolved(self, *, request_event: Event) -> None:
        """Fail closed when a persisted transport attempt has no durable response."""

        call = self.store.model_call(request_event.id)
        if call is None:
            # The process stopped before reservation, so no transport could occur.
            return
        state = str(call["state"])
        attempts = int(call["attempt_count"])
        if attempts == 0 and state in {"reserved", "failed"}:
            if state == "reserved":
                self.store.fail_model_call(
                    request_event.id,
                    uncertain=False,
                    error_type="AbandonedReservation",
                    error_message="process stopped before a transport attempt started",
                )
            self._append(
                request_event,
                "recovery:failed",
                "model.call.failed",
                {
                    "call_id": request_event.id,
                    "attempt": 0,
                    "state": "failed",
                    "error_type": "AbandonedReservation",
                    "reason": "no transport attempt started; a new Turn may retry",
                },
            )
            return

        recovered_state = "failed" if state == "failed" else "unknown"
        if state not in {"failed", "unknown"}:
            self.store.fail_model_call(
                request_event.id,
                uncertain=True,
                error_type="UnresolvedModelAttempt",
                error_message="transport may have completed before response persistence",
            )
        self._append(
            request_event,
            f"recovery:{recovered_state}",
            "model.call.failed",
            {
                "call_id": request_event.id,
                "attempt": attempts,
                "state": recovered_state,
                "error_type": "UnresolvedModelAttempt",
                "reason": "persisted Attempt has no durable model.completed",
            },
        )
        error = RuntimeError(
            "persisted model attempt has no durable response; resampling is unsafe"
        )
        raise ModelCallFailed(state=recovered_state, error=error)

    def complete(
        self,
        *,
        request_event: Event,
        provider: ModelProvider,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        response_schema: dict[str, Any] | None,
    ) -> ModelResponse:
        run = self.store.projection("run", request_event.run_id) or {}
        if run.get("model_mode", "scripted") != "deepseek":
            return provider.complete(
                messages, tools=tools, response_schema=response_schema
            )
        budget = ModelBudgetConfig.model_validate(run.get("model_budget") or {})
        self.store.recover_stale_model_calls(
            run_id=request_event.run_id,
            stale_before=datetime.now(timezone.utc)
            - timedelta(seconds=self.STALE_CALL_SECONDS),
        )
        model = str(getattr(provider, "model", "deepseek-v4-flash"))
        price = _price_card(model)
        output_tokens = int(
            getattr(provider, "max_tokens", budget.max_output_tokens_per_call)
        )
        if output_tokens > budget.max_output_tokens_per_call:
            raise ValueError("provider max_tokens exceeds the persisted Run budget")
        input_tokens = _input_token_upper_bound(messages, tools, response_schema)
        reserved_cost = price.estimate_microusd(
            input_tokens=input_tokens, output_tokens=output_tokens
        )
        if self.before_reserve is not None:
            self.before_reserve()
        self.store.reserve_model_call(
            call_id=request_event.id,
            run_id=request_event.run_id,
            task_id=request_event.task_id,
            agent_id=request_event.source,
            provider=type(provider).__name__,
            model=model,
            reserved_input_tokens=input_tokens,
            reserved_output_tokens=output_tokens,
            reserved_cost_microusd=reserved_cost,
            max_total_tokens=budget.max_total_tokens,
            max_cost_microusd=_usd_to_microusd(budget.max_cost_usd),
            max_concurrent_calls=budget.max_concurrent_calls,
        )
        try:
            self._append(
                request_event,
                "reserved",
                "model.call.reserved",
                {
                    "call_id": request_event.id,
                    "model": model,
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_cost_microusd": reserved_cost,
                    "price_effective_date": price.effective_date,
                    "cost_kind": "estimate",
                },
            )
        except Exception as exc:
            self.store.fail_model_call(
                request_event.id,
                uncertain=False,
                error_type=type(exc).__name__,
                error_message="reservation audit event was not committed",
            )
            raise
        attempts = budget.max_retries_per_call + 1
        for attempt_index in range(attempts):
            attempt = self.store.start_model_call_attempt(request_event.id)
            try:
                self._append(
                    request_event,
                    f"attempt:{attempt}",
                    "model.call.attempt.started",
                    {
                        "call_id": request_event.id,
                        "attempt": attempt,
                        "model": model,
                    },
                )
            except Exception as exc:
                self.store.fail_model_call(
                    request_event.id,
                    uncertain=False,
                    error_type=type(exc).__name__,
                    error_message="attempt audit event was not committed",
                )
                raise
            try:
                return provider.complete(
                    messages, tools=tools, response_schema=response_schema
                )
            except Exception as exc:
                retryable, uncertain = _classify_error(exc)
                if retryable and attempt_index + 1 < attempts:
                    delay = min(2.0, 0.25 * (2**attempt_index))
                    self._append(
                        request_event,
                        f"retry:{attempt}",
                        "model.call.retry.scheduled",
                        {
                            "call_id": request_event.id,
                            "attempt": attempt,
                            "delay_seconds": delay,
                            "error_type": type(exc).__name__,
                        },
                    )
                    self.sleep(delay)
                    continue
                self.store.fail_model_call(
                    request_event.id,
                    uncertain=uncertain,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                self._append(
                    request_event,
                    "failed",
                    "model.call.failed",
                    {
                        "call_id": request_event.id,
                        "attempt": attempt,
                        "state": "unknown" if uncertain else "failed",
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                )
                raise ModelCallFailed(
                    state="unknown" if uncertain else "failed",
                    error=exc,
                ) from exc
        raise AssertionError("model attempt loop exhausted without a result")

    def reconcile(self, *, request_event: Event, completion_event: Event) -> bool:
        call = self.store.model_call(request_event.id)
        if call is None:
            return False
        usage, usage_quality = _normalize_usage(
            dict(completion_event.payload.get("usage") or {}),
            fallback_input_tokens=int(call["reserved_input_tokens"]),
            fallback_output_tokens=int(call["reserved_output_tokens"]),
        )
        price = _price_card(str(call["model"]))
        actual_cost = price.actual_microusd(usage)
        changed = self.store.reconcile_model_call(
            call_id=request_event.id,
            completion_event_id=completion_event.id,
            usage=usage,
            actual_cost_microusd=actual_cost,
        )
        try:
            self._append(
                completion_event,
                "usage",
                "model.usage.recorded",
                {
                    "call_id": request_event.id,
                    "completion_event_id": completion_event.id,
                    "usage": usage,
                    "usage_quality": usage_quality,
                    "estimated_cost_microusd": actual_cost,
                    "cost_kind": "estimate",
                    "price_effective_date": price.effective_date,
                    "price_source": price.source_url,
                },
            )
        except WorkClaimLost:
            # Accounting is still retained when a cancelled Run rejects late events.
            pass
        return changed

    def _append(
        self,
        identity: Event,
        key: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> Event:
        event_id = str(
            uuid5(NAMESPACE_URL, f"model-governance:{identity.id}:{key}")
        )
        return self.store.append(
            Event(
                id=event_id,
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source="runtime.model-governance",
                payload=payload,
                causation_id=identity.id,
            )
        )


def _input_token_upper_bound(
    messages: list[ModelMessage],
    tools: list[dict[str, Any]] | None,
    response_schema: dict[str, Any] | None,
) -> int:
    encoded = json.dumps(
        {
            "messages": [message.model_dump(mode="json") for message in messages],
            "tools": tools or [],
            "response_schema": response_schema or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + 32


def _normalize_usage(
    usage: dict[str, Any],
    *,
    fallback_input_tokens: int,
    fallback_output_tokens: int,
) -> tuple[dict[str, int], str]:
    """Normalize external usage without allowing malformed values to free budget."""

    prompt, prompt_fallback = _non_negative_usage_int(
        usage, "prompt_tokens", fallback_input_tokens
    )
    completion, completion_fallback = _non_negative_usage_int(
        usage, "completion_tokens", fallback_output_tokens
    )
    hit, hit_fallback = _non_negative_usage_int(
        usage, "prompt_cache_hit_tokens", 0
    )
    miss, miss_fallback = _non_negative_usage_int(
        usage, "prompt_cache_miss_tokens", max(0, prompt - hit)
    )
    cache_inconsistent = hit + miss != prompt
    if cache_inconsistent:
        # A malformed cache split must never turn expensive miss tokens into cheap hits.
        prompt = max(prompt, hit + miss)
        hit = 0
        miss = prompt
    total, total_fallback = _non_negative_usage_int(
        usage, "total_tokens", prompt + completion
    )
    total = max(total, prompt + completion)
    normalized = {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    used_fallback = any(
        (
            prompt_fallback,
            completion_fallback,
            hit_fallback,
            miss_fallback,
            total_fallback,
            cache_inconsistent,
        )
    )
    return normalized, "pessimistic_fallback" if used_fallback else "reported"


def _non_negative_usage_int(
    usage: dict[str, Any], key: str, fallback: int
) -> tuple[int, bool]:
    if key not in usage or isinstance(usage[key], bool):
        return fallback, True
    try:
        value = int(usage[key])
    except (TypeError, ValueError, OverflowError):
        return fallback, True
    if value < 0:
        return fallback, True
    return value, False


def _classify_error(error: Exception) -> tuple[bool, bool]:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {429, 500, 503}, False
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True, False
    if isinstance(error, httpx.TransportError):
        return False, True
    return False, False


def _price_card(model: str) -> DeepSeekPriceCard:
    try:
        return _PRICE_CARDS[model]
    except KeyError as exc:
        raise ValueError(f"no versioned price card for model: {model}") from exc


def _ceil_microusd(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _usd_to_microusd(value: Decimal) -> int:
    return _ceil_microusd(value * Decimal(1_000_000))
