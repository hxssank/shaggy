import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from firmware import volume_control


class FakeDac:
    def __init__(self) -> None:
        self.speaker_mute = True
        self.speaker_shorted = False
        self.overtemperature = False
        self.configure_clocks = Mock()
        self._page0 = SimpleNamespace(_read_register=Mock(return_value=0))

    @property
    def speaker_output(self) -> bool:
        return self._speaker_output

    @speaker_output.setter
    def speaker_output(self, enabled: bool) -> None:
        self._speaker_output = enabled
        if enabled:
            self.speaker_mute = False


@pytest.fixture
def hardware(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    dac, reset, i2c, pot = FakeDac(), Mock(), Mock(), Mock(value=0)
    audio, screen = Mock(), Mock()
    handles = [Mock() for _ in range(9)]
    waves = [
        Mock(sample_rate=44100, channel_count=1, bits_per_sample=16) for _ in range(9)
    ]
    voices = [Mock(playing=True) for _ in range(9)]
    mixer = Mock(voice=voices)
    state = SimpleNamespace(
        now=0.0,
        started=False,
        events=[],
        mute_samples=[],
        knob_samples=[],
        control_samples=[],
        screen_samples=[],
        render_dac_samples=[],
        render_seconds=0.0,
    )

    def play(sample: object) -> None:
        state.started = True
        state.events.append("audio.play")
        state.mute_samples.append(dac.speaker_mute)

    def advance(seconds: float) -> None:
        state.now += seconds
        if state.started:
            state.mute_samples.append(dac.speaker_mute)

    def render(control: volume_control.VolumeControl, applied_db: float) -> None:
        state.render_dac_samples.append(
            (round(control.db * 2) / 2, control.muted, dac.dac_volume, dac.speaker_mute)
        )
        state.screen_samples.append(
            (
                state.now,
                control.percent,
                control.muted,
                applied_db,
                len(state.knob_samples),
            )
        )
        advance(state.render_seconds)

    class RecordingControl(volume_control.VolumeControl):
        def update(self, raw: int, elapsed_seconds: float) -> None:
            super().update(raw, elapsed_seconds)
            state.control_samples.append(
                (state.now, raw, elapsed_seconds, self.percent)
            )

    audio.play.side_effect = play
    audio.pause.side_effect = lambda: state.events.append("audio.pause")
    audio.resume.side_effect = lambda: state.events.append("audio.resume")
    audio.stop.side_effect = lambda: state.events.append(("audio.stop", reset.value))
    screen.update.side_effect = render
    for index, voice in enumerate(voices):
        voice.play.side_effect = lambda *args, index=index, **kwargs: (
            state.events.append(("voice.play", index))
        )
    modules = {
        "adafruit_tlv320": {"TLV320DAC3100": Mock(return_value=dac)},
        "analogio": {"AnalogIn": Mock(return_value=pot)},
        "audiobusio": {"I2SOut": Mock(return_value=audio)},
        "audiocore": {"WaveFile": Mock(side_effect=waves)},
        "audiomixer": {"Mixer": Mock(return_value=mixer)},
        "board": {"GP%d" % index: index for index in range(29)},
        "busio": {"I2C": Mock(return_value=i2c)},
        "digitalio": {"DigitalInOut": Mock(return_value=reset)},
        "displayio": {"Group": list},
        "fourwire": {},
        "terminalio": {},
        "vectorio": {},
        "adafruit_display_text": {"label": Mock()},
        "adafruit_st7789": {"ST7789": Mock()},
    }
    for name, attributes in modules.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**attributes))
    monkeypatch.setitem(sys.modules, "volume_control", volume_control)
    path = Path(__file__).parents[1] / "firmware" / "volume_test.py"
    spec = importlib.util.spec_from_file_location("volume_test_under_test", path)
    assert spec is not None and spec.loader is not None
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    program.VolumeDisplay = Mock(return_value=screen)
    program.VolumeControl = RecordingControl
    program.time = SimpleNamespace(monotonic=lambda: state.now, sleep=advance)
    program.open = Mock(side_effect=handles)
    read_knob = program.read_knob

    def record_knob(pot: object) -> int:
        raw = read_knob(pot)
        state.knob_samples.append((state.now, raw))
        return raw

    program.read_knob = record_knob
    return SimpleNamespace(
        program=program,
        dac=dac,
        reset=reset,
        i2c=i2c,
        pot=pot,
        audio=audio,
        screen=screen,
        mixer=mixer,
        handles=handles,
        waves=waves,
        state=state,
    )


def test_zero_percent_stays_muted_at_playback_start_and_during_loop(
    hardware: SimpleNamespace,
) -> None:
    hardware.program.main(0.04)
    assert len(hardware.state.mute_samples) >= 3
    assert all(hardware.state.mute_samples)
    hardware.screen.update.assert_called()
    for _, percent, muted, applied_db, _ in hardware.state.screen_samples:
        assert percent == 0
        assert muted
        assert applied_db == -40


def test_volume_uses_elapsed_time_after_a_slow_display_refresh(
    hardware: SimpleNamespace,
) -> None:
    hardware.pot.value = 65535
    hardware.state.render_seconds = 0.15
    hardware.program.main(0.18)

    first, second = hardware.state.control_samples[:2]
    assert first[3] == 0
    _, raw, elapsed, percent = second
    assert raw == 65535
    assert elapsed == pytest.approx(0.152)
    assert percent == 76
    assert hardware.state.screen_samples[0][1:4] == (0, True, -40)


def test_slow_display_refresh_leaves_time_for_control_reads_before_redrawing(
    hardware: SimpleNamespace,
) -> None:
    hardware.pot.value = 65535
    hardware.state.render_seconds = 0.15
    hardware.program.main(0.43)

    first, second = hardware.state.screen_samples[:2]
    first_time, _, _, _, first_reads = first
    second_time, percent, muted, applied_db, second_reads = second
    assert second_time - first_time >= 0.158 - 1e-9
    assert second_reads - first_reads >= 3
    assert 79 <= percent <= 80
    assert not muted
    assert -9 < applied_db < -7.5


def test_fast_display_runs_near_thirty_frames_per_second_between_control_updates(
    hardware: SimpleNamespace,
) -> None:
    hardware.pot.value = 65535
    hardware.state.render_seconds = 0.006
    hardware.program.main(0.3)

    frames = hardware.state.screen_samples
    assert 8 <= len(frames) <= 10
    for previous, current in zip(frames, frames[1:]):
        assert 1 / 30 <= current[0] - previous[0] <= 0.036
        assert current[4] - previous[4] >= 10
    for (
        expected_db,
        expected_mute,
        actual_db,
        actual_mute,
    ) in hardware.state.render_dac_samples:
        assert actual_db == expected_db
        assert actual_mute == expected_mute


def test_starts_mixer_before_voices_and_stops_audio_with_dac_in_reset(
    hardware: SimpleNamespace,
) -> None:
    hardware.program.main(0.04)
    assert hardware.state.events[:12] == ["audio.play", "audio.pause"] + [
        ("voice.play", index) for index in range(9)
    ] + ["audio.resume"]
    assert hardware.state.events[-1] == ("audio.stop", False)
    hardware.audio.deinit.assert_called_once()
    hardware.mixer.deinit.assert_called_once()
    hardware.screen.stop.assert_called_once()


def test_missing_wav_holds_reset_low_and_closes_already_open_files(
    hardware: SimpleNamespace,
) -> None:
    hardware.program.open.side_effect = hardware.handles[:2] + [FileNotFoundError()]
    with pytest.raises(FileNotFoundError):
        hardware.program.main(0.04)
    assert hardware.reset.value is False
    hardware.audio.play.assert_not_called()
    for handle, wave in zip(hardware.handles[:2], hardware.waves[:2], strict=True):
        handle.close.assert_called_once()
        wave.deinit.assert_called_once()
    hardware.pot.deinit.assert_called_once()
    hardware.i2c.deinit.assert_called_once()
    hardware.screen.stop.assert_called_once()


def test_loop_exception_stops_audio_and_releases_wave_resources(
    hardware: SimpleNamespace,
) -> None:
    hardware.screen.update.side_effect = RuntimeError("display failed")
    with pytest.raises(RuntimeError, match="display failed"):
        hardware.program.main(0.04)
    assert hardware.reset.value is False
    assert hardware.state.events[-1] == ("audio.stop", False)
    hardware.audio.deinit.assert_called_once()
    hardware.mixer.deinit.assert_called_once()
    for handle, wave in zip(hardware.handles, hardware.waves, strict=True):
        handle.close.assert_called_once()
        wave.deinit.assert_called_once()
    hardware.pot.deinit.assert_called_once()
    hardware.i2c.deinit.assert_called_once()
    hardware.screen.stop.assert_called_once()
