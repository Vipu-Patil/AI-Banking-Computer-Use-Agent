"""
Tests the LOOP's control flow logic in isolation: stopping conditions,
error-bucket handling, dead-end detection. Uses fake stand-ins for both
the browser and the LLM, so this needs no API key and no real browser —
it verifies the state machine itself, not browser/LLM behavior (those
were verified separately: browser in Step 4, LLM via test_llm_real_call.py).

Run: python -m src.discovery.test_loop_offline
"""

from unittest.mock import MagicMock, patch

from src.artifact.schema import SafetyPolicy, ActionRiskLevel
from src.discovery.loop import run_discovery, DiscoveryResult
from src.discovery.llm_client import Decision, ActionDecision
from src.discovery.element_inventory import InventoryEntry, Locator, LocatorStrategy


def make_fake_controller():
    ctrl = MagicMock()
    ctrl.page.url = "http://localhost:5000/member/search"
    obs = MagicMock()
    obs.text_by_frame = {"main": "fake page text"}
    ctrl.observe.return_value = obs
    return ctrl


def decision(*, done=False, success=False, stuck=False, outcome_note="",
             action_type=None, target_index=None, value=None,
             risk_level="safe", reasoning="test"):
    action = None
    if not done and not stuck:
        action = ActionDecision(type=action_type, target_index=target_index,
                                 value=value, risk_level=risk_level, checkpoint_hint="hint")
    return Decision(reasoning=reasoning, done=done, success=success,
                     outcome_note=outcome_note, stuck=stuck, action=action, raw={})


fake_inventory = [
    InventoryEntry(0, "input", "", "text",
                    Locator(strategy=LocatorStrategy.CSS, value="input[name='x']"), None),
]


def test_normal_success():
    ctrl = make_fake_controller()
    ctrl.execute_step.return_value = None  # every action "succeeds"
    decisions = [
        decision(action_type="type", target_index=0, value="{{x}}"),
        decision(done=True, success=True, outcome_note="Balance found"),
    ]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={"x": "1"}, max_steps=10)
    assert result.success and result.stop_reason == "llm_reported_done"
    print("PASS: normal success ->", result.stop_reason)


def test_business_outcome():
    from src.browser.errors import BusinessOutcomeResult
    ctrl = make_fake_controller()
    ctrl.execute_step.return_value = BusinessOutcomeResult(
        outcome_code="NOT_FOUND", outcome_message="not found")
    decisions = [decision(action_type="click", target_index=0)]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={}, max_steps=10)
    assert result.business_outcome and result.stop_reason == "business_outcome_detected"
    print("PASS: business outcome ->", result.stop_reason)


def test_stuck_escalates():
    ctrl = make_fake_controller()
    decisions = [decision(stuck=True, reasoning="genuinely confused")]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={}, max_steps=10)
    assert result.stuck and result.stop_reason == "stuck_escalated_to_human"
    ctrl.pause_for_human.assert_called_once()
    print("PASS: stuck escalation ->", result.stop_reason, "(pause_for_human called)")


def test_invalid_index_recovers_then_succeeds():
    ctrl = make_fake_controller()
    ctrl.execute_step.return_value = None
    decisions = [
        decision(action_type="click", target_index=99),  # out of range
        decision(done=True, success=True, outcome_note="recovered"),
    ]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={}, max_steps=10)
    assert result.success
    assert any(e.outcome == "hard_failure" for e in result.trajectory)
    print("PASS: invalid index recorded as failure but loop continued and recovered")


def test_dead_end_repeated_failures():
    from src.browser.errors import HardFailure
    ctrl = make_fake_controller()
    # First call (the deterministic nav step) succeeds; every action
    # step after that fails, to isolate "the loop itself is stuck" from
    # "it couldn't even load the entry page" (that's a separate, correctly
    # distinct stop_reason: dead_end_at_entry).
    ctrl.execute_step.side_effect = [None] + [HardFailure("boom", outcome_code="X")] * 10
    decisions = [decision(action_type="click", target_index=0) for _ in range(10)]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={}, max_steps=10)
    assert result.dead_end and result.stop_reason == "dead_end_repeated_failures"
    print("PASS: repeated hard failures correctly detected as dead end ->", result.stop_reason)


def test_risky_action_escalates():
    from src.browser.errors import RecoverableCondition
    ctrl = make_fake_controller()
    ctrl.execute_step.side_effect = [None, RecoverableCondition("CONFIRMATION_REQUIRED", "needs human")]
    decisions = [decision(action_type="click", target_index=0, risk_level="high")]
    with patch("src.discovery.loop.build_inventory", return_value=fake_inventory), \
         patch("src.discovery.loop.decide_next_action", side_effect=decisions):
        result = run_discovery(controller=ctrl, client=MagicMock(), goal="g",
                                entry_url="http://x", input_params={}, max_steps=10)
    assert result.stuck and result.stop_reason == "risky_action_needs_confirmation"
    ctrl.pause_for_human.assert_called_once()
    print("PASS: risky action correctly escalated ->", result.stop_reason)


if __name__ == "__main__":
    test_normal_success()
    test_business_outcome()
    test_stuck_escalates()
    test_invalid_index_recovers_then_succeeds()
    test_dead_end_repeated_failures()
    test_risky_action_escalates()
    print("\nALL OFFLINE LOOP TESTS PASSED")
