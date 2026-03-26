"""
Observation capture — frame-aware.

Same purpose as before (screenshot + readable text dump for evidence and
for a future discovery LLM), but walks every frame, not just the top page,
since the target app's real content lives in a named frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page

_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***-**-****"),
    (re.compile(r"\b\d{4}\b(?=\s*$)"), "[REDACTED_LAST4]"),  # bare trailing 4-digit ids (ssn_last4 etc.)
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}"), "[REDACTED_PHONE]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class Observation:
    url: str
    frame_urls: dict[str, str]
    text_by_frame: dict[str, str]
    screenshot_path: str | None = None


def capture(page: Page, *, screenshot_dir: str | None = None, tag: str = "obs") -> Observation:
    frame_urls, text_by_frame = {}, {}
    for f in page.frames:
        label = f.name or ("main" if f is page.main_frame else f"frame_{id(f)}")
        frame_urls[label] = f.url
        try:
            text_by_frame[label] = redact(f.locator("body").inner_text(timeout=1000))[:4000]
        except Exception:
            text_by_frame[label] = ""

    screenshot_path = None
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        screenshot_path = str(Path(screenshot_dir) / f"{tag}.png")
        page.screenshot(path=screenshot_path)

    return Observation(
        url=page.url,
        frame_urls=frame_urls,
        text_by_frame=text_by_frame,
        screenshot_path=screenshot_path,
    )
