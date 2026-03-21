"""
Result & error types for the browser control layer.

These map 1:1 onto `ErrorOutcomeType` in src/artifact/schema.py:

    ErrorOutcomeType.BUSINESS_OUTCOME -> BusinessOutcomeResult (data, not raised)
    ErrorOutcomeType.RECOVERABLE      -> RecoverableCondition (raised, caller may retry)
    ErrorOutcomeType.HARD_FAILURE     -> HardFailure (raised, run stops)

Why a dataclass (not an exception) for BusinessOutcomeResult:
A business outcome is a normal return value the caller has to branch on,
not something exceptional. Making it a raised exception would tempt the
replay engine to catch-and-log it next to real errors, which is exactly
the conflation the assignment calls out as "the most common design
mistake." By making it a return type, `execute_step()`'s return type is
`StepOutcome = BusinessOutcomeResult | dict[str, str] | None`, and the
type checker forces every caller to handle the business-outcome branch
explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BusinessOutcomeResult:
    """A named, expected non-happy-path result. Returned, never raised."""
    outcome_code: str
    outcome_message: str
    step_order: Optional[int] = None
    fields: dict[str, Any] = field(default_factory=dict)


class RecoverableCondition(Exception):
    """
    Raised when a recoverable condition was detected but auto-recovery
    either isn't defined for it or didn't resolve the condition after
    retrying. The replay engine decides whether to retry the whole step
    again, skip, or escalate to a human.
    """
    def __init__(self, outcome_code: str, outcome_message: str, *,
                 recovery_action: Optional[str] = None, attempted_recovery: bool = False):
        self.outcome_code = outcome_code
        self.outcome_message = outcome_message
        self.recovery_action = recovery_action
        self.attempted_recovery = attempted_recovery
        super().__init__(f"[{outcome_code}] {outcome_message}")


class HardFailure(Exception):
    """
    Unrecoverable. Ends the run. Carries everything a human needs to
    debug without re-running: which step, what we expected, what we
    actually observed, and a reference to captured evidence.
    """
    def __init__(self, outcome_message: str, *,
                 outcome_code: str = "HARD_FAILURE",
                 step_order: Optional[int] = None,
                 step_name: Optional[str] = None,
                 expected: Optional[str] = None,
                 observed: Optional[str] = None,
                 evidence_ref: Optional[str] = None):
        self.outcome_code = outcome_code
        self.outcome_message = outcome_message
        self.step_order = step_order
        self.step_name = step_name
        self.expected = expected
        self.observed = observed
        self.evidence_ref = evidence_ref
        super().__init__(outcome_message)

    def to_dict(self) -> dict:
        return {
            "outcome_code": self.outcome_code,
            "outcome_message": self.outcome_message,
            "step_order": self.step_order,
            "step_name": self.step_name,
            "expected": self.expected,
            "observed": self.observed,
            "evidence_ref": self.evidence_ref,
        }
