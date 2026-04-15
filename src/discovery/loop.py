"""
The discovery loop.

Ties together everything built so far:
  element_inventory.build_inventory()  -> grounded, real locators
  llm_client.decide_next_action()      -> one action, by index
  browser.controller.BrowserController -> actually performs it

Deliberately does NOT attach checkpoints or error_detectors to the Step
objects it builds — those require structured, verified conditions (a
specific CSS selector for "not found", a specific URL pattern for
success), which discovery hasn't earned yet on a first pass. Instead,
the LLM verifies its own progress by re-observing the page every turn
and reasoning about what it sees (that's what `done`/`success`/`stuck`
are for). Turning a successful trajectory into a hardened Artifact with
real checkpoints and error_detectors is Step 6's job, not this one's.

Stopping conditions (Section 3.1 requires these explicitly):
  - LLM reports done=true            -> normal completion (success OR a
                                         recognized business outcome)
  - LLM reports stuck=true           -> escalates to human, does not guess
  - A risky action needs confirmation -> escalates to human (ties into
                                         the same safety gate from Step 4)
  - MAX_CONSECUTIVE_FAILURES reached -> "dead end": the loop itself is
                                         going nowhere, distinct from...
  - max_steps reached                -> ...taking too long overall
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from src.artifact.schema import Step, ActionType, ActionRiskLevel, ElementTarget
from src.browser.controller import BrowserController
from src.browser.errors import BusinessOutcomeResult, RecoverableCondition, HardFailure
from src.discovery.element_inventory import build_inventory, render_for_prompt, InventoryEntry
from src.discovery.llm_client import decide_next_action

MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class TrajectoryEntry:
    step: Step
    reasoning: str
    outcome: str  # "ok" | "recoverable" | "hard_failure" | "business_outcome"
    detail: str = ""


@dataclass
class DiscoveryResult:
    success: bool
    business_outcome: bool
    stuck: bool
    dead_end: bool
    outcome_note: str
    trajectory: list[TrajectoryEntry]
    stop_reason: str


def _history_summary(trajectory: list[TrajectoryEntry]) -> str:
    lines = []
    for i, entry in enumerate(trajectory, start=1):
        line = f"Step {i}: {entry.step.action} '{entry.step.name}' -> {entry.outcome}"
        if entry.detail:
            line += f" ({entry.detail})"
        lines.append(line)
    return "\n".join(lines)


def _build_target(entry: InventoryEntry) -> ElementTarget:
    return ElementTarget(
        locators=[entry.locator],
        frame=entry.frame_name,
        description=entry.visible_text or f"<{entry.tag}>",
    )


def run_discovery(
    *,
    controller: BrowserController,
    client,
    goal: str,
    entry_url: str,
    input_params: dict[str, str],
    max_steps: int = 20,
    on_turn: Optional[Callable[[dict], None]] = None,
) -> DiscoveryResult:
    on_turn = on_turn or (lambda info: None)
    trajectory: list[TrajectoryEntry] = []
    consecutive_failures = 0

    # Always navigate to the entry URL deterministically first — no need
    # to burn an LLM call asking it to "find" the starting page.
    nav_step = Step(
        order=1, name="Navigate to entry URL", action=ActionType.NAVIGATE,
        value=entry_url, risk_level=ActionRiskLevel.SAFE, error_detectors=[],
    )
    try:
        controller.execute_step(nav_step, input_params)
        trajectory.append(TrajectoryEntry(nav_step, "Start of discovery run", "ok"))
    except (RecoverableCondition, HardFailure) as e:
        return DiscoveryResult(
            False, False, False, True, f"Could not load entry URL: {e}",
            trajectory, "dead_end_at_entry",
        )

    for order in range(2, max_steps + 2):
        obs = controller.observe(tag=f"discovery_step{order}")
        page_text = "\n".join(obs.text_by_frame.values())
        inventory = build_inventory(controller.page)

        decision = decide_next_action(
            client, goal=goal, input_params=input_params, page_text=page_text,
            inventory=inventory, history_summary=_history_summary(trajectory),
        )

        on_turn({
            "order": order,
            "inventory_shown": render_for_prompt(inventory),
            "page_text_shown": page_text[:3000],
            "decision_raw": decision.raw,
        })

        if decision.stuck:
            controller.pause_for_human(f"Discovery agent is stuck: {decision.reasoning}")
            return DiscoveryResult(
                False, False, True, False, decision.reasoning,
                trajectory, "stuck_escalated_to_human",
            )

        if decision.done:
            return DiscoveryResult(
                success=decision.success, business_outcome=decision.success,
                stuck=False, dead_end=False, outcome_note=decision.outcome_note,
                trajectory=trajectory, stop_reason="llm_reported_done",
            )

        act = decision.action
        target: Optional[ElementTarget] = None

        if act.target_index is not None:
            if not (0 <= act.target_index < len(inventory)):
                consecutive_failures += 1
                bad_step = Step(
                    order=order, name="invalid_target_index", action=ActionType.CLICK,
                    risk_level=ActionRiskLevel.SAFE, error_detectors=[],
                )
                trajectory.append(TrajectoryEntry(
                    bad_step, decision.reasoning, "hard_failure",
                    f"LLM referenced target_index {act.target_index}, "
                    f"out of range 0-{len(inventory) - 1}",
                ))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    return DiscoveryResult(
                        False, False, False, True, "Repeated invalid target references",
                        trajectory, "dead_end_repeated_failures",
                    )
                continue
            target = _build_target(inventory[act.target_index])

        step = Step(
            order=order,
            name=f"{act.type} ({target.description if target else act.value})",
            action=ActionType(act.type),
            target=target,
            value=act.value,
            risk_level=ActionRiskLevel(act.risk_level),
            error_detectors=[],
        )

        try:
            outcome = controller.execute_step(step, input_params)
            if isinstance(outcome, BusinessOutcomeResult):
                trajectory.append(TrajectoryEntry(
                    step, decision.reasoning, "business_outcome", outcome.outcome_message,
                ))
                return DiscoveryResult(
                    True, True, False, False, outcome.outcome_message,
                    trajectory, "business_outcome_detected",
                )
            trajectory.append(TrajectoryEntry(
                step, decision.reasoning, "ok", act.checkpoint_hint or "",
            ))
            consecutive_failures = 0

        except RecoverableCondition as e:
            if e.outcome_code == "CONFIRMATION_REQUIRED":
                controller.pause_for_human(f"Risky action needs confirmation: {step.name}")
                return DiscoveryResult(
                    False, False, True, False, e.outcome_message,
                    trajectory, "risky_action_needs_confirmation",
                )
            consecutive_failures += 1
            trajectory.append(TrajectoryEntry(step, decision.reasoning, "recoverable", e.outcome_message))
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return DiscoveryResult(
                    False, False, False, True, "Repeated recoverable failures",
                    trajectory, "dead_end_repeated_failures",
                )

        except HardFailure as e:
            consecutive_failures += 1
            trajectory.append(TrajectoryEntry(step, decision.reasoning, "hard_failure", e.outcome_message))
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return DiscoveryResult(
                    False, False, False, True, "Repeated hard failures",
                    trajectory, "dead_end_repeated_failures",
                )

    return DiscoveryResult(
        False, False, False, False, "Max steps reached without completion",
        trajectory, "max_steps_reached",
    )