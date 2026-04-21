"""
Tests Piece 2 (output-locator discovery) with exactly ONE real API call,
against the REAL results page — deliberately navigating straight there
instead of re-running full discovery, to conserve your daily quota.

Run target-app/app.py first.
"""

import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
from openai import OpenAI

from src.artifact.schema import SafetyPolicy, ActionRiskLevel, ActionType, Step
from src.browser.controller import BrowserController
from src.discovery.output_locator import (
    build_value_inventory, identify_output_locators, build_output_field,
)

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

controller = BrowserController(safety_policy, headless=False, slow_mo_ms=300,
                                step_logger=lambda e: None)
controller.start()

try:
    nav_step = Step(order=1, name="go", action=ActionType.NAVIGATE,
                     value="http://localhost:5000/member/results?member_id=10001",
                     risk_level=ActionRiskLevel.SAFE, error_detectors=[])
    controller.execute_step(nav_step, {})

    obs = controller.observe()
    page_text = "\n".join(obs.text_by_frame.values())
    value_inventory = build_value_inventory(controller.page)
    print(f"Found {len(value_inventory)} readable value cells.")

    output_specs = [
        {"name": "savings_balance", "description": "The member's savings account balance"},
        {"name": "member_name", "description": "The member's full name"},
    ]

    raw = identify_output_locators(
        client, output_specs=output_specs, value_inventory=value_inventory, page_text=page_text,
    )
    print("\nRaw LLM identification:", raw)

    for spec in output_specs:
        name = spec["name"]
        if name not in raw:
            print(f"\n{name}: LLM did not confidently identify this one.")
            continue
        choice = raw[name]
        entry = value_inventory[choice["value_index"]]
        try:
            field = build_output_field(
                name=name, description=spec["description"], sensitive=False,
                entry=entry, anchor_text=choice["anchor_text"],
            )
        except ValueError as e:
            print(f"\n{name}: REJECTED — {e}")
            continue

        # Prove it actually works: resolve it live, right now, on this page.
        from src.browser.locator import resolve
        pw_loc, winning, scope = resolve(controller.page, field.extract_from, timeout_ms=5000)
        extracted = pw_loc.inner_text(timeout=5000).strip()
        xpath = field.extract_from.locators[0].value
        print(f"\n{name}:")
        print(f"  constructed xpath: {xpath}")
        print(f"  ACTUALLY EXTRACTED (live, right now): {extracted!r}")

finally:
    input("\n(press Enter to close the browser window) ")
    controller.stop()
