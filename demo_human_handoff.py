"""
Tests the ONE piece of Step 4 that's never been exercised: human handoff
(Section 3.6). Specifically proves three things, not just that the
methods exist:

  1. While paused, execute_step() genuinely REFUSES to act (raises
     RecoverableCondition/AUTOMATION_PAUSED) rather than silently
     continuing — a fake handoff would just ignore the pause flag.
  2. The browser window never closes or reopens during the pause. It's
     the SAME live session a human would be looking at, not a new one
     spun up after the "handoff." We prove this by comparing page.url
     before pause and after resume.
  3. Once resumed, automation genuinely continues the remaining steps
     of the SAME artifact run — not a restarted run.

Run target-app/app.py first, same as the other demos.
"""

import json
import sys

sys.path.insert(0, ".")

from src.artifact.schema import Artifact
from src.browser.controller import BrowserController
from src.browser.errors import BusinessOutcomeResult, RecoverableCondition, HardFailure


def load_artifact(path: str) -> Artifact:
    return Artifact(**json.load(open(path)))


def main():
    artifact = load_artifact("evidence/artifacts/lookup_member_balance.json")
    params = {"member_id": "10001"}

    ctrl = BrowserController(
        artifact.safety_policy,
        headless=False,
        slow_mo_ms=400,
        home_url="http://localhost:5000/home",
        evidence_dir="evidence/logs",
        step_logger=lambda e: print("  [LOG]", e),
    )
    ctrl.start()

    steps = {s.order: s for s in artifact.steps}

    try:
        # --- Run the first two steps completely normally ---
        print("--- Automation runs steps 1-2 normally ---")
        ctrl.execute_step(steps[1], params)   # navigate
        ctrl.execute_step(steps[2], params)   # type member id

        url_before_pause = ctrl.page.url
        print(f"\nURL right before pausing: {url_before_pause}")

        # --- Simulate: the system decided a human should look at this
        #     before the (moderate/high-risk-ish) next step proceeds ---
        print("\n--- Pausing for human ---")
        ctrl.pause_for_human("Demo: handing control to a human before clicking Search")
        print(f"controller_of_session is now: '{ctrl.controller_of_session}'")

        # --- Prove automation genuinely refuses to act while paused ---
        print("\n--- Attempting step 3 (click) WHILE PAUSED (should be refused) ---")
        try:
            ctrl.execute_step(steps[3], params)
            print("!! UNEXPECTED: automation was allowed to act while paused — this is a bug.")
        except RecoverableCondition as e:
            print(f"Correctly refused: [{e.outcome_code}] {e.outcome_message}")

        # --- A human is "looking at" the SAME window right now. Feel
        #     free to actually click around in the real browser window
        #     that's open, to convince yourself it's a live, untouched
        #     session, not a placeholder. ---
        input("\n(You are now 'the human.' Press Enter to resume automation) ")

        print("\n--- Resuming automation ---")
        ctrl.resume_from_human()
        print(f"controller_of_session is now: '{ctrl.controller_of_session}'")

        url_after_resume = ctrl.page.url
        print(f"URL right after resuming: {url_after_resume}")
        print(f"Same session (URL unchanged, no restart)? {url_before_pause == url_after_resume}")

        # --- Automation finishes the REMAINING steps of the SAME run ---
        print("\n--- Automation continues steps 3-4 to completion ---")
        for order in (3, 4):
            outcome = ctrl.execute_step(steps[order], params)
            if isinstance(outcome, BusinessOutcomeResult):
                print(f"[BUSINESS OUTCOME] -> {outcome.outcome_code}")
                return

        outputs = ctrl.extract_all_outputs(artifact.outputs)
        print(f"\n[SUCCESS] -> {outputs}")

    except HardFailure as e:
        print(f"[HARD FAILURE] -> {e.to_dict()}")
    finally:
        input("\n(press Enter to close the browser window) ")
        ctrl.stop()


if __name__ == "__main__":
    main()
