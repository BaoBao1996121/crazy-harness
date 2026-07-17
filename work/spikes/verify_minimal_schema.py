from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from crazy_harness.core.events import Event
from crazy_harness.worlds.cicd.artifacts import ReleasePlan, RiskReport

Event(run_id="r1", task_id="t1", type="seed", source="spike")
RiskReport(risks=["ok"])
ReleasePlan(risk_level="low", approval_required=False)
print("schemas ok")
