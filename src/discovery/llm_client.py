"""
LLM decision client (OpenAI).

Sends the LLM: the goal, the input parameters available to use, the
current page's text content, and the GROUNDED element inventory (see
element_inventory.py) — then asks for exactly one next action, as
strict JSON, chosen by INDEX into that inventory. The model never
invents a selector; it can only point at something we already found
and already have a real Locator for.

Uses OpenAI's Chat Completions API with `response_format={"type":
"json_object"}`, which forces the model to return valid JSON — no
code-fence stripping needed, though _extract_json() below still
tolerates one defensively in case that changes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Optional

from openai import RateLimitError

from src.discovery.element_inventory import InventoryEntry, render_for_prompt

MODEL = "gemini-3.5-flash-lite"  # 2.5-flash-lite deprecated for new users; this is its replacement

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 15  # free tier is ~5 req/min, so back off meaningfully

_SYSTEM_PROMPT = """You are a discovery agent operating a real legacy web application inside a browser, one action at a time, to accomplish a stated goal.

You do NOT write selectors. You are given a numbered list of every currently-interactive element on the page (its visible text and tag). To act on an element, you refer to it ONLY by its index number from that list.

Respond with ONLY a single JSON object, no prose, no markdown code fences, matching exactly this shape:

{
  "reasoning": "<one short sentence: why this action>",
  "done": <true|false>,
  "success": <true|false, only meaningful if done=true>,
  "outcome_note": "<if done=true, describe what happened: success, or which business outcome / error was hit>",
  "stuck": <true|false, set true instead of guessing if you genuinely don't know what to do next>,
  "action": {
    "type": "<click|type|select|wait|navigate>",
    "target_index": <int, required for click/type/select, omit for wait/navigate>,
    "value": "<string, required for type/select/navigate, omit otherwise>",
    "risk_level": "<safe|moderate|high>",
    "checkpoint_hint": "<one short sentence describing how you'd know THIS action worked>"
  }
}

Omit "action" entirely if done=true or stuck=true.

Rules:
- Only ever reference an index that actually appears in the CURRENT element list you were just given — indices are not stable across turns, the page changes after every action.
- Use "value" fields like {{param_name}} to reference an input parameter instead of writing its literal value, when the goal supplies one under that name — this keeps the action reusable.
- If the page shows a message indicating the goal cannot be completed as a normal, expected result (e.g. "not found", "access denied") rather than a technical error, set done=true, success=true, and describe the outcome in outcome_note — this is a valid result, not a failure.
- If the page shows a genuine technical error, or you have repeated the same failed action twice in a row, set stuck=true rather than guessing again.
"""


@dataclass
class ActionDecision:
    type: str
    target_index: Optional[int]
    value: Optional[str]
    risk_level: str
    checkpoint_hint: Optional[str]


@dataclass
class Decision:
    reasoning: str
    done: bool
    success: bool
    outcome_note: str
    stuck: bool
    action: Optional[ActionDecision]
    raw: dict  # kept for evidence logging


class DecisionParseError(Exception):
    def __init__(self, message: str, raw_text: str):
        self.raw_text = raw_text
        super().__init__(f"{message} | raw response: {raw_text[:500]}")


def _extract_json(raw_text: str) -> dict:
    """Tolerate the model wrapping JSON in a code fence anyway, since
    that's the single most common way models violate 'no markdown'
    instructions even when told not to."""
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Fall back to the first {...} block if there's stray prose.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise DecisionParseError(f"Could not parse JSON: {e}", raw_text) from e


def parse_decision(raw_text: str) -> Decision:
    data = _extract_json(raw_text)

    # Deliberately no longer hard-requiring specific keys to be present.
    # Live responses have shown the model omits "success" when done=false,
    # and omits "done" when stuck=true — both reasonable interpretations
    # of "only meaningful if X" language in the prompt. Defaulting missing
    # booleans to False is safe: if that default is wrong, the next check
    # (action required when done=false and stuck=false) still catches a
    # genuinely broken/empty response with a clear error.

    action = None
    if not data.get("done") and not data.get("stuck"):
        a = data.get("action")
        if a is None:
            raise DecisionParseError("action is required when done=false and stuck=false", raw_text)
        if "type" not in a:
            raise DecisionParseError("action.type is required", raw_text)
        action = ActionDecision(
            type=a["type"],
            target_index=a.get("target_index"),
            value=a.get("value"),
            risk_level=a.get("risk_level", "safe"),
            checkpoint_hint=a.get("checkpoint_hint"),
        )
        if action.type in ("click", "type", "select", "scroll") and action.target_index is None:
            raise DecisionParseError(
                f"action.type='{action.type}' requires a target_index, but none was given", raw_text,
            )

    return Decision(
        reasoning=data.get("reasoning", ""),
        done=bool(data.get("done", False)),
        success=bool(data.get("success", False)),
        outcome_note=data.get("outcome_note", ""),
        stuck=bool(data.get("stuck", False)),
        action=action,
        raw=data,
    )


def build_user_message(
    *, goal: str, input_params: dict[str, str], page_text: str,
    inventory: list[InventoryEntry], history_summary: str,
) -> str:
    return f"""GOAL: {goal}

AVAILABLE INPUT PARAMETERS (reference by {{{{name}}}} in "value"):
{json.dumps(input_params, indent=2)}

WHAT HAPPENED SO FAR:
{history_summary or "(nothing yet — this is the first action)"}

CURRENT PAGE TEXT (redacted):
{page_text[:3000]}

CURRENT INTERACTIVE ELEMENTS (refer to these ONLY by index):
{render_for_prompt(inventory)}

What is the single next action?"""


def decide_next_action(client, *, goal: str, input_params: dict[str, str],
                        page_text: str, inventory: list[InventoryEntry],
                        history_summary: str, model: str = MODEL) -> Decision:
    """`client` is an openai.OpenAI() instance, injected so this
    function (and the loop that calls it) stays unit-testable without
    a real API key — see the __main__ block below and
    demo_discovery_dryrun.py.

    Retries automatically on RateLimitError (HTTP 429), since Gemini's
    free tier allows only ~5 requests/minute for this model and a real
    discovery run makes one call per step — hitting this is normal,
    not exceptional, and shouldn't crash the whole run."""
    user_msg = build_user_message(
        goal=goal, input_params=input_params, page_text=page_text,
        inventory=inventory, history_summary=history_summary,
    )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw_text = response.choices[0].message.content
            return parse_decision(raw_text)

        except RateLimitError as e:
            last_error = e
            wait_seconds = _extract_retry_delay(e) or (BASE_BACKOFF_SECONDS * (attempt + 1))
            print(f"  [rate limited, attempt {attempt + 1}/{MAX_RETRIES}] "
                  f"waiting {wait_seconds:.0f}s before retrying...")
            time.sleep(wait_seconds)

    raise last_error


def _extract_retry_delay(error: RateLimitError) -> Optional[float]:
    """Google's error body includes an explicit RetryInfo.retryDelay
    (e.g. '46s') — prefer that over a guessed backoff when available."""
    try:
        body = error.response.json()
        for detail in body.get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                delay_str = detail.get("retryDelay", "")
                match = re.match(r"([\d.]+)s", delay_str)
                if match:
                    return float(match.group(1)) + 2  # small safety margin
    except Exception:
        pass
    return None


if __name__ == "__main__":
    # Offline test of the parsing logic only — no API key needed.
    # Run: python -m src.discovery.llm_client
    samples = [
        # Clean, well-formed response.
        '{"reasoning": "Type the member id", "done": false, "success": false, '
        '"outcome_note": "", "stuck": false, "action": {"type": "type", '
        '"target_index": 1, "value": "{{member_id}}", "risk_level": "safe", '
        '"checkpoint_hint": "input contains the member id"}}',

        # Wrapped in a code fence despite instructions not to.
        '```json\n{"reasoning": "Click search", "done": false, "success": false, '
        '"outcome_note": "", "stuck": false, "action": {"type": "click", '
        '"target_index": 2, "risk_level": "safe", "checkpoint_hint": "results page loads"}}\n```',

        # Done with a business outcome.
        '{"reasoning": "not found message shown", "done": true, "success": true, '
        '"outcome_note": "Member not found - valid business outcome", "stuck": false}',

        # Genuinely malformed - missing required field.
        '{"reasoning": "oops", "action": {"type": "click"}}',

        # Real case from a live Gemini response: "success" omitted
        # because done=false makes it not-yet-meaningful. Should parse
        # fine, not be rejected.
        '{"reasoning": "Type the member ID", "done": false, "stuck": false, '
        '"action": {"type": "type", "target_index": 0, "value": "{{member_id}}", '
        '"risk_level": "safe", "checkpoint_hint": "id is typed"}}',

        # Real case #2: "done" omitted entirely when stuck=true. Should
        # parse fine (done defaults to False), not be rejected.
        '{"reasoning": "The page failed to display interactive search '
        'elements or results after repeated waiting.", "stuck": true}',
    ]
    for i, s in enumerate(samples):
        print(f"--- sample {i} ---")
        try:
            d = parse_decision(s)
            print(" parsed OK:", d)
        except DecisionParseError as e:
            print(" correctly rejected:", e)

    print("\n(This only tested JSON parsing — no API call was made, no key needed.)")
    print("To test a REAL call once you have GEMINI_API_KEY set locally:")
    print("""
    import os
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv()
    client = OpenAI(
        api_key=os.environ["GEMINI_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    decision = decide_next_action(client, goal="...", input_params={...},
                                   page_text="...", inventory=[...], history_summary="")
    """)