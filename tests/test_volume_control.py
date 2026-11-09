import pytest

from firmware.volume_control import VolumeControl


def test_starts_muted_even_with_knob_at_maximum() -> None:
    control = VolumeControl(initial_raw=65535)
    assert control.position == 0
    assert control.percent == 0
    assert control.muted
    assert control.db == -40
    control.update(65535, 0.1)
    assert control.position == 10
    assert not control.muted


@pytest.mark.parametrize(
    ("raw", "percent"),
    [(-1000, 0), (0, 0), (32767, 50), (32768, 50), (65535, 100), (100000, 100)],
)
def test_maps_and_clamps_adc_input(raw: int, percent: int) -> None:
    control = VolumeControl()
    control.update(raw, 2)
    assert control.percent == percent
    assert 0 <= control.position <= 100


@pytest.mark.parametrize(
    ("adc_min", "adc_max"),
    [(-1, 65535), (0, 65536), (1000, 1000), (2000, 1000)],
)
def test_rejects_invalid_endpoints(adc_min: int, adc_max: int) -> None:
    with pytest.raises(ValueError, match="ADC endpoints"):
        VolumeControl(adc_min=adc_min, adc_max=adc_max)


def test_uses_fixed_custom_endpoints_without_learning_them() -> None:
    control = VolumeControl(adc_min=1000, adc_max=61000)
    for raw in (1000, 50000, 61000, 3000, 62000, 31000):
        control.update(raw, 2)
    assert control.position == 50
    control.update(999, 2)
    assert control.percent == 0
    control.update(61001, 2)
    assert control.percent == 100


def test_every_displayed_percent_has_monotonic_capped_gain() -> None:
    control = VolumeControl(adc_min=0, adc_max=10000, deadband_percent=0)
    previous_db = -40.0
    for percent in range(101):
        control.update(percent * 100, 2)
        assert control.percent == percent
        assert control.muted == (percent == 0)
        assert previous_db <= control.db <= 0
        previous_db = control.db
        if percent == 1:
            assert control.db == -40
    assert control.db == 0


def test_displayed_zero_is_always_muted() -> None:
    control = VolumeControl(adc_min=0, adc_max=10000, deadband_percent=0)
    control.update(49, 2)
    assert control.position == pytest.approx(0.49)
    assert control.percent == 0
    assert control.muted
    control.update(50, 2)
    assert control.percent == 1
    assert not control.muted
    assert control.db == -40


def test_noise_inside_deadband_does_not_move_settled_bar() -> None:
    control = VolumeControl(initial_raw=30000, adc_min=0, adc_max=60000)
    control.update(30000, 2)
    for raw in (29800, 30100, 30400, 29900, 30000):
        control.update(raw, 0.1)
        assert control.position == 50
    control.update(30600, 2)
    assert control.position == 51


def test_deadband_does_not_prevent_reaching_endpoints() -> None:
    control = VolumeControl(adc_min=0, adc_max=10000)
    control.update(60, 2)
    assert control.percent == 0
    control.update(100, 2)
    assert control.percent == 1
    control.update(0, 2)
    assert control.muted
    control.update(9940, 2)
    assert control.position == 99.4
    control.update(10000, 2)
    assert control.position == 100


@pytest.mark.parametrize(
    ("initial_raw", "raw", "position", "percent"),
    [(80, 25, 0.25, 0), (9920, 9975, 99.75, 100)],
)
def test_rounded_endpoints_bypass_deadband_without_exact_adc_limits(
    initial_raw: int, raw: int, position: float, percent: int
) -> None:
    control = VolumeControl(initial_raw=initial_raw, adc_min=0, adc_max=10000)
    control.update(initial_raw, 2)
    assert control.percent not in (0, 100)

    control.update(raw, 2)

    assert control.position == pytest.approx(position)
    assert control.percent == percent
    assert control.muted == (percent == 0)


def test_ramp_is_bounded_in_both_directions_without_overshoot() -> None:
    control = VolumeControl(ramp_percent_per_second=40)
    control.update(65535, 0.25)
    assert control.position == 10
    control.update(65535, 0.5)
    assert control.position == 30
    control.update(0, 0.1)
    assert control.position == 26
    control.update(0, 100)
    assert control.position == 0
    assert control.muted
    control.update(65535, 100)
    assert control.position == 100
    assert control.db == 0


def test_ramp_depends_on_elapsed_time_not_update_count() -> None:
    single_update = VolumeControl(initial_raw=65535)
    many_updates = VolumeControl(initial_raw=65535)
    single_update.update(65535, 0.25)
    for _ in range(25):
        many_updates.update(65535, 0.01)
    assert single_update.position == pytest.approx(many_updates.position)


def test_zero_elapsed_time_does_not_move_volume() -> None:
    control = VolumeControl()
    control.update(65535, 0)
    assert control.position == 0
    assert control.muted


@pytest.mark.parametrize("elapsed", [-0.01, float("nan"), float("inf")])
def test_rejects_invalid_elapsed_time(elapsed: float) -> None:
    control = VolumeControl()
    with pytest.raises(ValueError, match="Elapsed time"):
        control.update(65535, elapsed)
    assert control.muted


@pytest.mark.parametrize("deadband", [-1, 100, float("nan"), float("inf")])
def test_rejects_invalid_deadband(deadband: float) -> None:
    with pytest.raises(ValueError, match="Deadband"):
        VolumeControl(deadband_percent=deadband)


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_ramp_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="Ramp rate"):
        VolumeControl(ramp_percent_per_second=rate)
