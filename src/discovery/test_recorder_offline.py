"""
Verifies record_artifact() against a reconstruction of the ACTUAL live
discovery run's trajectory (the one that really succeeded, printed in
chat) — not a synthetic example. No browser, no API key needed.

Run: python -m src.discovery.test_recorder_offline
"""

import json

from src.artifact.schema import (
    ActionType, ActionRiskLevel, ElementTarget, Locator, LocatorStrategy,
    Step, InputParam, ParamType, SafetyPolicy, Checkpoint, CheckpointType,
)
from src.discovery.loop import TrajectoryEntry, DiscoveryResult
from src.discovery.recorder import record_artifact

# Reconstructing exactly what the real run produced:
trajectory = [
    TrajectoryEntry(
        step=Step(
            order=1, name="Navigate to entry URL", action=ActionType.NAVIGATE,
            value="http://localhost:5000/member/search", risk_level=ActionRiskLevel.SAFE,
            error_detectors=[],
        ),
        reasoning="Start of discovery run", outcome="ok",
    ),
    TrajectoryEntry(
        step=Step(
            order=2, name="type (<input>)", action=ActionType.TYPE,
            target=ElementTarget(
                locators=[Locator(strategy=LocatorStrategy.CSS, value="input[name='member_id']")],
                description="<input>",
            ),
            value="{{member_id}}", risk_level=ActionRiskLevel.SAFE, error_detectors=[],
        ),
        reasoning="Type the member_id into the search input field.", outcome="ok",
    ),
    TrajectoryEntry(
        step=Step(
            order=3, name="click (Search)", action=ActionType.CLICK,
            target=ElementTarget(
                locators=[Locator(strategy=LocatorStrategy.TEXT, value="Search")],
                description="Search",
            ),
            risk_level=ActionRiskLevel.SAFE, error_detectors=[],
        ),
        reasoning="Click Search to perform the search.", outcome="ok",
    ),
]

result = DiscoveryResult(
    success=True, business_outcome=True, stuck=False, dead_end=False,
    outcome_note="Successfully looked up member 10001 and found the savings balance of $5,230.45.",
    trajectory=trajectory, stop_reason="llm_reported_done",
)

artifact = record_artifact(
    result,
    artifact_id="lookup_member_balance_discovered",
    name="Look up member balance (auto-discovered)",
    description="Discovered automatically by the LLM agent, no hand-written steps.",
    entry_url="http://localhost:5000/member/search",
    input_specs=[
        InputParam(name="member_id", type=ParamType.STRING, required=True, sensitive=False),
    ],
    safety_policy=SafetyPolicy(
        allowed_domains=["localhost:5000", "127.0.0.1:5000"],
        allowed_actions=[a.value for a in ActionType],
        max_unconfirmed_risk=ActionRiskLevel.MODERATE,
        require_human_confirmation_for=["submit", "confirm"],
    ),
    success_condition=Checkpoint(
        type=CheckpointType.TEXT_PRESENT,
        value="Savings",
        description="Savings balance is visible on the results page after search.",
    ),
)

print("Artifact recorded successfully:")
print(f"  id: {artifact.id}")
print(f"  version: {artifact.metadata.version}  status: {artifact.metadata.status}")
print(f"  steps: {len(artifact.steps)}")
for s in artifact.steps:
    print(f"    {s.order}. {s.action} - {s.name}"
          + (f" -> {s.target.locators[0].strategy}:{s.target.locators[0].value}" if s.target else ""))

# The real proof: does it round-trip through JSON and re-validate,
# exactly like your hand-written artifacts already do?
as_json = artifact.model_dump_json(indent=2)
from src.artifact.schema import Artifact as ArtifactModel
reloaded = ArtifactModel(**json.loads(as_json))
print(f"\nRound-trips through JSON and re-validates: {reloaded.id == artifact.id}")

with open("evidence/artifacts/lookup_member_balance_discovered.json", "w") as f:
    f.write(as_json)
print("Saved to evidence/artifacts/lookup_member_balance_discovered.json")
