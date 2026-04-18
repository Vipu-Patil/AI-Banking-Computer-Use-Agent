"""
Artifact recorder — Piece 1: action steps.

Converts a successful DiscoveryResult's trajectory into real Artifact
steps. Deliberately does NOT yet populate `outputs` (the typed values a
caller wants back, e.g. "savings_balance") — the discovery loop never
recorded a structured locator for those, only that the LLM eventually
recognized success from the raw page text. Turning "the LLM could read
it" into "a locator that can read it with no LLM at all" is a separate,
harder problem — see the outputs-recorder piece that follows this one.

What this piece DOES give you: a real, valid Artifact whose action
steps (navigate/type/click/...) are ready to replay deterministically,
using the exact locators that already worked during discovery. That's
useful and testable on its own before output-extraction is added.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.artifact.schema import (
    Artifact, ArtifactMetadata, InputParam, Step, SafetyPolicy, Checkpoint,
)
from src.discovery.loop import DiscoveryResult, TrajectoryEntry


def _successful_action_steps(trajectory: list[TrajectoryEntry]) -> list[Step]:
    """Keep only steps that actually executed successfully, renumbered
    sequentially. Failed/retried attempts (invalid index, recoverable
    hiccups the agent worked around) are discovery-time noise, not part
    of the reusable recipe — replay shouldn't repeat a mistake that
    discovery itself already recovered from."""
    kept = []
    for entry in trajectory:
        if entry.outcome not in ("ok",):
            continue
        kept.append(entry.step)

    renumbered = []
    for i, step in enumerate(kept, start=1):
        renumbered.append(step.model_copy(update={"order": i}))
    return renumbered


def record_artifact(
    result: DiscoveryResult,
    *,
    artifact_id: str,
    name: str,
    description: str,
    entry_url: str,
    input_specs: list[InputParam],
    safety_policy: SafetyPolicy,
    success_condition: Checkpoint,
) -> Artifact:
    if not result.success:
        raise ValueError(
            "Refusing to record an artifact from a discovery run that "
            f"didn't succeed (stop_reason={result.stop_reason!r}). Only "
            "successful runs produce a trustworthy recipe."
        )

    steps = _successful_action_steps(result.trajectory)
    now = datetime.now(timezone.utc).isoformat()

    return Artifact(
        id=artifact_id,
        name=name,
        description=description,
        inputs=input_specs,
        outputs=[],  # filled in by the outputs-recorder piece, next
        entry_url=entry_url,
        steps=steps,
        success_condition=success_condition,
        global_error_detectors=[],
        safety_policy=safety_policy,
        metadata=ArtifactMetadata(
            version="0.1.0",  # 0.x = not yet hardened with outputs/checkpoints
            created_at=now,
            updated_at=now,
            recorded_by="discovery_agent",
            status="draft",
        ),
    )
