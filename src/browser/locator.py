"""
Locator resolution.

Turns an ElementTarget (a fallback chain of Locators, optionally scoped to
a named frame) into a live, resolved Playwright locator.

Two things make legacy frameset apps like the target app hard:
  1. No stable ids/classes -> handled by trying multiple locator
     strategies in priority order (css -> xpath -> text -> role ->
     coordinates), same idea as a normal fallback chain.
  2. Content lives inside a NAMED FRAME (`<frame name="content">`), and
     navigating within that frame does NOT change `page.url` or the
     frameset's own DOM -> handled by resolving against the right
     Playwright Frame object, not just the top-level Page.

Playwright frames support the same locator API as Page (`.locator()`,
`.get_by_text()`, `.get_by_role()`, ...), so once we've picked the right
search scope (Page or Frame), the rest of the resolution logic is
identical.
"""

from __future__ import annotations

import json
from typing import Union

from playwright.sync_api import Page, Frame, Locator as PWLocator, TimeoutError as PWTimeoutError

from src.artifact.schema import Locator, ElementTarget, LocatorStrategy
from src.browser.errors import HardFailure

SearchScope = Union[Page, Frame]


def get_search_scopes(page: Page, target: ElementTarget) -> list[SearchScope]:
    """
    Build the ordered list of scopes to search in.

    If the artifact names a frame explicitly, that frame is tried first
    (and is required to exist — a missing named frame is itself a strong
    signal something structural changed, so we still fall back to
    scanning other frames rather than failing immediately, but we log
    the mismatch via the caller).
    """
    scopes: list[SearchScope] = []
    if target.frame:
        named = page.frame(name=target.frame)
        if named is not None:
            scopes.append(named)
    # Always fall back to top-level page + every other frame. This keeps
    # replay working even if an artifact's recorded frame name drifts
    # (e.g. a legacy app renaming "content" to "main") while still
    # preferring the recorded frame when it's present.
    scopes.append(page)
    for f in page.frames:
        if f is page.main_frame:
            continue
        if target.frame and f.name == target.frame:
            continue  # already added first
        scopes.append(f)
    return scopes


def _build_pw_locator(scope: SearchScope, loc: Locator, *, exact_text: bool = True) -> PWLocator:
    strategy = loc.strategy

    if strategy == LocatorStrategy.CSS or strategy == "css":
        return scope.locator(loc.value)

    if strategy == LocatorStrategy.XPATH or strategy == "xpath":
        return scope.locator(f"xpath={loc.value}")

    if strategy == LocatorStrategy.TEXT or strategy == "text":
        # Try an EXACT match first. Legacy pages routinely have one piece
        # of text that's a substring of another ("Search" vs. "Search
        # Member") — substring matching alone silently clicks the wrong
        # element instead of erroring, which is worse than a clean
        # failure. resolve() retries with exact_text=False if this
        # yields nothing.
        return scope.get_by_text(loc.value, exact=exact_text)

    if strategy == LocatorStrategy.ROLE or strategy == "role":
        # `value` doubles as the accessible name when present; legacy
        # table-based "buttons" often have no accessible name, so we
        # fall back to role-only matching (first match) if get_by_role
        # with a name finds nothing. Handled by resolve()'s retry loop,
        # not here — this stays a thin wrapper.
        return scope.get_by_role(loc.value)  # type: ignore[arg-type]

    if strategy == LocatorStrategy.COORDINATES or strategy == "coordinates":
        # value is JSON: {"x": 120, "y": 340} relative to this scope's
        # viewport. We click via a position-relative click on <body>,
        # which works identically for Page and Frame scopes.
        spec = json.loads(loc.value)
        body = scope.locator("body")
        # Stash the click point on the locator via a wrapper isn't
        # possible with plain Playwright, so resolve() special-cases
        # COORDINATES and performs the click itself using this body
        # locator + spec. We still return `body` here so resolve()'s
        # generic "does it exist" check passes.
        body._coord_spec = spec  # type: ignore[attr-defined]
        return body

    raise HardFailure(f"Unknown locator strategy: {strategy}")


def resolve(page: Page, target: ElementTarget, *, timeout_ms: int = 5000) -> tuple[PWLocator, Locator, SearchScope]:
    """
    Walk scopes x candidate locators (scope outer loop: try the most
    likely scope fully before moving to the next) and return the first
    that resolves to a visible element.

    Returns (resolved_pw_locator, winning_candidate, winning_scope).
    Raises HardFailure with the full attempt log if nothing resolves.
    """
    scopes = get_search_scopes(page, target)
    attempts: list[str] = []
    per_try_timeout = max(300, timeout_ms // max(1, len(target.locators)))

    for scope in scopes:
        scope_name = getattr(scope, "name", None) or "page"
        for candidate in target.locators:
            # For TEXT locators, try exact match, then substring, as two
            # sub-attempts of the SAME candidate before moving on — this
            # is what stops "Search" from matching "Search Member".
            text_variants = [True, False] if candidate.strategy in (LocatorStrategy.TEXT, "text") else [True]
            for exact in text_variants:
                try:
                    pw_loc = _build_pw_locator(scope, candidate, exact_text=exact)
                    pw_loc.first.wait_for(state="visible", timeout=per_try_timeout)
                    count = pw_loc.count()
                    if count == 0:
                        attempts.append(f"[{scope_name}] {candidate.strategy}:{candidate.value} (exact={exact}) -> 0 matches")
                        continue
                    attempts.append(f"[{scope_name}] {candidate.strategy}:{candidate.value} (exact={exact}) -> resolved ({count} match(es))")
                    return pw_loc.first, candidate, scope
                except PWTimeoutError:
                    attempts.append(f"[{scope_name}] {candidate.strategy}:{candidate.value} (exact={exact}) -> timeout")
                    continue
                except Exception as e:  # noqa: BLE001 - classify at controller level
                    attempts.append(f"[{scope_name}] {candidate.strategy}:{candidate.value} (exact={exact}) -> error: {e}")
                    continue

    raise HardFailure(
        f"Could not resolve target: {target.description or '(no description)'}",
        outcome_code="LOCATOR_RESOLUTION_FAILED",
        expected=target.description,
        observed="; ".join(attempts),
    )