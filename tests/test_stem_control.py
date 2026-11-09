import pytest

from firmware.stem_control import StemControl


def test_startup_waits_for_stable_presence_before_enabling_any_stem() -> None:
    control = StemControl()
    present = [True] * 9
    assert control.levels == [0.0] * 9
    assert control.active_mask == control.active_count == control.changed_mask == 0
    assert not control.update(present, 1.0)
    assert not control.update(present, 0.019)
    assert control.active_count == 0

    assert control.update(present, 0.001)

    assert control.active_mask == control.changed_mask == 0x1FF
    assert control.active_count == 9
    assert control.levels == [0.0] * 9


def test_presence_holds_level_without_retriggering_or_toggling() -> None:
    control = StemControl()
    present = (True,) + (False,) * 8
    levels = control.levels
    control.update(present, 0)
    assert control.update(present, 0.03)
    assert levels[0] == pytest.approx(0.175)
    assert control.update(present, 0.01)
    assert levels[0] == pytest.approx(0.35)
    assert not control.update(present, 10)
    assert control.levels is levels
    assert control.active_mask == 1
    assert control.active_count == 1
    assert control.changed_mask == 0
    assert levels[1:] == [0.0] * 8


def test_removal_debounces_then_fades_to_silence() -> None:
    control = StemControl()
    present = [True] + [False] * 8
    control.update(present, 0)
    control.update(present, 0.04)
    present[0] = False
    assert not control.update(present, 0)
    assert not control.update(present, 0.019)
    assert control.active_count == 1
    assert control.levels[0] == 0.35

    assert control.update(present, 0.011)

    assert control.active_mask == control.active_count == 0
    assert control.changed_mask == 1
    assert control.levels[0] == pytest.approx(0.175)
    assert control.update(present, 0.011)
    assert control.levels[0] == 0
    assert not control.update(present, 1)


def test_bounce_restarts_the_presence_timer() -> None:
    control = StemControl()
    present = [True] + [False] * 8
    control.update(present, 0)
    control.update(present, 0.015)
    present[0] = False
    control.update(present, 0.001)
    present[0] = True
    control.update(present, 0.002)
    assert not control.update(present, 0.015)
    assert control.active_count == 0
    assert control.levels[0] == 0
    assert control.update(present, 0.0051)
    assert control.active_mask == 1


def test_stems_debounce_and_fade_independently() -> None:
    control = StemControl()
    present = [False] * 9
    present[0] = present[8] = True
    control.update(present, 0)
    control.update(present, 0.04)
    assert control.active_mask == control.changed_mask == 0x101
    assert control.active_count == 2
    present[0] = False
    present[4] = True
    control.update(present, 0)
    control.update(present, 0.04)
    assert control.active_mask == 0x110
    assert control.changed_mask == 0x011
    assert control.active_count == 2
    assert control.levels == [0, 0, 0, 0, 0.35, 0, 0, 0, 0.35]


def test_debounce_and_fade_depend_on_time_not_update_count() -> None:
    coarse, fine = StemControl(), StemControl()
    present = [True] * 9
    coarse.update(present, 0)
    fine.update(present, 0)
    coarse.update(present, 0.03)
    for _ in range(10):
        fine.update(present, 0.003)
    assert coarse.active_mask == fine.active_mask == 0x1FF
    assert coarse.levels == pytest.approx(fine.levels)
    assert coarse.levels == pytest.approx([0.175] * 9)
    coarse.update(present, 1)
    fine.update(present, 1)
    assert coarse.levels == fine.levels == [0.35] * 9


def test_zero_elapsed_time_neither_debounces_nor_fades() -> None:
    control = StemControl()
    present = [True] * 9
    for _ in range(10):
        assert not control.update(present, 0)
    assert control.active_mask == 0
    assert control.levels == [0.0] * 9


@pytest.mark.parametrize("elapsed", [-0.01, float("nan"), float("inf")])
def test_invalid_elapsed_time_does_not_change_state(elapsed: float) -> None:
    control = StemControl()
    with pytest.raises(ValueError, match="Elapsed time"):
        control.update([True] * 9, elapsed)
    assert control.levels == [0.0] * 9
    assert control.active_count == 0


@pytest.mark.parametrize("count", [0, 8, 10])
def test_invalid_input_count_does_not_change_state(count: int) -> None:
    control = StemControl()
    with pytest.raises(ValueError, match="nine"):
        control.update([True] * count, 0.04)
    assert control.levels == [0.0] * 9
    assert control.active_count == 0


@pytest.mark.parametrize("value", [0, -0.01, float("nan"), float("inf")])
@pytest.mark.parametrize("parameter", ["debounce_seconds", "fade_seconds"])
def test_invalid_durations_are_rejected(parameter: str, value: float) -> None:
    with pytest.raises(ValueError, match="duration"):
        StemControl(**{parameter: value})


@pytest.mark.parametrize("value", [0, -0.01, 1.01, float("nan"), float("inf")])
def test_invalid_stem_level_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="Stem level"):
        StemControl(stem_level=value)
