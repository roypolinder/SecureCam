from securecam.statemachine import ActionKind, EventState, FinalizeReason, MotionStateMachine


def machine():
    return MotionStateMachine(post_motion_seconds=10.0, max_event_seconds=60.0, cooldown_seconds=5.0)


def test_motion_starts_an_event():
    fsm = machine()
    actions = fsm.on_motion_start(0.0)
    assert [a.kind for a in actions] == [ActionKind.START_EVENT]
    assert fsm.state is EventState.MOTION_DETECTED
    fsm.mark_recording()
    assert fsm.state is EventState.RECORDING


def test_event_finalizes_after_the_quiet_period():
    fsm = machine()
    fsm.on_motion_start(0.0)
    fsm.mark_recording()
    assert [a.kind for a in fsm.on_motion_end(2.0)] == [ActionKind.MOTION_PAUSED]
    assert fsm.tick(5.0) == []
    actions = fsm.tick(12.1)
    assert [a.kind for a in actions] == [ActionKind.FINALIZE_EVENT]
    assert actions[0].reason is FinalizeReason.QUIET_PERIOD


def test_motion_during_the_quiet_period_extends_the_event():
    fsm = machine()
    fsm.on_motion_start(0.0)
    fsm.mark_recording()
    fsm.on_motion_end(2.0)
    assert [a.kind for a in fsm.on_motion_start(5.0)] == [ActionKind.MOTION_RESUMED]
    assert fsm.tick(12.1) == []
    fsm.on_motion_end(13.0)
    assert [a.kind for a in fsm.tick(23.1)] == [ActionKind.FINALIZE_EVENT]


def test_max_duration_caps_a_stuck_sensor():
    fsm = machine()
    fsm.on_motion_start(0.0)
    fsm.mark_recording()
    actions = fsm.tick(60.5)
    assert actions[0].kind is ActionKind.FINALIZE_EVENT
    assert actions[0].reason is FinalizeReason.MAX_DURATION


def test_cooldown_blocks_an_immediate_restart():
    fsm = machine()
    fsm.on_motion_start(0.0)
    fsm.mark_recording()
    fsm.force_finalize(10.0)
    fsm.notify_finalized(10.0)
    assert fsm.on_motion_start(11.0) == []
    assert fsm.on_motion_start(16.0)[0].kind is ActionKind.START_EVENT


def test_still_active_sensor_starts_a_new_event_after_cooldown():
    fsm = machine()
    fsm.on_motion_start(0.0)
    fsm.mark_recording()
    fsm.tick(60.5)
    fsm.notify_finalized(60.5)
    assert fsm.tick(61.0) == []
    assert [a.kind for a in fsm.tick(66.0)] == [ActionKind.START_EVENT]


def test_force_finalize_is_a_no_op_when_idle():
    fsm = machine()
    assert fsm.force_finalize(1.0) == []
