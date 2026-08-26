from securecam.pir import MockSensor, MotionDebouncer, MotionEdge, PirMonitor


def test_short_spike_is_ignored():
    debouncer = MotionDebouncer(debounce_seconds=0.2, min_active_seconds=1.0)
    assert debouncer.update(True, 0.0) is None
    assert debouncer.update(False, 0.1) is None
    assert debouncer.active is False


def test_stable_high_produces_one_start():
    debouncer = MotionDebouncer(debounce_seconds=0.2, min_active_seconds=1.0)
    assert debouncer.update(True, 0.0) is None
    assert debouncer.update(True, 0.3) is MotionEdge.START
    assert debouncer.update(True, 0.4) is None
    assert debouncer.active is True


def test_end_waits_for_minimum_active_time():
    debouncer = MotionDebouncer(debounce_seconds=0.1, min_active_seconds=2.0)
    debouncer.update(True, 0.0)
    assert debouncer.update(True, 0.2) is MotionEdge.START
    debouncer.update(False, 0.3)
    assert debouncer.update(False, 0.5) is None
    assert debouncer.update(False, 2.5) is MotionEdge.END


def test_mid_motion_dip_does_not_end_the_event():
    debouncer = MotionDebouncer(debounce_seconds=0.1, min_active_seconds=1.0)
    debouncer.update(True, 0.0)
    assert debouncer.update(True, 0.2) is MotionEdge.START
    assert debouncer.update(False, 0.4) is None
    assert debouncer.update(True, 0.6) is None
    assert debouncer.active is True


def test_monitor_reports_edges_from_a_mock_sensor(config):
    clock = {"now": 0.0}
    edges = []
    sensor = MockSensor(False)
    monitor = PirMonitor(
        config.motion, lambda edge, now: edges.append(edge), sensor=sensor, clock=lambda: clock["now"]
    )
    monitor._sensor = sensor  # the monitor is driven manually, no thread is started
    monitor._started_at = 0.0
    clock["now"] = config.motion.warmup_seconds + 1
    sensor.set(True)
    for offset in (0.0, 0.5, 1.0, 1.5):
        clock["now"] = config.motion.warmup_seconds + 1 + offset
        monitor.poll_once()
    sensor.set(False)
    for offset in (5.0, 6.0, 7.0):
        clock["now"] = config.motion.warmup_seconds + 1 + offset
        monitor.poll_once()
    assert edges == [MotionEdge.START, MotionEdge.END]


def test_status_reports_unavailable_sensor(config):
    monitor = PirMonitor(config.motion, lambda edge, now: None)
    status = monitor.status()
    assert status.available is False
