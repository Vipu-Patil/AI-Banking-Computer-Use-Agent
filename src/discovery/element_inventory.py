"""
Element inventory.

The single biggest failure mode for an LLM driving a browser is
hallucinating a selector that doesn't exist, or that exists but isn't
the element it thinks it is. The fix: don't ask the LLM to invent a
CSS/XPath selector from scratch. Instead, WE enumerate every actually-
interactive element on the page ourselves, assign each one a real
Locator (reusing the exact same schema as artifacts), and hand the LLM
a numbered menu to choose from. It picks an index; we already know that
index resolves to a real element, because we just found it by looking
at the live DOM.

This is also why discovery and replay end up producing compatible
artifacts: an inventory entry already IS a Locator from
src/artifact/schema.py, so recording a discovered action is just
"attach the Locator the LLM picked" — no format translation needed
later in Step 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Page, Frame

from src.artifact.schema import Locator, LocatorStrategy

# Elements worth offering to the LLM as click/type/select targets.
# Deliberately includes onclick'd <td>/<div>s, since legacy UIs fake
# buttons out of arbitrary elements (see target-app's own markup).
_INTERACTIVE_SELECTOR = (
    "input, select, textarea, button, a[href], "
    "[onclick], [role='button'], [role='link']"
)

_MAX_ELEMENTS = 60  # keep the prompt small; legacy pages can be huge


@dataclass
class InventoryEntry:
    index: int
    tag: str
    visible_text: str
    input_type: str | None       # for <input>, e.g. "text", "submit"
    locator: Locator             # a single, real, already-verified locator
    frame_name: str | None       # None = top-level page


def _best_locator_for(el, frame_name: str | None) -> tuple[Locator, str, str] | None:
    """Pick the most stable single locator for one already-found element.
    Returns (locator, tag, visible_text) so build_inventory never needs
    a second, redundant evaluate()/inner_text() call for display purposes
    — that redundancy was silently swallowing real errors before and
    showing "<unknown>" for elements that actually resolved fine."""
    try:
        name = el.get_attribute("name")
        elem_id = el.get_attribute("id")
        tag = el.evaluate("e => e.tagName.toLowerCase()")
    except Exception as e:
        print(f"  [inventory] skipping element: could not read tag/attributes: {e}")
        return None

    try:
        text = el.inner_text().strip()
    except Exception as e:
        print(f"  [inventory] warning: could not read text for a <{tag}>: {e}")
        text = ""

    if elem_id:
        return Locator(strategy=LocatorStrategy.CSS, value=f"#{elem_id}"), tag, text
    if name:
        return Locator(strategy=LocatorStrategy.CSS, value=f"{tag}[name='{name}']"), tag, text
    if text and len(text) <= 60:
        return Locator(strategy=LocatorStrategy.TEXT, value=text), tag, text

    # Last resort: nothing stable found. Skip rather than offer a
    # locator we know is fragile — better the LLM sees fewer, reliable
    # options than more, unreliable ones.
    return None


def build_inventory(page: Page) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    scopes: list[tuple[Page | Frame, str | None]] = [(page, None)]
    for f in page.frames:
        if f is not page.main_frame:
            scopes.append((f, f.name or None))

    for scope, frame_name in scopes:
        try:
            handles = scope.query_selector_all(_INTERACTIVE_SELECTOR)
        except Exception as e:
            print(f"  [inventory] could not query scope '{frame_name or 'page'}': {e}")
            continue

        print(f"  [inventory] scope '{frame_name or 'page'}': {len(handles)} raw matches "
              f"before visibility/locator filtering")

        for el in handles:
            if len(entries) >= _MAX_ELEMENTS:
                break
            try:
                if not el.is_visible():
                    continue
            except Exception as e:
                print(f"  [inventory] skipping element: is_visible() failed: {e}")
                continue

            result = _best_locator_for(el, frame_name)
            if result is None:
                continue
            loc, tag, text = result

            input_type = None
            if tag == "input":
                try:
                    input_type = el.get_attribute("type")
                except Exception:
                    pass

            entries.append(InventoryEntry(
                index=len(entries),
                tag=tag,
                visible_text=text[:80],
                input_type=input_type,
                locator=loc,
                frame_name=frame_name,
            ))

    return entries


def render_for_prompt(entries: list[InventoryEntry]) -> str:
    """Turn the inventory into a compact, LLM-readable numbered list."""
    lines = []
    for e in entries:
        desc = f"[{e.index}] <{e.tag}"
        if e.input_type:
            desc += f" type={e.input_type}"
        desc += f"> \"{e.visible_text}\""
        if e.frame_name:
            desc += f" (frame: {e.frame_name})"
        lines.append(desc)
    return "\n".join(lines) if lines else "(no interactive elements found)"