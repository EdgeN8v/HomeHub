from app.comfort import decide


def d(read, target=25, sp=25, step=0.5, dark=False, since=60, fresh=True):
    return decide(read, target, sp, step, 16, 31, dark, since, fresh)


def test_within_band_does_nothing():
    assert d(25.3) is None and d(24.7) is None


def test_too_warm_lowers_setpoint_too_cold_raises():
    assert d(26.0) == 24.5          # 1.0 over: half a degree... moves at least the step
    assert d(26.6) == 24.0          # 1.6 over: a whole degree
    assert d(23.4) == 26.0


def test_waits_for_the_room_to_answer():
    assert d(26.6, since=5) is None
    assert d(26.6, fresh=False) is None


def test_dark_display_tolerates_more_and_waits_longer():
    assert d(25.8, dark=True) is None
    assert d(26.6, dark=True, since=20) is None
    assert d(26.6, dark=True, since=40) == 24.0


def test_clamped_to_range_and_drift():
    assert d(30, sp=19.0) is None            # already 6 below the target
    assert decide(30, 17, 16, 0.5, 16, 31, False, 60, True) is None   # at the unit's minimum
