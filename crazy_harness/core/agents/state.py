from __future__ import annotations

from enum import StrEnum


class LoopPhase(StrEnum):
    CONTEXT_BUILDING = "context_building"
    MODEL_CALLING = "model_calling"
    DECISION_VALIDATING = "decision_validating"
    ACTION_AUTHORIZING = "action_authorizing"
    ACTION_EXECUTING = "action_executing"
    RESULT_RECORDING = "result_recording"
    WAITING = "waiting"
    SUBMITTED = "submitted"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[LoopPhase, set[LoopPhase]] = {
    LoopPhase.CONTEXT_BUILDING: {LoopPhase.MODEL_CALLING},
    LoopPhase.MODEL_CALLING: {LoopPhase.DECISION_VALIDATING, LoopPhase.FAILED},
    LoopPhase.DECISION_VALIDATING: {LoopPhase.ACTION_AUTHORIZING, LoopPhase.FAILED},
    LoopPhase.ACTION_AUTHORIZING: {LoopPhase.ACTION_EXECUTING, LoopPhase.RESULT_RECORDING, LoopPhase.FAILED},
    LoopPhase.ACTION_EXECUTING: {LoopPhase.RESULT_RECORDING, LoopPhase.FAILED},
    LoopPhase.RESULT_RECORDING: {LoopPhase.WAITING, LoopPhase.SUBMITTED, LoopPhase.FAILED},
    LoopPhase.WAITING: {LoopPhase.CONTEXT_BUILDING, LoopPhase.FAILED},
    LoopPhase.SUBMITTED: set(),
    LoopPhase.FAILED: set(),
}


def transition_allowed(previous: LoopPhase, current: LoopPhase) -> bool:
    return current in ALLOWED_TRANSITIONS[previous]
