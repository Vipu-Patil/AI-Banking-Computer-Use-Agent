"""
Manual smoke test for BrowserController — no discovery loop, no replay
engine yet (those are steps 5 and 7). This just proves the browser
control layer can drive the REAL target app end to end using the
ALREADY-WRITTEN lookup_member_balance.json artifact, and correctly tells
apart a happy path from a genuine business outcome.

Run the target app first:
    python target-app/app.py

Then, in another terminal:
    python demo_lookup_balance.py
"""

import json
import sys

sys.path.insert(0, ".")

from src.artifact.schema import Artifact
from src.browser.controller import BrowserController
from src.browser.errors import BusinessOutcomeResult, RecoverableCondition, HardFailure


def load_artifact(path: str) -> Artifact:
    return Artifact(**json.load(open(path)))


def run(artifact: Artifact, member_id: str) -> None:
    ctrl = BrowserController(
        artifact.safety_policy,
        headless=False,       # watch it happen
        slow_mo_ms=600,        # pause ~0.6s between every Playwright action
        home_url="http://localhost:5000/home",
        evidence_dir="evidence/logs",
        step_logger=lambda e: print("  [LOG]", e),
    )
    ctrl.start()
    try:
        params = {"member_id": member_id}
        for step in artifact.steps:
            outcome = ctrl.execute_step(step, params)
            if isinstance(outcome, BusinessOutcomeResult):
                print(f"[BUSINESS OUTCOME] member={member_id} -> "
                      f"{outcome.outcome_code}: {outcome.outcome_message}")
                return
        # All steps completed with no business outcome -> extract results.
        outputs = ctrl.extract_all_outputs(artifact.outputs)
        print(f"[SUCCESS] member={member_id} -> {outputs}")
    except RecoverableCondition as e:
        print(f"[RECOVERABLE] member={member_id} -> {e.outcome_code}: {e.outcome_message} "
              f"(recovery attempted: {e.attempted_recovery}, action: {e.recovery_action})")
    except HardFailure as e:
        print(f"[HARD FAILURE] member={member_id} -> {e.to_dict()}")
    finally:
        input("  (press Enter to close this browser window and continue) ")
        ctrl.stop()


if __name__ == "__main__":
    artifact = load_artifact("evidence/artifacts/lookup_member_balance.json")
    print("=== existing member (happy path) ===")
    run(artifact, "10001")
    print("\n=== nonexistent member (business outcome) ===")
    run(artifact, "00000")
    print("\n=== session-expired trigger (recoverable) ===")
    run(artifact, "99999")