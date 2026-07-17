from crazy_harness.worlds.cicd.artifacts import ReleasePlan, ReviewDecision, RiskReport


def test_cicd_artifacts_validate():
    RiskReport(risks=["test risk"])
    ReleasePlan(risk_level="low", approval_required=False)
    ReviewDecision(decision="approve")
