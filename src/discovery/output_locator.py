"""
Value inventory — the read-side counterpart to element_inventory.py.

element_inventory.py grounds the LLM's ACTIONS (click/type) in real,
interactive elements. This module grounds output-locator discovery in
real, READABLE text cells — and critically, captures each cell's ROW
CONTEXT (what other text sits in the same table row), because a raw
value like "$5,230.45" is useless as a locator for other members, but
"the cell in the same row as the label 'Savings'" generalizes.

This directly reuses the ancestor::tr[1] fix from Step 4: once the LLM
tells us which value it wants and which nearby text is a stable label
for it, WE construct the actual XPath ourselves using that proven
pattern — the LLM never writes XPath, same "point at reality by index"
philosophy as element_inventory.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page, Frame

# Leaf-ish text-bearing tags worth offering as candidate VALUES.
_VALUE_SELECTOR = "td, span, font, b, div, li"
_MAX_ENTRIES = 80
_MAX_TEXT_LEN = 100


@dataclass
class ValueEntry:
    index: int
    text: str
    tag: str
    row_texts: list[str]      # other cell texts in the same <tr>, if any
    column_position: int | None  # 1-indexed position among the row's <td> children
    frame_name: str | None


def _row_context(el) -> tuple[list[str], int | None]:
    """Find the nearest ancestor <tr> (if any) and return the text of
    its other direct <td> children, plus this element's own column
    position among them (1-indexed) if it's a <td> or lives in one."""
    try:
        info = el.evaluate("""
            (node) => {
                let cell = node;
                while (cell && cell.tagName && cell.tagName.toLowerCase() !== 'td') {
                    cell = cell.parentElement;
                }
                if (!cell) return null;
                const row = cell.closest('tr');
                if (!row) return null;
                const cells = Array.from(row.children).filter(c => c.tagName.toLowerCase() === 'td');
                const position = cells.indexOf(cell) + 1;
                const texts = cells.map(c => c.innerText.trim());
                return { position, texts };
            }
        """)
    except Exception:
        return [], None
    if not info:
        return [], None
    return info["texts"], info["position"]


def build_value_inventory(page: Page) -> list[ValueEntry]:
    entries: list[ValueEntry] = []
    scopes: list[tuple[Page | Frame, str | None]] = [(page, None)]
    for f in page.frames:
        if f is not page.main_frame:
            scopes.append((f, f.name or None))

    seen_texts_at_position: set[tuple] = set()  # dedupe nested tags wrapping the same text

    for scope, frame_name in scopes:
        try:
            handles = scope.query_selector_all(_VALUE_SELECTOR)
        except Exception as e:
            print(f"  [value_inventory] could not query scope '{frame_name or 'page'}': {e}")
            continue

        for el in handles:
            if len(entries) >= _MAX_ENTRIES:
                break
            try:
                if not el.is_visible():
                    continue
                text = el.inner_text().strip()
                tag = el.evaluate("e => e.tagName.toLowerCase()")
            except Exception:
                continue

            if not text or len(text) > _MAX_TEXT_LEN:
                continue

            row_texts, col_pos = _row_context(el)
            # Dedupe: a <font> nested inside a <td> that's also matched
            # separately would otherwise produce two near-identical
            # entries for the same visual cell.
            dedupe_key = (text, col_pos, tuple(row_texts))
            if dedupe_key in seen_texts_at_position:
                continue
            seen_texts_at_position.add(dedupe_key)

            entries.append(ValueEntry(
                index=len(entries), text=text, tag=tag,
                row_texts=[t for t in row_texts if t != text],
                column_position=col_pos, frame_name=frame_name,
            ))

    return entries


def render_value_inventory_for_prompt(entries: list[ValueEntry]) -> str:
    lines = []
    for e in entries:
        row_info = f" | same row as: {e.row_texts}" if e.row_texts else ""
        lines.append(f"[{e.index}] \"{e.text}\"{row_info}")
    return "\n".join(lines) if lines else "(no readable values found)"


# ---------------------------------------------------------------------------
# LLM-assisted identification of WHICH entry holds each output, and WHICH
# nearby text is a stable anchor for it. The LLM never writes a locator —
# it only picks indices/exact strings from what we already found; WE build
# the actual XPath, reusing the ancestor::tr[1] pattern proven in Step 4.
# ---------------------------------------------------------------------------

import json
from src.artifact.schema import OutputField, ElementTarget, Locator, LocatorStrategy, ParamType
from src.discovery.llm_client import _extract_json, DecisionParseError

_OUTPUT_SYSTEM_PROMPT = """You are identifying WHERE specific output values live on a web page, so they can be re-extracted later without any AI involvement.

You are given a numbered list of every readable text cell on the page, and for each one, what OTHER text sits in the same table row (if any) — this is important, because the row-mate text is a stable LABEL (like "Savings") while the value itself (like "$5,230.45") will be different for every other record.

For each requested output field, respond with the index of the cell holding its value, AND the exact row-mate text (copied character-for-character from the "same row as" list) that best serves as a stable label for that value.

Respond with ONLY a single JSON object, no prose, no code fences:

{
  "field_name_here": {"value_index": <int>, "anchor_text": "<exact string copied from that entry's same-row list>"},
  ...
}

If a field's value cannot be confidently identified, omit it from the JSON entirely rather than guessing.
"""


def identify_output_locators(
    client, *, output_specs: list[dict], value_inventory: list[ValueEntry],
    page_text: str, model: str = None,
) -> dict[str, dict]:
    from src.discovery.llm_client import MODEL as DEFAULT_MODEL
    model = model or DEFAULT_MODEL

    user_msg = f"""OUTPUT FIELDS TO LOCATE:
{json.dumps(output_specs, indent=2)}

PAGE TEXT (for context):
{page_text[:3000]}

READABLE CELLS (refer to these ONLY by index; anchor_text must be copied exactly from a listed "same row as" entry):
{render_value_inventory_for_prompt(value_inventory)}
"""
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _OUTPUT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw_text = response.choices[0].message.content
    return _extract_json(raw_text)


def _xpath_literal(text: str) -> str:
    """XPath 1.0 has no escape character for quotes inside a literal, so
    a label containing a single quote needs the concat() workaround."""
    if "'" not in text:
        return f"'{text}'"
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def build_output_field(
    *, name: str, description: str, sensitive: bool,
    entry: ValueEntry, anchor_text: str,
) -> OutputField:
    if entry.column_position is None:
        raise ValueError(
            f"Output '{name}' points at a value with no table-row context "
            "— the ancestor::tr[1] pattern this recorder uses doesn't apply "
            "outside table layouts. This is a documented limitation, not a bug."
        )
    if anchor_text not in entry.row_texts:
        raise ValueError(
            f"Output '{name}': anchor_text {anchor_text!r} was not actually "
            f"among entry {entry.index}'s row-mates {entry.row_texts!r} — "
            "refusing to build an unverified locator."
        )

    xpath = f"//*[normalize-space(text())={_xpath_literal(anchor_text)}]/ancestor::tr[1]//td[{entry.column_position}]"
    return OutputField(
        name=name,
        type=ParamType.STRING,
        description=description,
        sensitive=sensitive,
        extract_from=ElementTarget(
            locators=[Locator(strategy=LocatorStrategy.XPATH, value=xpath)],
            frame=entry.frame_name,
            description=f"Value in the same row as '{anchor_text}'",
        ),
    )
