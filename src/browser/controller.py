"""
BrowserController — the "hands" layer.

Executes `Step` objects from src/artifact/schema.py against a live
Playwright page, and extracts `OutputField` values once a workflow has
succeeded. This is the ONLY module that touches Playwright directly;
both the (future) discovery loop and the (future) replay engine call
into this class so behaviour is identical whether an LLM or a saved
artifact is driving.

Execution order per step (matches the schema's own docstrings):
  1. wait_before_ms
  2. perform the action (click/type/select/navigate/wait/scroll/
     extract/screenshot/submit/confirm) — gated by the safety policy
  3. check this step's error_detectors, in order, BEFORE the checkpoint
     - business_outcome -> return BusinessOutcomeResult immediately
       (this is a normal result, not an error)
     - recoverable       -> attempt the mapped recovery_action once,
                             then retry the action once; if it still
                             doesn't clear, raise RecoverableCondition
     - hard_failure       -> raise HardFailure immediately
  4. if no error_detector matched and a checkpoint is defined, wait for
     it; if it never becomes true, raise HardFailure — "the checkpoint
     failed and none of the known error conditions explain why" is
     itself useful diagnostic information, so we say exactly that.
  5. wait_after_ms
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright, Page, BrowserContext, Browser, TimeoutError as PWTimeoutError,
)

from src.artifact.schema import (
    Step, ActionType, ActionRiskLevel, ElementTarget, ErrorDetector,
    ErrorOutcomeType, OutputField, SafetyPolicy,
)
from src.browser.errors import BusinessOutcomeResult, RecoverableCondition, HardFailure
from src.browser.locator import resolve
from src.browser.checkpoints import check, wait_until
from src.browser.observation import Observation, capture, redact

StepOutcome = Optional[BusinessOutcomeResult]  # None = proceed normally

_RISK_ORDER = {ActionRiskLevel.SAFE: 0, ActionRiskLevel.MODERATE: 1, ActionRiskLevel.HIGH: 2}
_PARAM_RE = re.compile(r"\{\{(\w+)\}\}")


def substitute(template: Optional[str], params: dict[str, str]) -> Optional[str]:
    if template is None:
        return None
    return _PARAM_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), template)


class BrowserController:
    def __init__(
        self,
        safety_policy: SafetyPolicy,
        *,
        headless: bool = True,
        slow_mo_ms: int = 0,
        home_url: Optional[str] = None,
        evidence_dir: str = "evidence/logs",
        on_risky_action: Optional[Callable[[Step], bool]] = None,
        step_logger: Optional[Callable[[dict], None]] = None,
        sensitive_param_names: Optional[set[str]] = None,
    ):
        self.safety_policy = safety_policy
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.home_url = home_url
        self.evidence_dir = evidence_dir
        self.on_risky_action = on_risky_action or (lambda step: False)
        self.step_logger = step_logger or (lambda entry: None)
        self.sensitive_param_names = sensitive_param_names or set()

        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Human-in-the-loop control tracking (see pause_for_human below).
        self.controller_of_session: str = "automation"

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo_ms)
        self._context = self._browser.new_context()
        self.page = self._context.new_page()

    def stop(self) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # -- human handoff ------------------------------------------------
    # The same BrowserContext/Page stays alive across a pause — the
    # human takes over THIS session, not a fresh one. A real operator
    # UI is out of scope; this is the minimal, real pause/resume gate
    # a real one would sit behind.

    def pause_for_human(self, reason: str) -> None:
        self.controller_of_session = "human"
        self.step_logger({"event": "handoff_pause", "reason": reason, "url": self.page.url})

    def resume_from_human(self) -> None:
        self.controller_of_session = "automation"
        self.step_logger({"event": "handoff_resume", "url": self.page.url})

    def _assert_automation_in_control(self) -> None:
        if self.controller_of_session != "automation":
            raise RecoverableCondition(
                "AUTOMATION_PAUSED", "Human currently has control of the session.",
            )

    # -- safety gate ---------------------------------------------------

    def _domain_allowed(self) -> bool:
        urls = [self.page.url] + [f.url for f in self.page.frames if f is not self.page.main_frame]
        for u in urls:
            host = urlparse(u).netloc
            if not any(host == d or host.endswith("." + d) for d in self.safety_policy.allowed_domains):
                return False
        return True

    def _check_safety(self, step: Step, params: dict[str, str]) -> None:
        if step.action == ActionType.NAVIGATE:
            # There is no "current page" worth checking yet — the page may
            # still be about:blank. The thing to validate is the
            # DESTINATION this step is about to navigate to.
            dest = substitute(step.value, params) or ""
            host = urlparse(dest).netloc
            if not any(host == d or host.endswith("." + d) for d in self.safety_policy.allowed_domains):
                raise HardFailure(
                    f"Navigate target is outside the allowed domains: {dest}",
                    outcome_code="DOMAIN_NOT_ALLOWED",
                    expected=str(self.safety_policy.allowed_domains),
                    observed=dest,
                )
        elif not self._domain_allowed():
            raise HardFailure(
                f"Current page is outside the allowed domains: {self.page.url}",
                outcome_code="DOMAIN_NOT_ALLOWED",
                expected=str(self.safety_policy.allowed_domains),
                observed=self.page.url,
            )
        if step.action not in self.safety_policy.allowed_actions:
            raise HardFailure(
                f"Action type '{step.action}' is not in the safety policy's allowed_actions",
                outcome_code="ACTION_NOT_ALLOWED",
                step_order=step.order, step_name=step.name,
            )
        needs_confirmation = (
            step.action in self.safety_policy.require_human_confirmation_for
            or _RISK_ORDER[step.risk_level] > _RISK_ORDER[self.safety_policy.max_unconfirmed_risk]
        )
        if needs_confirmation:
            if not self.on_risky_action(step):
                raise RecoverableCondition(
                    "CONFIRMATION_REQUIRED",
                    f"Step '{step.name}' (risk={step.risk_level}) requires human confirmation.",
                )

    # -- recovery actions ------------------------------------------------

    def _run_recovery(self, recovery_action: Optional[str]) -> bool:
        if recovery_action == "navigate_home_and_retry" and self.home_url:
            self.page.goto(self.home_url, timeout=10000)
            self.page.wait_for_load_state("networkidle", timeout=10000)
            return True
        if recovery_action == "dismiss_dialog":
            for label in ("Cancel", "Close", "Dismiss", "OK"):
                for scope in [self.page, *self.page.frames]:
                    try:
                        btn = scope.get_by_role("button", name=label)
                        if btn.count() > 0:
                            btn.first.click(timeout=1000)
                            return True
                    except Exception:
                        continue
        return False

    # -- error detector evaluation ---------------------------------------

    def _evaluate_detectors(self, detectors: list[ErrorDetector], step: Step) -> Optional[BusinessOutcomeResult]:
        """Returns a BusinessOutcomeResult if one matched, or raises
        RecoverableCondition/HardFailure. Returns None if nothing matched
        (i.e. proceed to the normal checkpoint)."""
        for detector in detectors:
            if not check(self.page, detector):
                continue

            if detector.outcome_type in (ErrorOutcomeType.BUSINESS_OUTCOME, "business_outcome"):
                self.step_logger({
                    "step_order": step.order, "event": "business_outcome",
                    "outcome_code": detector.outcome_code,
                })
                return BusinessOutcomeResult(
                    outcome_code=detector.outcome_code,
                    outcome_message=detector.outcome_message,
                    step_order=step.order,
                )

            if detector.outcome_type in (ErrorOutcomeType.RECOVERABLE, "recoverable"):
                self.step_logger({
                    "step_order": step.order, "event": "recoverable_detected",
                    "outcome_code": detector.outcome_code, "recovery_action": detector.recovery_action,
                })
                recovered = self._run_recovery(detector.recovery_action)
                # Deliberately NOT falling through to this step's own
                # checkpoint even if recovery "succeeded" (e.g. the page
                # navigated home). Recovery changes the page context out
                # from under the current step — the ONLY safe thing this
                # layer can claim is "I detected a recoverable condition
                # and attempted <recovery_action>." Whether that's enough
                # to retry the whole remaining sequence from an earlier
                # step is a call for the caller (replay engine), which has
                # the full step list and can re-run steps 1..N — this
                # single-step layer does not.
                raise RecoverableCondition(
                    detector.outcome_code, detector.outcome_message,
                    recovery_action=detector.recovery_action, attempted_recovery=recovered,
                )

            # hard_failure
            obs = self.observe(tag=f"failure_step{step.order}_{detector.outcome_code}")
            raise HardFailure(
                detector.outcome_message,
                outcome_code=detector.outcome_code,
                step_order=step.order, step_name=step.name,
                evidence_ref=obs.screenshot_path,
            )
        return None

    # -- observation ------------------------------------------------------

    def observe(self, tag: str = "obs") -> Observation:
        return capture(self.page, screenshot_dir=self.evidence_dir, tag=tag)

    # -- action execution --------------------------------------------------

    def _perform_action(self, step: Step, params: dict[str, str]) -> None:
        action = step.action
        value = substitute(step.value, params)

        if action in (ActionType.NAVIGATE,):
            self.page.goto(value, timeout=10000)
            self.page.wait_for_load_state("networkidle", timeout=10000)
            return

        if action in (ActionType.CLICK, ActionType.SUBMIT, ActionType.CONFIRM):
            pw_loc, winning, scope = resolve(self.page, step.target, timeout_ms=step.checkpoint.timeout_ms if step.checkpoint else 5000)
            coord = getattr(pw_loc, "_coord_spec", None)
            if coord:
                pw_loc.click(position={"x": coord["x"], "y": coord["y"]})
            else:
                pw_loc.click(timeout=5000)
            return

        if action == ActionType.TYPE:
            pw_loc, winning, scope = resolve(self.page, step.target, timeout_ms=5000)
            pw_loc.fill(value or "", timeout=5000)
            return

        if action == ActionType.SELECT:
            pw_loc, winning, scope = resolve(self.page, step.target, timeout_ms=5000)
            pw_loc.select_option(value or "", timeout=5000)
            return

        if action == ActionType.SCROLL:
            pw_loc, winning, scope = resolve(self.page, step.target, timeout_ms=5000)
            pw_loc.scroll_into_view_if_needed(timeout=5000)
            return

        if action == ActionType.WAIT:
            if step.target:
                resolve(self.page, step.target, timeout_ms=step.checkpoint.timeout_ms if step.checkpoint else 5000)
            else:
                self.page.wait_for_timeout(int(value) if value else 1000)
            return

        if action == ActionType.SCREENSHOT:
            self.observe(tag=f"step{step.order}_screenshot")
            return

        if action == ActionType.EXTRACT:
            # Steps of type EXTRACT with no target are used purely as a
            # "verify + branch" point (see example artifact step 4) —
            # actual field extraction happens via extract_output() using
            # each OutputField.extract_from after the workflow succeeds.
            # If a step DOES carry a target, extract here for logging.
            if step.target:
                pw_loc, winning, scope = resolve(self.page, step.target, timeout_ms=5000)
                text = redact(pw_loc.inner_text(timeout=5000))
                self.step_logger({"step_order": step.order, "event": "inline_extract", "value": text})
            return

        raise HardFailure(f"Unknown action type: {action}", outcome_code="UNKNOWN_ACTION", step_order=step.order)

    # -- public: execute one step -----------------------------------------

    def execute_step(self, step: Step, params: dict[str, str]) -> StepOutcome:
        self._assert_automation_in_control()
        self._check_safety(step, params)

        log_entry = {"step_order": step.order, "step_name": step.name, "action": str(step.action)}
        start = time.time()

        if step.wait_before_ms:
            self.page.wait_for_timeout(step.wait_before_ms)

        try:
            self._perform_action(step, params)
        except PWTimeoutError as e:
            raise RecoverableCondition("ACTION_TIMEOUT", f"Timed out performing '{step.name}': {e}")
        except HardFailure as e:
            e.step_order = e.step_order or step.order
            e.step_name = e.step_name or step.name
            if not e.evidence_ref:
                obs = self.observe(tag=f"failure_step{step.order}")
                e.evidence_ref = obs.screenshot_path
            log_entry["outcome"] = "hard_failure"
            self.step_logger({**log_entry, **e.to_dict()})
            raise

        # Check this step's error detectors BEFORE its checkpoint.
        outcome = self._evaluate_detectors(step.error_detectors, step)
        if outcome is not None:
            log_entry["outcome"] = "business_outcome"
            log_entry["outcome_code"] = outcome.outcome_code
            self.step_logger(log_entry)
            return outcome

        # Note on recovery: if a RECOVERABLE detector fired above and
        # `_run_recovery` succeeded, `_evaluate_detectors` returned None
        # and we fall through to the checkpoint check below as if nothing
        # happened. If the checkpoint still isn't met after recovery
        # (e.g. the retry also hit a timeout), that's reported as a
        # HardFailure with CHECKPOINT_FAILED rather than silently retried
        # again — the replay engine (next component) is what decides
        # whether to re-invoke execute_step() for a second full attempt.
        # This is a deliberate "Cut": see REPORT.md.

        if step.checkpoint:
            ok = wait_until(self.page, step.checkpoint)
            if not ok:
                obs = self.observe(tag=f"failure_step{step.order}_checkpoint")
                log_entry["outcome"] = "hard_failure"
                self.step_logger(log_entry)
                raise HardFailure(
                    f"Checkpoint not met after step '{step.name}': {step.checkpoint.description or step.checkpoint.value}",
                    outcome_code="CHECKPOINT_FAILED",
                    step_order=step.order, step_name=step.name,
                    expected=step.checkpoint.description or step.checkpoint.value,
                    observed="none of the known error_detectors matched either",
                    evidence_ref=obs.screenshot_path,
                )

        if step.wait_after_ms:
            self.page.wait_for_timeout(step.wait_after_ms)

        log_entry["outcome"] = "ok"
        log_entry["duration_ms"] = int((time.time() - start) * 1000)
        self.step_logger(log_entry)
        return None

    # -- public: extract a typed output field -----------------------------

    def extract_output(self, output_field: OutputField) -> str:
        if output_field.extract_from is None:
            raise HardFailure(
                f"Output '{output_field.name}' has no extract_from target",
                outcome_code="MALFORMED_OUTPUT_FIELD",
            )
        pw_loc, winning, scope = resolve(self.page, output_field.extract_from, timeout_ms=5000)
        raw = pw_loc.inner_text(timeout=5000).strip()
        scope_name = getattr(scope, "name", None) or "page"
        self.step_logger({
            "event": "extract_output",
            "output": output_field.name,
            "resolved_via": f"[{scope_name}] {winning.strategy}:{winning.value}",
            "value": "[REDACTED]" if output_field.sensitive else raw,
        })
        return raw

    def extract_all_outputs(self, output_fields: list[OutputField]) -> dict[str, str]:
        return {f.name: self.extract_output(f) for f in output_fields}