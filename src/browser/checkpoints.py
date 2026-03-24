"""
Checkpoint evaluation.

`Checkpoint` and `ErrorDetector` share the same condition shape
(CheckpointType + value + optional target), so both are evaluated by the
same function here. The difference between them is purely in how the
*caller* interprets a match: a Checkpoint match means "proceed", an
ErrorDetector match means "branch to business_outcome / recoverable /
hard_failure" (see controller.py).

Frame handling for TEXT_PRESENT/TEXT_ABSENT/URL_MATCHES/URL_CONTAINS
without an explicit target: because the target app is a frameset, the
"page" the artifact author means is usually the content frame, not the
outer frameset shell (which never changes: it's just two <frame> tags).
So when no target is given we check text/url across the TOP page AND
every child frame, and match on ANY of them - this is deliberately
permissive so artifacts authored before a frame rename still work.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page

from src.artifact.schema import Checkpoint, ErrorDetector, CheckpointType
from src.browser.locator import resolve, get_search_scopes
from src.browser.errors import HardFailure


def _all_texts(page: Page) -> list[str]:
    texts = []
    for frame in [page, *[f for f in page.frames if f is not page.main_frame]]:
        try:
            texts.append(frame.locator("body").inner_text(timeout=1000))
        except Exception:
            continue
    return texts


def _all_urls(page: Page) -> list[str]:
    return [page.url] + [f.url for f in page.frames if f is not page.main_frame]


def check(page: Page, condition: Checkpoint | ErrorDetector, *, timeout_ms: int | None = None) -> bool:
    """Returns True if the condition currently holds. Never raises for a
    'no match' outcome - only raises HardFailure for a genuinely broken
    element-target lookup when the check TYPE requires resolving one."""
    ctype = condition.type
    timeout = timeout_ms if timeout_ms is not None else getattr(condition, "timeout_ms", 5000)

    if ctype in (CheckpointType.TEXT_PRESENT, "text_present"):
        needle = condition.value.lower()
        return any(needle in t.lower() for t in _all_texts(page))

    if ctype in (CheckpointType.TEXT_ABSENT, "text_absent"):
        needle = condition.value.lower()
        return all(needle not in t.lower() for t in _all_texts(page))

    if ctype in (CheckpointType.URL_CONTAINS, "url_contains"):
        return any(condition.value in u for u in _all_urls(page))

    if ctype in (CheckpointType.URL_MATCHES, "url_matches"):
        pattern = re.compile(condition.value)
        return any(pattern.search(u) for u in _all_urls(page))

    if ctype in (CheckpointType.ELEMENT_VISIBLE, "element_visible"):
        if condition.target is None:
            raise HardFailure("element_visible checkpoint requires a target", outcome_code="MALFORMED_CHECKPOINT")
        try:
            resolve(page, condition.target, timeout_ms=timeout)
            return True
        except HardFailure:
            return False

    if ctype in (CheckpointType.ELEMENT_HIDDEN, "element_hidden"):
        if condition.target is None:
            raise HardFailure("element_hidden checkpoint requires a target", outcome_code="MALFORMED_CHECKPOINT")
        try:
            resolve(page, condition.target, timeout_ms=min(timeout, 1500))
            return False  # it resolved -> it's visible -> hidden-check fails
        except HardFailure:
            return True

    raise HardFailure(f"Unknown checkpoint type: {ctype}", outcome_code="MALFORMED_CHECKPOINT")


def wait_until(page: Page, condition: Checkpoint, *, poll_ms: int = 250) -> bool:
    """Poll `check()` until it's true or the checkpoint's own timeout_ms
    elapses. Used for checkpoints that need the page to catch up (e.g.
    right after a click that triggers navigation in a frame)."""
    import time
    deadline = time.time() + (condition.timeout_ms / 1000)
    while time.time() < deadline:
        if check(page, condition, timeout_ms=300):
            return True
        page.wait_for_timeout(poll_ms)
    return check(page, condition, timeout_ms=300)
