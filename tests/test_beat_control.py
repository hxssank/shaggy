import pytest

from firmware.beat_control import BeatControl


def test_switches_after_six_continuous_seconds_at_maximum() -> None:
    control = BeatControl()
    assert control.remaining is None
    assert not control.update(100, 20)
    assert control.remaining == 6
    for remaining in (5, 4, 3, 2, 1):
        assert not control.update(100, 1)
        assert control.remaining == remaining
    assert control.update(100, 1)
    assert control.remaining is None


def test_leaving_maximum_restarts_the_entire_hold() -> None:
    control = BeatControl()
    control.update(100, 0)
    control.update(100, 5.9)
    assert not control.update(99, 0.1)
    assert control.remaining is None
    assert not control.update(100, 100)
    assert control.remaining == 6
    assert not control.update(100, 5.9)
    assert control.update(100, 0.2)


def test_only_rearms_below_95_after_switching() -> None:
    control = BeatControl()
    control.update(100, 0)
    assert control.update(100, 6)
    for percent in (100, 99, 95, 100):
        assert not control.update(percent, 60)
        assert control.remaining is None
    assert not control.update(94, 60)
    assert not control.update(100, 60)
    assert control.remaining == 6
    assert control.update(100, 6)


def test_countdown_uses_elapsed_time_with_uneven_frames() -> None:
    control = BeatControl(1.5)
    control.update(100, 2)
    assert control.remaining == 2
    assert not control.update(100, 0.4)
    assert control.remaining == 2
    assert not control.update(100, 0.7)
    assert control.remaining == 1
    assert control.update(100, 0.5)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_rejects_invalid_hold_time(seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        BeatControl(seconds)


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
def test_invalid_elapsed_time_does_not_change_a_pending_hold(seconds: float) -> None:
    control = BeatControl()
    control.update(100, 0)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        control.update(100, seconds)
    assert control.remaining == 6


@pytest.mark.parametrize("percent", [-1, 101, float("nan")])
def test_rejects_invalid_volume(percent: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        BeatControl().update(percent, 0)
