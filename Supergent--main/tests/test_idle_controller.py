"""Tests for the resident activity-to-idle lifecycle."""

from core.idle_controller import RuntimeIdleController
from core.state.state_machine import WiseState, WiseStateMachine


def _ready_controller(timeout: int = 30):
    machine = WiseStateMachine()
    controller = RuntimeIdleController(
        state_machine=machine,
        resource_manager=object(),
        timeout_provider=lambda: timeout,
        check_interval_seconds=60,
    )
    controller.start()
    controller.stop()
    return controller, machine


def test_activity_wakes_runtime_then_idles_after_timeout():
    controller, machine = _ready_controller()
    controller.begin_activity("test work")
    assert machine.current_state == WiseState.REASONING
    controller.end_activity()

    assert controller.check_once(now=controller._last_activity + 31) is True
    assert machine.current_state == WiseState.IDLE


def test_active_operation_prevents_idle_transition():
    controller, machine = _ready_controller()
    controller.begin_activity("long work")

    assert controller.check_once(now=controller._last_activity + 999) is False
    assert machine.current_state == WiseState.REASONING
