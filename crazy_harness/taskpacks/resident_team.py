from __future__ import annotations

from crazy_harness.core.a2a.orchestration import TeamContract, TeamStageSpec


class ResidentDemoTeamTaskPack:
    """Replaceable business contract for the resident Team learning scenario."""

    task_pack_id = "resident-demo"

    def team_contract(self) -> TeamContract:
        return TeamContract(
            contract_id=self.task_pack_id,
            version=1,
            max_parallel_assignments=1,
            lease_seconds=30,
            stages=(
                TeamStageSpec(
                    stage_id="evidence",
                    result_kind="evidence",
                    goal="Collect verifiable evidence for the incoming task.",
                    required_capabilities=frozenset({"evidence.collect"}),
                    exit_criteria=(
                        "tool evidence is persisted",
                        "evidence refs are returned",
                    ),
                    completion_event_type="evidence.recorded",
                ),
                TeamStageSpec(
                    stage_id="artifact",
                    result_kind="artifact",
                    goal="Compose a bounded execution artifact from the collected evidence.",
                    required_capabilities=frozenset({"artifact.compose"}),
                    exit_criteria=(
                        "one peer reconciliation is complete",
                        "artifact cites evidence",
                    ),
                    depends_on=("evidence",),
                    completion_event_type="artifact.recorded",
                ),
                TeamStageSpec(
                    stage_id="review",
                    result_kind="review",
                    goal="Independently review the artifact and its evidence.",
                    required_capabilities=frozenset({"artifact.review"}),
                    exit_criteria=(
                        "review decision is explicit",
                        "decision cites evidence",
                    ),
                    depends_on=("artifact",),
                    completion_event_type="review.recorded",
                ),
            ),
        )
