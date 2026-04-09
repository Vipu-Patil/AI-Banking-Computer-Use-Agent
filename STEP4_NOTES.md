# Step 4 — Playwright Browser Control: what was added

```
src/browser/
├── errors.py         # BusinessOutcomeResult / RecoverableCondition / HardFailure
├── locator.py         # resolves ElementTarget fallback chains, frame-aware
├── checkpoints.py     # evaluates Checkpoint/ErrorDetector conditions across page+frames
├── observation.py     # frame-aware screenshot + text capture, PII redaction
└── controller.py      # BrowserController — executes Step objects end to end

demo_lookup_balance.py   # runs the REAL lookup_member_balance.json artifact
                         # through the controller against the real target app
```

## Why frame-aware, specifically

Your target app uses a real `<frameset>` (`/nav` + `/home` in separate
named frames). Two consequences that would otherwise silently break
replay:

1. Navigating inside the `content` frame does NOT change `page.url` —
   so `url_contains` / `url_matches` checkpoints check every frame's URL,
   not just the top-level page's.
2. An `ElementTarget.frame` name might drift between app versions/tenants.
   `locator.py` tries the named frame first, then falls back to scanning
   the top page and every other frame — same "fallback chain" philosophy
   as locator strategies, just one level up.

## How the 3-tier error taxonomy actually executes

`BrowserController.execute_step()` checks each step's `error_detectors`
**before** its `checkpoint`, exactly as your schema's docstring specifies:

- `business_outcome` → returns `BusinessOutcomeResult` (data, never raised)
  — nothing upstream can accidentally `except Exception` and swallow it.
- `recoverable` → runs the mapped `recovery_action` once
  (`navigate_home_and_retry`, `dismiss_dialog` are implemented; more are
  trivial to add — see `_run_recovery`), then falls through to the normal
  checkpoint check. If recovery isn't defined or doesn't clear the
  condition, raises `RecoverableCondition`.
- `hard_failure` → raises `HardFailure` immediately, screenshot attached.
- If NO detector matches but the checkpoint still isn't met, that's
  reported as `CHECKPOINT_FAILED` with the explicit note that no known
  error condition explains it — real signal for "the schema doesn't
  cover this case yet," not a silent hang.

## Safety enforcement (already wired to your SafetyPolicy)

Every `execute_step()` call checks, in order: current page/frame domains
against `allowed_domains` → action type against `allowed_actions` →
whether `risk_level` exceeds `max_unconfirmed_risk` OR the action is in
`require_human_confirmation_for` (defaults to `submit`/`confirm`) →
if so, calls `on_risky_action(step)` and blocks unless it returns True.

## PII

`OutputField.sensitive` and `InputParam` sensitivity are respected in
logs: `extract_output()` logs `[REDACTED]` for sensitive fields while
still returning the real value to the caller (the caller needs it; logs
don't). `observation.py`'s `redact()` additionally scrubs SSNs, emails,
and phone numbers from anything captured for evidence.

## Validated against your actual files (not synthetic examples)

- Both `lookup_member_balance.json` and `open_sub_account.json` still
  validate against `schema.py` unchanged.
- Confirmed via Flask test client that the exact strings your
  `error_detectors` look for ("Member Information", "No Member Found",
  "Session Expired") really do appear in `target-app`'s rendered HTML
  for member ids `10001`, `00000`, `99999`.
- All modules import cleanly against the real `playwright` pip package.

## Couldn't do in this sandbox

Launching real Chromium — Playwright's browser-binary CDN isn't in this
environment's network allowlist. Run this locally to see it drive the
actual browser:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python target-app/app.py            # terminal 1
python demo_lookup_balance.py       # terminal 2
```

Expected: member `10001` → SUCCESS with a `savings_balance` output;
member `00000` → BUSINESS OUTCOME `MEMBER_NOT_FOUND`; member `99999` →
either a clean recovery (if `navigate_home_and_retry` clears it) or a
reported `RecoverableCondition` if it doesn't — never a crash either way.

## Deliberate cuts (see full list in REPORT.md later)

- Retry-after-recovery only happens once, inside a single `execute_step`
  call. A second consecutive recoverable hit on the same step surfaces
  as `RecoverableCondition` for the (future) replay engine to decide on
  — full retry-budget policy belongs at the artifact/run level, not here.
- `coordinates` locator strategy is implemented but untested against a
  real canvas/image element (none exist in this target app).

## Next: Step 5 — Discovery agent (LLM loop)

Will call `controller.observe()` each turn, ask an LLM (with the goal +
observation) for the next `Step`-shaped action, call
`controller.execute_step()`, and on success serialize the recorded steps
into a new `Artifact` — reusing every module built here unchanged.
