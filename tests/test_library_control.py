import pytest

from firmware.library_control import LibraryControl


def open_library(control: LibraryControl) -> None:
    assert control.update(0, 0, 0) is None
    assert control.update(0, 0, 2) == "open"
    assert control.browsing


def test_boot_does_not_count_time_before_first_muted_reading() -> None:
    control = LibraryControl(4)
    assert control.remaining is None
    assert control.update(0, 0, 60) is None
    assert control.remaining == 2
    assert control.update(1, 0, 1) is None
    assert control.remaining == 1
    assert control.update(0, 0, 1) == "open"
    assert control.remaining is None


@pytest.mark.parametrize("percent,mask", [(2, 0), (0, 1), (0, 256), (0, 511)])
def test_sound_or_present_tile_interrupts_the_entire_hold(
    percent: int, mask: int
) -> None:
    control = LibraryControl(4)
    control.update(0, 0, 0)
    control.update(0, 0, 1.9)
    assert control.update(percent, mask, 10) is None
    assert control.remaining is None
    assert not control.browsing
    assert control.update(0, 0, 10) is None
    assert control.remaining == 2
    assert control.update(0, 0, 1.9) is None
    assert control.update(0, 0, 0.2) == "open"


def test_opening_preserves_current_bank_until_knob_moves() -> None:
    control = LibraryControl(4, selected_index=2)
    open_library(control)
    for percent in (0, 0.5, 1, 2.9):
        assert control.update(percent, 0, 60) is None
        assert control.selected_index == 2
    control.update(3, 0, 60)
    assert control.selected_index == 2
    control.update(3, 0, 0.16)
    assert control.selected_index == 0


def test_boundary_jitter_does_not_switch_banks() -> None:
    control = LibraryControl(4)
    open_library(control)
    for percent in (24, 25, 26, 25.5, 26.9):
        control.update(percent, 0, 1)
        assert control.selected_index == 0
    control.update(27, 0, 1)
    control.update(27, 0, 0.16)
    assert control.selected_index == 1
    for percent in (26, 25, 24, 23.1):
        control.update(percent, 0, 1)
        assert control.selected_index == 1
    control.update(23, 0, 1)
    control.update(23, 0, 0.16)
    assert control.selected_index == 0


def test_quick_sweep_only_commits_a_stable_bank() -> None:
    control = LibraryControl(4)
    open_library(control)
    for percent in (30, 55, 80, 100):
        control.update(percent, 0, 0.02)
    assert control.selected_index == 0
    control.update(100, 0, 0.1)
    assert control.selected_index == 0
    control.update(100, 0, 0.1)
    assert control.selected_index == 3


def test_returning_inside_current_band_cancels_pending_change() -> None:
    control = LibraryControl(4)
    open_library(control)
    control.update(40, 0, 0)
    control.update(40, 0, 0.1)
    control.update(10, 0, 0.1)
    control.update(40, 0, 1)
    assert control.selected_index == 0
    control.update(40, 0, 0.16)
    assert control.selected_index == 1


def test_tile_confirms_highlighted_bank_once_without_pending_selection() -> None:
    control = LibraryControl(4, selected_index=2)
    open_library(control)
    control.update(100, 0, 0.01)
    assert control.update(100, 16, 0.01) == "select"
    assert control.selected_index == 2
    assert not control.browsing
    for _ in range(4):
        assert control.update(0, 16, 10) is None
    assert control.remaining is None
    assert control.update(0, 0, 10) is None
    assert control.update(0, 0, 2) == "open"
    assert control.selected_index == 2


def test_single_bank_library_can_open_and_confirm() -> None:
    control = LibraryControl(1)
    open_library(control)
    control.update(100, 0, 1)
    control.update(100, 0, 1)
    assert control.selected_index == 0
    assert control.update(100, 1, 0) == "select"


def test_small_bands_remain_reachable_in_large_libraries() -> None:
    control = LibraryControl(100, selected_index=50)
    open_library(control)
    control.update(52.5, 0, 0)
    control.update(52.5, 0, 0.16)
    assert control.selected_index == 52


@pytest.mark.parametrize("count", [0, -1, 1.5, True, float("nan"), float("inf")])
def test_rejects_invalid_bank_count(count: int) -> None:
    with pytest.raises(ValueError, match="at least one"):
        LibraryControl(count)


@pytest.mark.parametrize("selected", [-1, 4, 0.5, True, float("nan")])
def test_rejects_invalid_selected_bank(selected: int) -> None:
    with pytest.raises(ValueError, match="inside the library"):
        LibraryControl(4, selected_index=selected)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
@pytest.mark.parametrize("parameter", ["hold_seconds", "debounce_seconds"])
def test_rejects_invalid_timing(parameter: str, seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        LibraryControl(4, **{parameter: seconds})


@pytest.mark.parametrize("percent", [-1, 101, float("inf"), float("nan")])
def test_invalid_volume_preserves_pending_hold(percent: float) -> None:
    control = LibraryControl(4)
    control.update(0, 0, 0)
    with pytest.raises(ValueError, match="between 0 and 100"):
        control.update(percent, 0, 1)
    assert control.remaining == 2


@pytest.mark.parametrize("mask", [-1, 512, True, 1.5, float("nan")])
def test_invalid_sensor_mask_preserves_pending_hold(mask: int) -> None:
    control = LibraryControl(4)
    control.update(0, 0, 0)
    with pytest.raises(ValueError, match="nine bits"):
        control.update(0, mask, 1)
    assert control.remaining == 2


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
def test_invalid_elapsed_time_preserves_pending_hold(seconds: float) -> None:
    control = LibraryControl(4)
    control.update(0, 0, 0)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        control.update(0, 0, seconds)
    assert control.remaining == 2
