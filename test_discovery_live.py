"""
The FIRST real, live, end-to-end discovery run: real browser, real
Gemini API calls, real target app. No fakes, no mocks — this either
actually discovers the lookup_member_balance flow on its own, or it
doesn't, and either way that's real information.

Run target-app/app.py first, same as every other demo.
"""

import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
from openai import OpenAI

from src.artifact.schema import SafetyPolicy, ActionRiskLevel, ActionType
from src.browser.controller import BrowserController
from src.discovery.loop import run_discovery

load_dotenv()

client = OpenAI(
    api_key=os.environ["GEMINI_API_KEY"],
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

safety_policy = SafetyPolicy(
    allowed_domains=["localhost:5000", "127.0.0.1:5000"],
    allowed_actions=[a.value for a in ActionType],
    max_unconfirmed_risk=ActionRiskLevel.MODERATE,
    require_human_confirmation_for=["submit", "confirm"],
)

controller = BrowserController(
    safety_policy,
    headless=False,
    slow_mo_ms=500,
    home_url="http://localhost:5000/home",
    evidence_dir="evidence/logs",
    step_logger=lambda e: print("  [LOG]", e),
)
controller.start()

try:
    def on_turn(info):
        print(f"\n  --- Turn (step {info['order']}) ---")
        print(f"  Inventory shown to LLM:\n" +
              "\n".join(f"    {line}" for line in info["inventory_shown"].splitlines()))
        print(f"  Raw decision: {info['decision_raw']}")

    result = run_discovery(
        controller=controller,
        client=client,
        goal="Look up member {{member_id}} and read their savings account balance.",
        entry_url="http://localhost:5000/member/search",
        input_params={"member_id": "10001"},
        max_steps=10,
        on_turn=on_turn,
    )

    print("\n" + "=" * 60)
    print("DISCOVERY RESULT")
    print("=" * 60)
    print("success:", result.success)
    print("business_outcome:", result.business_outcome)
    print("stuck:", result.stuck)
    print("dead_end:", result.dead_end)
    print("stop_reason:", result.stop_reason)
    print("outcome_note:", result.outcome_note)
    print(f"\nTrajectory ({len(result.trajectory)} steps):")
    for i, entry in enumerate(result.trajectory, start=1):
        print(f"  {i}. [{entry.outcome}] {entry.step.action} - {entry.step.name}")
        print(f"     reasoning: {entry.reasoning}")
        if entry.step.target:
            print(f"     target: {entry.step.target.locators[0].strategy}:{entry.step.target.locators[0].value}")

finally:
    input("\n(press Enter to close the browser window) ")
    controller.stop()