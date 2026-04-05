"""
Tests the ONE thing demo_lookup_balance.py never exercised: the
SafetyPolicy confirmation gate for risky actions (Section 3.4).

open_sub_account.json has a "submit" step at risk_level="high", and its
safety_policy.require_human_confirmation_for includes "submit". So:

  Run 1: on_risky_action always returns False (nobody confirmed) ->
         execution should STOP at step 9 with a RecoverableCondition,
         and never actually create an account.

  Run 2: on_risky_action always returns True (a human said "go ahead") ->
         execution should proceed through submit and reach the real
         confirmation page, extracting new_account_number/status.

Run target-app/app.py first, same as the other demo.
"""

import json
import sys

sys.path.insert(0, ".")

from src.artifact.schema import Artifact
from src.browser.controller import BrowserController
from src.browser.errors import BusinessOutcomeResult, RecoverableCondition, HardFailure


def load_artifact(path: str) -> Artifact:
    return Artifact(**json.load(open(path)))


def run(artifact: Artifact, params: dict, *, confirm_risky: bool) -> None:
    ctrl = BrowserController(
        artifact.safety_policy,
        headless=False,
        slow_mo_ms=400,
        home_url="http://localhost:5000/home",
        evidence_dir="evidence/logs",
        on_risky_action=lambda step: confirm_risky,
        step_logger=lambda e: print("  [LOG]", e),
    )
    ctrl.start()
    try:
        for step in artifact.steps:
            outcome = ctrl.execute_step(step, params)
            if isinstance(outcome, BusinessOutcomeResult):
                print(f"[BUSINESS OUTCOME] -> {outcome.outcome_code}: {outcome.outcome_message}")
                return
        outputs = ctrl.extract_all_outputs(artifact.outputs)
        print(f"[SUCCESS] -> {outputs}")
    except RecoverableCondition as e:
        print(f"[RECOVERABLE] -> {e.outcome_code}: {e.outcome_message} "
              f"(recovery attempted: {e.attempted_recovery})")
    except HardFailure as e:
        print(f"[HARD FAILURE] -> {e.to_dict()}")
    finally:
        input("  (press Enter to close this browser window and continue) ")
        ctrl.stop()


if __name__ == "__main__":
    artifact = load_artifact("evidence/artifacts/open_sub_account.json")
    params = {
        "member_id": "10001",
        "account_type": "Savings",
        "initial_deposit": "50.00",
        "nickname": "Vacation Fund",
    }

    print("=== Run 1: risky action NOT confirmed (should block before submit) ===")
    run(artifact, params, confirm_risky=False)

    print("\n=== Run 2: risky action confirmed (should complete and create account) ===")
    run(artifact, params, confirm_risky=True)