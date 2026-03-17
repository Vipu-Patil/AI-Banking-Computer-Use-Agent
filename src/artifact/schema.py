"""
Artifact Schema — The Capability Contract
==========================================

This is the core data model for a recorded, replayable capability.
An artifact represents a complete workflow that an AI agent can invoke
by name with typed parameters, like calling a function.

Design decisions:
1. Pydantic for validation + serialization (JSON in, JSON out, type-safe)
2. Multi-strategy locators with fallback chains (because legacy UIs have
   no stable IDs — we need multiple ways to find an element)
3. Three-tier error taxonomy: business outcomes vs recoverable conditions
   vs hard failures (the assignment's key insight)
4. Explicit input/output contracts so a calling agent knows exactly what
   to provide and what it gets back
5. Versioned so we can track changes and detect drift across tenants
6. Checkpoints after every step to verify we're in the expected state
   before proceeding (don't blindly click and hope)

The artifact is the centerpiece of the system:
- Discovery mode PRODUCES an artifact
- Replay mode CONSUMES an artifact
- A human reviewer READS an artifact to understand what it does
- A calling AI agent INVOKES an artifact like a function
"""

from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime


# =============================================================
# LOCATOR — How we find an element on screen
# =============================================================

class LocatorStrategy(str, Enum):
    """
    How to find a UI element. Ordered by reliability in legacy apps:
    
    - CSS:          Standard CSS selector. Fragile in legacy apps (no IDs, 
                    deep nesting) but fast when available.
    - XPATH:        XPath expression. More powerful than CSS for legacy 
                    table-based layouts.
    - TEXT:         Find by visible text content. Most robust for legacy 
                    apps because text rarely changes even when structure does.
    - ROLE:         Accessibility role + name. Works across web and desktop.
                    More stable than DOM selectors.
    - COORDINATES:  Screenshot-based x,y click. Last resort — works when 
                    nothing else does (e.g., canvas elements, images).
    """
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"
    ROLE = "role"
    COORDINATES = "coordinates"


class Locator(BaseModel):
    """
    A single way to find an element. An element can have multiple locators
    as fallbacks (try CSS first, then text, then coordinates).
    
    Why fallback chains?
    Legacy banking apps often have:
    - No IDs (CSS fails)
    - Dynamic class names (CSS fails)  
    - But consistent visible text ("Search", "Submit" don't change)
    - Stable layout positions (coordinates as last resort)
    
    So we try the most specific strategy first, fall back to broader ones.
    """
    strategy: LocatorStrategy
    value: str = Field(description="The selector/text/role/coordinates value")
    description: str = Field(
        default="",
        description="Human-readable description: 'the Search button in the top form'"
    )


class ElementTarget(BaseModel):
    """
    How to find a specific UI element, with a prioritized chain of locators.
    
    The replay engine tries locators in order. If the first fails,
    it tries the second, etc. This makes replay robust against minor
    UI changes while keeping it deterministic (no LLM guessing).
    
    Example: To find the Search button, we might try:
    1. CSS: "td[onclick*='submit']"  (specific but fragile)
    2. TEXT: "Search"                  (robust, works if text stays the same)
    3. XPATH: "//font[b='Search']/.." (structural fallback)
    """
    locators: list[Locator] = Field(
        min_length=1,
        description="Ordered list of locator strategies. Try first, fall back."
    )
    frame: Optional[str] = Field(
        default=None,
        description="If element is inside a frame/iframe, which frame name. "
                    "Critical for legacy apps that use framesets."
    )
    description: str = Field(
        default="",
        description="Human-readable description of what this element is"
    )


# =============================================================
# ACTIONS — What the agent does at each step
# =============================================================

class ActionType(str, Enum):
    """
    The types of actions the automation can perform.
    Split into SAFE (read-only, reversible) and RISKY (write, irreversible).
    The safety layer uses this classification.
    """
    # Safe actions (read-only or reversible)
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"          # dropdown selection
    NAVIGATE = "navigate"      # go to URL
    WAIT = "wait"              # wait for element/condition
    SCROLL = "scroll"
    
    # Read actions (extracting data)
    EXTRACT = "extract"        # read text from element
    SCREENSHOT = "screenshot"  # capture visual state
    
    # Risky actions (irreversible writes)
    SUBMIT = "submit"          # form submission
    CONFIRM = "confirm"        # confirmation dialog


class ActionRiskLevel(str, Enum):
    """
    Risk classification for safety guardrails.
    
    SAFE:       Read-only, navigation, typing (reversible)
    MODERATE:   Form submissions, data changes (potentially reversible)
    HIGH:       Financial transactions, account changes (irreversible)
    
    The safety layer can block, require confirmation, or flag based on this.
    """
    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"


# =============================================================
# CHECKPOINTS — How we verify each step worked
# =============================================================

class CheckpointType(str, Enum):
    """
    How to verify we reached the expected state after an action.
    
    ELEMENT_VISIBLE:  An element matching a locator is visible on page
    ELEMENT_HIDDEN:   An element is NOT visible (e.g., loading spinner gone)
    TEXT_PRESENT:     Specific text appears somewhere on the page
    TEXT_ABSENT:      Specific text does NOT appear
    URL_MATCHES:      Current URL matches a pattern
    URL_CONTAINS:     Current URL contains a substring
    """
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_HIDDEN = "element_hidden"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    URL_MATCHES = "url_matches"
    URL_CONTAINS = "url_contains"


class Checkpoint(BaseModel):
    """
    A verification condition to assert after a step.
    
    Why checkpoints matter:
    Without them, the replay blindly clicks and types, assuming each
    step worked. With them, we VERIFY the page changed as expected
    before moving on. This catches errors early rather than failing
    five steps later with a confusing error.
    
    Example: After clicking Search, checkpoint:
    - type: TEXT_PRESENT, value: "Member Information"
    This confirms the results page actually loaded.
    """
    type: CheckpointType
    value: str = Field(description="The text/URL/locator to check")
    target: Optional[ElementTarget] = Field(
        default=None,
        description="For element-based checks, which element to verify"
    )
    timeout_ms: int = Field(
        default=5000,
        description="How long to wait for this checkpoint before failing"
    )
    description: str = Field(
        default="",
        description="Human-readable: 'results page has loaded'"
    )


# =============================================================
# ERROR DETECTION — What to look for at runtime
# =============================================================

class ErrorOutcomeType(str, Enum):
    """
    The three-tier error taxonomy from the assignment:
    
    BUSINESS_OUTCOME:   A legitimate result the caller needs to know about.
                        "Member not found" is an answer, not a crash.
                        
    RECOVERABLE:        A transient condition we can handle automatically.
                        Session timeout → navigate home and retry.
                        Loading spinner → wait longer.
                        
    HARD_FAILURE:       Something is genuinely broken. Stop and report.
                        Element missing, page structure changed, app crashed.
    """
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class ErrorDetector(BaseModel):
    """
    A condition to check for at each step that indicates an error or
    exceptional state. These are checked BEFORE the normal checkpoint.
    
    Why check for errors before checking for success?
    Because a "member not found" page won't have the success checkpoint
    ("Member Information" text), and we want to detect it as a business
    outcome rather than a generic "checkpoint failed" error.
    
    Example detectors for our banking app:
    - TEXT_PRESENT "No Member Found" → BUSINESS_OUTCOME
    - TEXT_PRESENT "Session Expired" → RECOVERABLE (go home, retry)
    - TEXT_PRESENT "Access Denied" → BUSINESS_OUTCOME
    - TEXT_PRESENT "Account Frozen" → BUSINESS_OUTCOME
    - TEXT_PRESENT "System Error" → HARD_FAILURE
    """
    type: CheckpointType
    value: str
    target: Optional[ElementTarget] = None
    outcome_type: ErrorOutcomeType
    outcome_code: str = Field(
        description="Machine-readable code: 'MEMBER_NOT_FOUND', 'SESSION_EXPIRED'"
    )
    outcome_message: str = Field(
        description="Human-readable: 'The requested member does not exist'"
    )
    recovery_action: Optional[str] = Field(
        default=None,
        description="For RECOVERABLE: what to do. 'navigate_home_and_retry', 'dismiss_dialog'"
    )


# =============================================================
# STEPS — The individual actions in the workflow
# =============================================================

class Step(BaseModel):
    """
    A single step in the workflow.
    
    Each step has:
    - An action to perform (click, type, extract, etc.)
    - A target element to act on (with fallback locators)
    - A value (what to type, what URL to navigate to)
    - A checkpoint (how to verify the step worked)
    - Error detectors (what to look for that indicates something went wrong)
    - Risk level (for safety classification)
    
    Values can contain parameter placeholders like {{member_id}} that get
    substituted at replay time from the input parameters.
    """
    order: int = Field(description="Step number (1-based)")
    name: str = Field(description="Human-readable step name: 'Enter member ID'")
    description: str = Field(default="", description="What this step does and why")
    
    # What to do
    action: ActionType
    risk_level: ActionRiskLevel = ActionRiskLevel.SAFE
    
    # Where to do it
    target: Optional[ElementTarget] = Field(
        default=None,
        description="The element to act on. None for navigate/wait/screenshot."
    )
    
    # With what value
    value: Optional[str] = Field(
        default=None,
        description="The text to type, URL to navigate to, option to select, "
                    "or data key to extract into. Supports {{param}} placeholders."
    )
    
    # How to verify it worked
    checkpoint: Optional[Checkpoint] = Field(
        default=None,
        description="Condition to verify after this step succeeds"
    )
    
    # What errors to look for
    error_detectors: list[ErrorDetector] = Field(
        default_factory=list,
        description="Conditions that indicate errors or exceptional states. "
                    "Checked BEFORE the success checkpoint."
    )
    
    # Timing
    wait_before_ms: int = Field(
        default=500,
        description="Milliseconds to wait before performing this action. "
                    "Gives the page time to settle."
    )
    wait_after_ms: int = Field(
        default=500,
        description="Milliseconds to wait after performing this action. "
                    "Gives the page time to respond."
    )


# =============================================================
# INPUT/OUTPUT CONTRACT — The capability's interface
# =============================================================

class ParamType(str, Enum):
    """Supported parameter types."""
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    """
    A typed input parameter the caller must provide.
    
    Example: member_id is a required string that the calling AI agent
    supplies when invoking this capability.
    """
    name: str
    type: ParamType
    required: bool = True
    default: Optional[str] = None
    description: str = ""
    sensitive: bool = Field(
        default=False,
        description="If true, this value will be redacted in logs. "
                    "Use for SSNs, account numbers, passwords, etc."
    )


class OutputField(BaseModel):
    """
    A typed output field the capability extracts and returns.
    
    Example: savings_balance is a string extracted from the page
    at the element matching the given locator.
    """
    name: str
    type: ParamType
    description: str = ""
    extract_from: Optional[ElementTarget] = Field(
        default=None,
        description="Which element to read this value from during replay"
    )
    sensitive: bool = Field(
        default=False,
        description="If true, this value will be redacted in logs."
    )


# =============================================================
# SAFETY POLICY — What this capability is allowed to do
# =============================================================

class SafetyPolicy(BaseModel):
    """
    The safety boundaries for this capability.
    
    Defines what domains are allowed, which actions are permitted,
    and the maximum risk level before human confirmation is required.
    """
    allowed_domains: list[str] = Field(
        description="Domains this capability is permitted to interact with. "
                    "e.g., ['localhost:5000', '127.0.0.1:5000']"
    )
    allowed_actions: list[ActionType] = Field(
        default_factory=lambda: list(ActionType),
        description="Which action types are permitted"
    )
    max_unconfirmed_risk: ActionRiskLevel = Field(
        default=ActionRiskLevel.MODERATE,
        description="Highest risk level that can execute without human confirmation. "
                    "Actions above this trigger escalation."
    )
    require_human_confirmation_for: list[ActionType] = Field(
        default_factory=lambda: [ActionType.SUBMIT, ActionType.CONFIRM],
        description="Action types that always need human confirmation during replay"
    )


# =============================================================
# METADATA — Version, provenance, tenant info
# =============================================================

class ArtifactMetadata(BaseModel):
    """
    Metadata about the artifact: when it was recorded, by whom,
    which version, and which tenant/app it targets.
    
    Why tenant info?
    Many banks run the SAME vendor software configured differently.
    An artifact recorded on Tenant A's version of "CU Core" should
    ideally work on Tenant B's version too — but we need to track
    which tenant it was recorded on and detect when another tenant's
    version has drifted.
    """
    version: str = Field(
        default="1.0.0",
        description="Semantic version of this artifact"
    )
    created_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="When this artifact was first recorded"
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="When this artifact was last modified"
    )
    recorded_by: str = Field(
        default="discovery_agent",
        description="Who/what created this artifact"
    )
    
    # Target application info
    app_name: str = Field(
        default="",
        description="Name of the target application: 'CU Core Back Office'"
    )
    app_version: str = Field(
        default="",
        description="Version of the target app when this was recorded"
    )
    
    # Tenant info for multi-tenant reuse
    tenant_id: Optional[str] = Field(
        default=None,
        description="Which tenant/institution this was recorded against. "
                    "None = generic/base artifact."
    )
    base_artifact_id: Optional[str] = Field(
        default=None,
        description="If this is a tenant-specific override, which base "
                    "artifact it derives from."
    )
    
    # Approval status
    status: str = Field(
        default="draft",
        description="Lifecycle state: draft → reviewed → approved → deprecated"
    )
    approval_notes: Optional[str] = None


# =============================================================
# THE ARTIFACT — The complete capability
# =============================================================

class Artifact(BaseModel):
    """
    The complete artifact — a recorded, replayable, agent-invocable capability.
    
    This is the central data structure of the entire system.
    Think of it as a function definition:
    
        function lookup_member_balance(member_id: string) -> {
            member_name: string,
            savings_balance: string
        }
    
    ...except instead of code, the "body" is a list of UI steps
    (click here, type this, read that) with fallback locators,
    checkpoints, and error detectors.
    
    A calling AI agent invokes it like:
        result = replay("lookup_member_balance", {member_id: "10001"})
        # result.outputs = {member_name: "John Smith", savings_balance: "$5,230.45"}
        # result.status = "success"
    
    Or it might get:
        # result.status = "business_outcome"
        # result.outcome_code = "MEMBER_NOT_FOUND"
        # result.outcome_message = "The requested member does not exist"
    """
    # Identity
    id: str = Field(description="Unique identifier: 'lookup_member_balance'")
    name: str = Field(description="Human-readable name: 'Look Up Member Balance'")
    description: str = Field(
        default="",
        description="What this capability does, in plain English"
    )
    
    # Contract
    inputs: list[InputParam] = Field(
        description="What the caller must provide"
    )
    outputs: list[OutputField] = Field(
        description="What the caller gets back on success"
    )
    
    # The workflow
    entry_url: str = Field(
        description="Where to start. Supports {{param}} placeholders. "
                    "e.g., 'http://localhost:5000/member/search'"
    )
    steps: list[Step] = Field(
        description="Ordered list of actions to perform"
    )
    
    # Success condition
    success_condition: Checkpoint = Field(
        description="How to verify the entire workflow succeeded"
    )
    
    # Error handling
    global_error_detectors: list[ErrorDetector] = Field(
        default_factory=list,
        description="Errors to check for at EVERY step (session timeout, "
                    "system error, etc.) — not just at specific steps."
    )
    
    # Safety
    safety_policy: SafetyPolicy
    
    # Metadata
    metadata: ArtifactMetadata = Field(
        default_factory=ArtifactMetadata
    )
