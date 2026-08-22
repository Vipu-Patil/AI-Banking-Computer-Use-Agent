"""
The FIRST real test of an actual OpenAI API call in this project.

Deliberately uses a FAKE, hand-written page state instead of a real
browser — this isolates one question at a time: "does the API call
and response-parsing actually work?" separate from "does the browser
feed it correctly?" (that's the next test, once this one passes).

Run:
    python test_llm_real_call.py

Requires OPENAI_API_KEY to be set (via .env or your environment).
"""

import sys
sys.path.insert(0, ".")

import os
from dotenv import load_dotenv
from openai import OpenAI

from src.discovery.llm_client import decide_next_action
from src.discovery.element_inventory import InventoryEntry, Locator, LocatorStrategy

load_dotenv()
client = OpenAI(
    api_key=os.environ["GEMINI_API_KEY"],
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

# A fabricated version of what the REAL element_inventory.py would have
# produced on the actual /member/search page — hand-written here so we
# don't need a live browser for this one test.
fake_inventory = [
    InventoryEntry(
        index=0, tag="input", visible_text="", input_type="text",
        locator=Locator(strategy=LocatorStrategy.CSS, value="input[name='member_id']"),
        frame_name=None,
    ),
    InventoryEntry(
        index=1, tag="td", visible_text="Search", input_type=None,
        locator=Locator(strategy=LocatorStrategy.TEXT, value="Search"),
        frame_name=None,
    ),
]

fake_page_text = "CreditUnion Core Back Office\nMember Search\nMember ID: [        ]\n[Search]"

decision = decide_next_action(
    client,
    goal="Look up member {{member_id}} and read their savings balance.",
    input_params={"member_id": "10001"},
    page_text=fake_page_text,
    inventory=fake_inventory,
    history_summary="",
)

print("=== Scenario 1: fresh search page (should choose: type member id) ===")
print("REASONING:", decision.reasoning)
print("DONE:", decision.done, " STUCK:", decision.stuck)
if decision.action:
    print("ACTION TYPE:", decision.action.type)
    print("TARGET INDEX:", decision.action.target_index)
    print("VALUE:", decision.action.value)
    print("RISK LEVEL:", decision.action.risk_level)
    print("CHECKPOINT HINT:", decision.action.checkpoint_hint)


# --- Scenario 2: member id already typed, should now click Search ---
fake_page_text_2 = "CreditUnion Core Back Office\nMember Search\nMember ID: [10001   ]\n[Search]"
fake_inventory_2 = [
    InventoryEntry(
        index=0, tag="input", visible_text="", input_type="text",
        locator=Locator(strategy=LocatorStrategy.CSS, value="input[name='member_id']"),
        frame_name=None,
    ),
    InventoryEntry(
        index=1, tag="td", visible_text="Search", input_type=None,
        locator=Locator(strategy=LocatorStrategy.TEXT, value="Search"),
        frame_name=None,
    ),
]
decision2 = decide_next_action(
    client,
    goal="Look up member {{member_id}} and read their savings balance.",
    input_params={"member_id": "10001"},
    page_text=fake_page_text_2,
    inventory=fake_inventory_2,
    history_summary="Step 1: typed {{member_id}} into the member ID field.",
)
print("\n=== Scenario 2: id already typed (should choose: click Search) ===")
print("REASONING:", decision2.reasoning)
print("DONE:", decision2.done, " STUCK:", decision2.stuck)
if decision2.action:
    print("ACTION TYPE:", decision2.action.type)
    print("TARGET INDEX:", decision2.action.target_index)


# --- Scenario 3: a business outcome page (member not found) ---
fake_page_text_3 = "CreditUnion Core Back Office\nMember Search\nNo member found matching that ID."
decision3 = decide_next_action(
    client,
    goal="Look up member {{member_id}} and read their savings balance.",
    input_params={"member_id": "00000"},
    page_text=fake_page_text_3,
    inventory=[],
    history_summary="Step 1: typed member id. Step 2: clicked Search.",
)
print("\n=== Scenario 3: 'not found' page (should choose: done=true, success=true) ===")
print("REASONING:", decision3.reasoning)
print("DONE:", decision3.done, " SUCCESS:", decision3.success, " STUCK:", decision3.stuck)
print("OUTCOME NOTE:", decision3.outcome_note)