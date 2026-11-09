import importlib.util
import io
import json
import runpy
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from firmware import (
    audio_assets,
    beat_control,
    library_control,
    stem_control,
    volume_control,
)


def wav_handle(frames: int = 4, rate: int = 44100) -> io.BytesIO:
    format_data = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    payload = (
        b"WAVEfmt "
        + struct.pack("<I", len(format_data))
        + format_data
        + b"data"
        + struct.pack("<I", frames * 2)
        + b"\0\0" * frames
    )
    return io.BytesIO(b"RIFF" + struct.pack("<I", len(payload)) + payload)


@pytest.fixture
def player(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        now=0.0,
        started=None,
        events=[],
        samples=[],
        sensor_schedule=[(0.0, 0)],
        render_seconds=0.006,
        fault=None,
        fault_at=0.0,
        paused=False,
    )

    def elapsed() -> float:
        return 0.0 if state.started is None else state.now - state.started

    class Reset:
        def __init__(self) -> None:
            self._value = False
            self.values = []
            self.deinit = Mock()
            self.switch_to_output = Mock(side_effect=self.set_output)

        def set_output(self, *, value: bool) -> None:
            self.value = value

        @property
        def value(self) -> bool:
            return self._value

        @value.setter
        def value(self, value: bool) -> None:
            self._value = value
            self.values.append(value)

    class Sensor:
        def __init__(self, index: int) -> None:
            self.index = index
            self.switch_to_input = Mock()
            self.deinit = Mock()

        @property
        def value(self) -> bool:
            mask = 0
            for at, active_mask in state.sensor_schedule:
                if elapsed() >= at:
                    mask = active_mask
            return not bool(mask & (1 << self.index))

    class Voice:
        def __init__(self) -> None:
            self._level = 0.0
            self.level_changes = []
            self.playing = False
            self.play = Mock(side_effect=self.start)

        def start(self, wave: object, *, loop: bool) -> None:
            assert loop
            assert state.paused
            assert dac.speaker_mute
            self.playing = True

        @property
        def level(self) -> float:
            return self._level

        @level.setter
        def level(self, value: float) -> None:
            self._level = value
            self.level_changes.append((elapsed(), value))

    class Dac:
        def __init__(self) -> None:
            self.speaker_mute = True
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

        @property
        def speaker_shorted(self) -> bool:
            return state.fault == "short" and elapsed() >= state.fault_at

        @property
        def overtemperature(self) -> bool:
            return state.fault == "temperature" and elapsed() >= state.fault_at

    reset, dac = Reset(), Dac()
    sensors = [Sensor(index) for index in range(9)]
    voices = [Voice() for _ in range(9)]
    mixer = Mock(voice=voices)
    audio, screen, i2c, pot = Mock(), Mock(), Mock(), Mock(value=65535)
    handles = [wav_handle() for _ in range(9)]
    waves = [Mock() for _ in range(9)]

    def sample() -> None:
        state.samples.append(
            (
                elapsed(),
                dac.speaker_mute,
                tuple(voice.level for voice in voices),
                tuple(voice.playing for voice in voices),
            )
        )

    def advance(seconds: float) -> None:
        state.now += seconds
        if state.started is not None:
            sample()
            if elapsed() > 3:
                raise AssertionError("Player exceeded the simulated test duration")

    def start_audio(sample_source: object) -> None:
        assert sample_source is mixer
        for voice in voices:
            voice.playing = False
        state.started = state.now
        sample()

    def pause_audio() -> None:
        assert dac.speaker_mute
        state.paused = True

    def resume_audio() -> None:
        assert state.paused
        assert all(voice.playing for voice in voices)
        assert dac.speaker_mute
        state.paused = False

    def stop_audio() -> None:
        state.events.append(("audio.stop", reset.value))
        for voice in voices:
            voice.playing = False

    audio.play.side_effect = start_audio
    audio.pause.side_effect = pause_audio
    audio.resume.side_effect = resume_audio
    audio.stop.side_effect = stop_audio
    screen.update.side_effect = lambda *args: advance(state.render_seconds)
    dac_factory = Mock(return_value=dac)
    pin_factory = Mock(side_effect=lambda pin: reset if pin == 13 else sensors[pin])
    i2c_factory = Mock(return_value=i2c)
    mixer_factory = Mock(return_value=mixer)
    screen_factory = Mock(return_value=screen)
    knob_reader = Mock(side_effect=lambda analog_input: analog_input.value)
    modules = {
        "adafruit_tlv320": SimpleNamespace(TLV320DAC3100=dac_factory),
        "analogio": SimpleNamespace(AnalogIn=Mock(return_value=pot)),
        "audiobusio": SimpleNamespace(I2SOut=Mock(return_value=audio)),
        "audiocore": SimpleNamespace(WaveFile=Mock(side_effect=waves)),
        "audiomixer": SimpleNamespace(Mixer=mixer_factory),
        "board": SimpleNamespace(**{"GP%d" % index: index for index in range(29)}),
        "busio": SimpleNamespace(I2C=i2c_factory),
        "digitalio": SimpleNamespace(
            DigitalInOut=pin_factory, Pull=SimpleNamespace(UP=1)
        ),
        "supervisor": SimpleNamespace(runtime=SimpleNamespace(serial_connected=False)),
        "audio_assets": audio_assets,
        "player_display": SimpleNamespace(PlayerDisplay=screen_factory),
        "stem_control": stem_control,
        "volume_control": volume_control,
        "volume_test": SimpleNamespace(
            CONTROL_INTERVAL=0.002,
            DISPLAY_FPS=30,
            MIN_CONTROL_WINDOW=0.008,
            SPEAKER_GAIN_DB=12,
            STEM_LEVEL=0.35,
            STEM_NAMES=(
                "kick",
                "clap",
                "closedhat",
                "openhat",
                "bass",
                "chords",
                "perc",
                "arp",
                "pad",
            ),
            read_knob=knob_reader,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).parents[1] / "firmware" / "shaggy.py"
    spec = importlib.util.spec_from_file_location("player_under_test", path)
    assert spec is not None and spec.loader is not None
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    program.time = SimpleNamespace(monotonic=lambda: state.now, sleep=advance)
    program.open = Mock(side_effect=handles)
    return SimpleNamespace(
        program=program,
        state=state,
        reset=reset,
        dac=dac,
        sensors=sensors,
        voices=voices,
        mixer=mixer,
        audio=audio,
        screen=screen,
        i2c=i2c,
        pot=pot,
        handles=handles,
        waves=waves,
        dac_factory=dac_factory,
        pin_factory=pin_factory,
        i2c_factory=i2c_factory,
        mixer_factory=mixer_factory,
        screen_factory=screen_factory,
        knob_reader=knob_reader,
    )


def test_no_tiles_stays_muted_with_all_voice_levels_zero_at_maximum_volume(
    player: SimpleNamespace,
) -> None:
    player.program.run(0.25)
    assert player.dac.dac_volume == 0
    assert player.state.samples
    assert all(
        muted and levels == (0,) * 9 for _, muted, levels, _ in player.state.samples
    )
    for voice in player.voices:
        voice.play.assert_called_once()
        assert all(level == 0 for _, level in voice.level_changes)
    player.screen.show_error.assert_not_called()
    player.screen.set_streaming.assert_not_called()


def test_one_stable_tile_fades_only_its_stem_in(player: SimpleNamespace) -> None:
    player.state.sensor_schedule = [(0, 1 << 2)]
    player.program.run(0.25)
    changes = player.voices[2].level_changes
    assert all(at >= 0.02 for at, level in changes if level > 0)
    assert any(0 < level < 0.35 for _, level in changes)
    assert changes[-1][1] == 0.35
    assert any(not muted for _, muted, _, _ in player.state.samples)
    for index, voice in enumerate(player.voices):
        if index != 2:
            assert all(level == 0 for _, level in voice.level_changes)
    player.screen.show_stems.assert_any_call(1 << 2)


def test_starts_all_voices_while_paused_before_resuming(
    player: SimpleNamespace,
) -> None:
    player.program.run(0.04)
    player.audio.pause.assert_called_once_with()
    player.audio.resume.assert_called_once_with()
    for voice in player.voices:
        voice.play.assert_called_once()
    assert not player.state.paused
    player.screen.show_error.assert_not_called()


def test_tile_removal_fades_and_mutes_without_restarting_loops(
    player: SimpleNamespace,
) -> None:
    player.state.sensor_schedule = [(0, 1 << 2), (0.12, 0)]
    player.program.run(0.25)
    changes = player.voices[2].level_changes
    assert any(at > 0.14 and 0 < level < 0.35 for at, level in changes)
    assert changes[-1][1] == 0
    after_removal = [sample for sample in player.state.samples if sample[0] >= 0.2]
    assert after_removal
    assert all(muted and levels == (0,) * 9 for _, muted, levels, _ in after_removal)
    assert all(all(playing) for _, _, _, playing in after_removal)
    for voice in player.voices:
        voice.play.assert_called_once()
    player.audio.play.assert_called_once()
    player.audio.stop.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["missing", "mismatched", "rate_mismatch", "malformed"]
)
def test_bad_assets_fail_before_enabling_dac_and_release_open_resources(
    player: SimpleNamespace, failure: str
) -> None:
    if failure == "missing":
        player.program.open.side_effect = player.handles[:3] + [FileNotFoundError()]
        opened, loaded, stage = 3, 3, "CHECK STEM 4"
    else:
        if failure == "mismatched":
            player.handles[3] = wav_handle(5)
        elif failure == "rate_mismatch":
            player.handles[3] = wav_handle(rate=22050)
        else:
            player.handles[3] = io.BytesIO(b"bad")
        player.program.open.side_effect = player.handles
        opened, loaded, stage = 4, 3, "CHECK STEM 4"

    assert player.program.run(0.1) is False

    assert not any(player.reset.values)
    player.dac_factory.assert_not_called()
    player.i2c_factory.assert_not_called()
    player.audio.play.assert_not_called()
    player.screen.show_error.assert_called_once_with(stage)
    player.screen.stop.assert_not_called()
    assert all(handle.closed for handle in player.handles[:opened])
    for wave in player.waves[:loaded]:
        wave.deinit.assert_called_once()
    for wave in player.waves[loaded:]:
        wave.deinit.assert_not_called()
    player.reset.deinit.assert_called_once()


@pytest.mark.parametrize("fault", ["short", "temperature"])
def test_speaker_fault_shuts_down_with_reset_low_and_reports_error(
    player: SimpleNamespace, fault: str
) -> None:
    player.state.fault = fault
    player.program.run(0.1)
    assert player.state.events == [("audio.stop", False)]
    assert player.reset.value is False
    assert all(handle.closed for handle in player.handles)
    player.screen.show_error.assert_called_once_with("SPEAKER FAULT")
    player.audio.deinit.assert_called_once()
    player.mixer.deinit.assert_called_once()


def test_bounded_run_stops_and_releases_all_resources(player: SimpleNamespace) -> None:
    player.program.run(0.1)
    elapsed = player.state.now - player.state.started
    assert 0.1 <= elapsed <= 0.13
    assert player.state.events == [("audio.stop", False)]
    for resource in (
        player.audio,
        player.mixer,
        player.pot,
        player.i2c,
        player.reset,
        *player.waves,
        *player.sensors,
    ):
        resource.deinit.assert_called_once()
    assert all(handle.closed for handle in player.handles)
    player.screen.stop.assert_called_once()
    player.screen.close.assert_called_once()
    player.screen.show_error.assert_not_called()


def test_default_unbounded_run_continues_until_a_later_fault(
    player: SimpleNamespace,
) -> None:
    player.state.fault = "short"
    player.state.fault_at = 0.05
    player.program.run()
    assert 1 <= player.state.now - player.state.started < 1.1
    player.screen.show_error.assert_called_once_with("SPEAKER FAULT")
    assert player.state.events == [("audio.stop", False)]


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_is_rejected_before_hardware_setup(
    player: SimpleNamespace, duration: float
) -> None:
    with pytest.raises(ValueError, match="Duration"):
        player.program.run(duration)
    player.pin_factory.assert_not_called()
    player.program.open.assert_not_called()


def test_bank_path_and_sample_rate_configure_the_mixer_and_dac(
    player: SimpleNamespace,
) -> None:
    handles = [wav_handle(rate=22050) for _ in range(9)]
    player.program.open.side_effect = handles
    assert (
        player.program.run(
            0.04,
            audio_root="/beats/block-party/",
            beat_title="BLOCK PARTY / 140 BPM",
        )
        is False
    )
    assert [call.args[0] for call in player.program.open.call_args_list] == [
        "/beats/block-party/stem%d_%s.wav" % (index + 1, name)
        for index, name in enumerate(player.program.STEM_NAMES)
    ]
    player.screen_factory.assert_called_once_with(
        beat_title="BLOCK PARTY / 140 BPM", instruments=None
    )
    player.dac.configure_clocks.assert_called_once_with(sample_rate=22050, bit_depth=16)
    settings = player.mixer_factory.call_args.kwargs
    assert settings["sample_rate"] == 22050
    assert settings["buffer_size"] == 8192
    assert all(handle.closed for handle in handles)


def test_switch_uses_raw_volume_then_fades_mutes_and_releases_resources(
    player: SimpleNamespace,
) -> None:
    player.state.sensor_schedule = [(0, 1)]
    controller = beat_control.BeatControl(hold_seconds=0.08)
    controller.update = Mock(wraps=controller.update)

    assert player.program.run(0.3, beat_control=controller) is True

    assert all(call.args[0] == 100 for call in controller.update.call_args_list)
    assert player.knob_reader.call_count == controller.update.call_count + 1
    changes = player.voices[0].level_changes
    fade = [(at, level) for at, level in changes if at >= 0.08 - 1e-9]
    assert len(fade) >= 3
    assert any(0 < level < 0.35 for _, level in fade)
    assert fade[-1][1] == 0
    assert fade[-1][0] - fade[0][0] >= 0.02 - 1e-9
    assert player.dac.speaker_mute
    assert player.state.events == [("audio.stop", False)]
    assert all(handle.closed for handle in player.handles)
    for resource in (
        player.audio,
        player.mixer,
        player.pot,
        player.i2c,
        player.reset,
        *player.sensors,
        *player.waves,
    ):
        resource.deinit.assert_called_once()
    player.screen.show_switch.assert_any_call(1)
    player.screen.close.assert_called_once()
    player.screen.show_error.assert_not_called()


def test_switch_latch_stays_disarmed_when_the_next_bank_starts_at_full_knob(
    player: SimpleNamespace,
) -> None:
    controller = beat_control.BeatControl(hold_seconds=0.08)
    assert player.program.run(0.3, beat_control=controller) is True
    player.program.open.side_effect = [wav_handle() for _ in range(9)]
    player.program.audiocore.WaveFile.side_effect = [Mock() for _ in range(9)]
    player.state.started = None

    assert player.program.run(0.35, beat_control=controller) is False

    assert controller.remaining is None
    player.screen.show_error.assert_not_called()


def test_state_report_identifies_the_current_beat(
    player: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    player.program.supervisor.runtime.serial_connected = True
    player.program.run(0.04, beat_title="BLOCK PARTY / 140 BPM")
    reports = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert reports[0]["beat_title"] == "BLOCK PARTY / 140 BPM"
    assert reports[0]["speaker_gain_db"] == 12
    assert reports[0]["speaker_volume_db"] == 0


def test_boot_starts_library_os_and_disables_autoreload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=False)
    runtime = SimpleNamespace(autoreload=True)
    monkeypatch.setitem(sys.modules, "shaggy_os", SimpleNamespace(run=run))
    monkeypatch.setitem(sys.modules, "supervisor", SimpleNamespace(runtime=runtime))

    runpy.run_path(str(Path(__file__).parents[1] / "firmware" / "code.py"))

    run.assert_called_once_with()
    assert runtime.autoreload is False


@pytest.mark.parametrize("rate", [22050, 44100])
def test_sd_playback_retains_separate_wave_buffers_and_shared_screen(
    player: SimpleNamespace, rate: int
) -> None:
    timeline = Mock()
    timeline.attach_mock(player.screen.set_streaming, "streaming")
    timeline.attach_mock(player.audio.play, "play")
    timeline.attach_mock(player.audio.stop, "stop")
    timeline.attach_mock(player.audio.deinit, "deinit")
    player.program.open.side_effect = [wav_handle(rate=rate) for _ in range(9)]
    assert (
        player.program.run(
            0.05,
            audio_root="/sd/shaggy/library/house",
            source="SD",
            screen=player.screen,
            raise_errors=True,
        )
        is False
    )
    calls = player.program.audiocore.WaveFile.call_args_list
    buffers = [call.args[1] for call in calls]
    assert len(buffers) == 9
    assert len({id(buffer) for buffer in buffers}) == 9
    assert all(
        isinstance(buffer, bytearray) and len(buffer) == 1024 for buffer in buffers
    )
    assert all(len(call.args) == 2 and not call.kwargs for call in calls)
    assert player.mixer_factory.call_args.kwargs["buffer_size"] == (
        8192 if rate == 22050 else 16384
    )
    assert player.mixer_factory.call_args.kwargs["sample_rate"] == rate
    player.screen_factory.assert_not_called()
    player.screen.close.assert_not_called()
    player.screen.stop.assert_called_once()
    assert timeline.mock_calls == [
        call.streaming(True),
        call.play(player.mixer),
        call.stop(),
        call.deinit(),
        call.streaming(False),
    ]


def test_asset_error_propagates_after_audio_cleanup_without_closing_shared_screen(
    player: SimpleNamespace,
) -> None:
    player.program.open.side_effect = player.handles[:3] + [OSError("card removed")]
    with pytest.raises(RuntimeError, match="CHECK STEM 4: card removed"):
        player.program.run(0.1, screen=player.screen, source="SD", raise_errors=True)
    assert all(handle.closed for handle in player.handles[:3])
    for wave in player.waves[:3]:
        wave.deinit.assert_called_once()
    player.reset.deinit.assert_called_once()
    assert player.reset.value is False
    player.audio.play.assert_not_called()
    player.screen.show_error.assert_called_once_with("CHECK STEM 4")
    player.screen.close.assert_not_called()
    player.screen.set_streaming.assert_called_once_with(False)


def test_sd_streaming_setup_failure_stops_audio_before_restoring_display_mode(
    player: SimpleNamespace,
) -> None:
    timeline = Mock()
    timeline.attach_mock(player.screen.set_streaming, "streaming")
    timeline.attach_mock(player.audio.stop, "stop")
    timeline.attach_mock(player.audio.deinit, "deinit")
    player.screen.set_streaming.side_effect = [
        RuntimeError("background refresh failed"),
        None,
    ]
    with pytest.raises(RuntimeError, match="AUDIO ERROR: background refresh failed"):
        player.program.run(0.1, screen=player.screen, source="SD", raise_errors=True)
    player.audio.play.assert_not_called()
    assert timeline.mock_calls == [
        call.streaming(True),
        call.stop(),
        call.deinit(),
        call.streaming(False),
    ]
    assert all(handle.closed for handle in player.handles)
    player.mixer.deinit.assert_called_once()
    player.reset.deinit.assert_called_once()


def test_sd_streaming_cleanup_failure_still_releases_audio_files_and_pins(
    player: SimpleNamespace,
) -> None:
    def set_streaming(enabled: bool) -> None:
        if not enabled:
            raise RuntimeError("display mode cleanup failed")

    player.screen.set_streaming.side_effect = set_streaming
    with pytest.raises(RuntimeError, match="display mode cleanup failed"):
        player.program.run(0.05, screen=player.screen, source="SD", raise_errors=True)
    player.audio.stop.assert_called_once()
    player.audio.deinit.assert_called_once()
    for resource in (
        player.mixer,
        player.pot,
        player.i2c,
        player.reset,
        *player.waves,
        *player.sensors,
    ):
        resource.deinit.assert_called_once()
    assert all(handle.closed for handle in player.handles)
    assert player.reset.value is False


def test_library_browsing_pauses_and_mutes_until_tile_confirms_selection(
    player: SimpleNamespace,
) -> None:
    control = library_control.LibraryControl(
        2, hold_seconds=0.03, debounce_seconds=0.02
    )
    player.state.sensor_schedule = [(0, 0), (0.14, 1)]

    def read_knob(pot: object) -> int:
        elapsed = (
            0
            if player.state.started is None
            else player.state.now - player.state.started
        )
        return 0 if elapsed < 0.06 else 65535

    player.knob_reader.side_effect = read_knob
    banks = [{"title": "HOUSE"}, {"title": "NEON RUSH"}]
    assert (
        player.program.run(
            0.3,
            screen=player.screen,
            library_control=control,
            banks=banks,
            source="SD",
            raise_errors=True,
        )
        is True
    )
    assert control.selected_index == 1
    assert not control.browsing
    assert player.audio.pause.call_count == 2
    player.audio.resume.assert_called_once()
    player.screen.show_library.assert_any_call(banks, 1, source="SD")
    assert all(muted for _, muted, _, _ in player.state.samples)
    assert player.dac.speaker_mute
    player.audio.stop.assert_called_once()
    assert all(handle.closed for handle in player.handles)
    player.screen.close.assert_not_called()


@pytest.mark.parametrize("with_library", [True, False])
def test_new_bank_stays_muted_while_knob_remains_above_zero(
    player: SimpleNamespace, with_library: bool
) -> None:
    player.state.sensor_schedule = [(0, 1)]
    control = library_control.LibraryControl(2) if with_library else None
    player.program.run(
        0.2,
        screen=player.screen,
        library_control=control,
        banks=[{}, {}] if with_library else None,
        wait_for_zero=True,
        raise_errors=True,
    )
    assert all(muted for _, muted, _, _ in player.state.samples)
    assert player.dac.dac_volume == -40
    player.screen.show_takeover.assert_called_with(True)
    assert all(
        call.args == (True,) for call in player.screen.show_takeover.call_args_list
    )
    player.screen.show_library.assert_not_called()


@pytest.mark.parametrize("with_library", [True, False])
def test_zero_takeover_requires_knob_reset_before_volume_can_rise(
    player: SimpleNamespace, with_library: bool
) -> None:
    player.state.sensor_schedule = [(0, 1)]
    control = library_control.LibraryControl(2) if with_library else None

    def read_knob(pot: object) -> int:
        elapsed = (
            0
            if player.state.started is None
            else player.state.now - player.state.started
        )
        return 0 if 0.08 <= elapsed < 0.12 else 65535

    player.knob_reader.side_effect = read_knob
    player.program.run(
        0.4,
        screen=player.screen,
        library_control=control,
        banks=[{}, {}] if with_library else None,
        wait_for_zero=True,
        raise_errors=True,
    )
    early = [muted for at, muted, _, _ in player.state.samples if at < 0.08]
    late = [muted for at, muted, _, _ in player.state.samples if at > 0.2]
    assert early and all(early)
    assert late and not all(late)
    player.screen.show_takeover.assert_any_call(True)
    player.screen.show_takeover.assert_any_call(False)
    player.screen.show_takeover.assert_called_with(False)
    assert player.dac.dac_volume == 0


@pytest.mark.parametrize(
    "failed_step",
    [
        "audio_stop",
        "audio_deinit",
        "mixer_deinit",
        "wave_deinit",
        "file_close",
        "sensor_deinit",
        "pot_deinit",
        "i2c_deinit",
        "screen_stop",
        "screen_close",
    ],
)
def test_cleanup_failure_releases_other_resources(
    player: SimpleNamespace, failed_step: str
) -> None:
    handle = Mock(wraps=player.handles[0])
    player.program.open.side_effect = [handle, *player.handles[1:]]
    failure = OSError("injected " + failed_step)
    actions = {
        "audio_stop": player.audio.stop,
        "audio_deinit": player.audio.deinit,
        "mixer_deinit": player.mixer.deinit,
        "wave_deinit": player.waves[0].deinit,
        "file_close": handle.close,
        "sensor_deinit": player.sensors[0].deinit,
        "pot_deinit": player.pot.deinit,
        "i2c_deinit": player.i2c.deinit,
        "screen_stop": player.screen.stop,
        "screen_close": player.screen.close,
    }
    actions[failed_step].side_effect = failure

    with pytest.raises(RuntimeError, match="injected " + failed_step):
        player.program.run(0.05, source="SD", raise_errors=True)

    assert player.reset.value is False
    for resource in (
        player.audio,
        player.mixer,
        player.pot,
        player.i2c,
        player.reset,
        *player.waves,
        *player.sensors,
    ):
        resource.deinit.assert_called_once()
    player.audio.stop.assert_called_once()
    player.screen.close.assert_called_once()
    player.screen.set_streaming.assert_any_call(False)
    handle.close.assert_called_once()
    assert all(handle.closed for handle in player.handles[1:])


def test_cleanup_failure_preserves_original_playback_error(
    player: SimpleNamespace,
) -> None:
    player.state.fault = "short"
    player.audio.stop.side_effect = OSError("audio stop failed")
    player.screen.show_error.side_effect = OSError("display failed")

    with pytest.raises(RuntimeError, match="SPEAKER FAULT: DAC reports"):
        player.program.run(0.05, source="SD", raise_errors=True)

    player.reset.deinit.assert_called_once()
    player.i2c.deinit.assert_called_once()
    player.screen.close.assert_called_once()
    assert all(handle.closed for handle in player.handles)


def test_switch_cleanup_failure_prevents_next_bank_and_propagates(
    player: SimpleNamespace,
) -> None:
    controller = beat_control.BeatControl(hold_seconds=0.02)
    player.audio.stop.side_effect = OSError("audio stop failed")

    with pytest.raises(RuntimeError, match="audio stop failed"):
        player.program.run(0.1, beat_control=controller, raise_errors=True)

    player.reset.deinit.assert_called_once()
    assert all(handle.closed for handle in player.handles)


def test_cleanup_failure_without_raise_errors_returns_false(
    player: SimpleNamespace,
) -> None:
    controller = beat_control.BeatControl(hold_seconds=0.02)
    player.audio.stop.side_effect = OSError("audio stop failed")

    assert player.program.run(0.1, beat_control=controller) is False
    player.reset.deinit.assert_called_once()
    assert all(handle.closed for handle in player.handles)


def test_interrupt_survives_cleanup_error_and_releases_resources(
    player: SimpleNamespace,
) -> None:
    player.screen.update.side_effect = KeyboardInterrupt()
    player.audio.stop.side_effect = OSError("audio stop failed")

    with pytest.raises(KeyboardInterrupt):
        player.program.run(0.05, source="SD", raise_errors=True)

    player.reset.deinit.assert_called_once()
    player.i2c.deinit.assert_called_once()
    assert all(handle.closed for handle in player.handles)


@pytest.mark.parametrize("rate", [22050, 44100])
def test_flash_playback_uses_short_buffers_and_manual_display(
    player: SimpleNamespace, rate: int
) -> None:
    player.program.open.side_effect = [wav_handle(rate=rate) for _ in range(9)]
    player.program.run(0.05, source="FLASH", raise_errors=True)
    assert player.mixer_factory.call_args.kwargs["buffer_size"] == (
        2048 if rate == 22050 else 4096
    )
    player.screen.set_streaming.assert_not_called()
    for voice in player.voices:
        voice.play.assert_called_once()
