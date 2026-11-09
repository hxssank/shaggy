import gc
import json
import time

import adafruit_tlv320
import analogio
import audiobusio
import audiocore
import audiomixer
import board
import busio
import digitalio
import supervisor
from audio_assets import inspect_wav
from player_display import PlayerDisplay
from stem_control import StemControl
from volume_control import VolumeControl
from volume_test import (
    CONTROL_INTERVAL,
    DISPLAY_FPS,
    MIN_CONTROL_WINDOW,
    SPEAKER_GAIN_DB,
    STEM_LEVEL,
    STEM_NAMES,
    read_knob,
)

SENSOR_PINS = (
    board.GP0,
    board.GP1,
    board.GP2,
    board.GP3,
    board.GP4,
    board.GP5,
    board.GP6,
    board.GP7,
    board.GP8,
)


def run(
    duration_seconds: float | None = None,
    *,
    beat_title: str = "HOUSE / 124 BPM",
    audio_root: str = "",
    beat_control: object | None = None,
    instruments: tuple | None = None,
    screen: object | None = None,
    library_control: object | None = None,
    banks: list | None = None,
    wait_for_zero: bool = False,
    source: str = "INTERNAL",
    raise_errors: bool = False,
) -> bool:
    if duration_seconds is not None and not 0 < duration_seconds < float("inf"):
        raise ValueError("Duration must be finite and positive")
    reset = digitalio.DigitalInOut(board.GP13)
    reset.switch_to_output(value=False)
    owns_screen = screen is None
    i2c = None
    pot = None
    audio = None
    mixer = None
    sensors = []
    handles = []
    waves = []
    wave_buffers = []
    error_message = None
    failure = None
    switched = False
    stage = "SCREEN ERROR"
    try:
        if screen is None:
            screen = PlayerDisplay(beat_title=beat_title, instruments=instruments)
        frames = None
        sample_rate = None
        for index, name in enumerate(STEM_NAMES):
            stage = "CHECK STEM %d" % (index + 1)
            handle = open(
                "%s/stem%d_%s.wav" % (audio_root.rstrip("/"), index + 1, name), "rb"
            )
            handles.append(handle)
            rate, count = inspect_wav(handle)
            if frames is not None and (count != frames or rate != sample_rate):
                raise ValueError("All stems must have the same sample rate and length")
            frames = count
            sample_rate = rate
            if source == "SD":
                wave_buffers.append(bytearray(1024))
                waves.append(audiocore.WaveFile(handle, wave_buffers[-1]))
            else:
                waves.append(audiocore.WaveFile(handle))
        stage = "INPUT ERROR"
        for pin in SENSOR_PINS:
            sensor = digitalio.DigitalInOut(pin)
            sensors.append(sensor)
            sensor.switch_to_input(pull=digitalio.Pull.UP)
        pot = analogio.AnalogIn(board.GP26)
        initial_raw = read_knob(pot)
        volume = VolumeControl(
            initial_raw=0 if wait_for_zero else initial_raw,
            ramp_percent_per_second=500,
        )
        stems = StemControl(stem_level=STEM_LEVEL)
        present = [False] * len(sensors)
        stage = "AUDIO ERROR"
        time.sleep(0.1)
        reset.value = True
        time.sleep(0.1)
        i2c = busio.I2C(scl=board.GP15, sda=board.GP14)
        dac = adafruit_tlv320.TLV320DAC3100(i2c)
        dac.speaker_mute = True
        dac.speaker_output = True
        dac.speaker_mute = True
        dac.headphone_output = False
        dac.speaker_gain = SPEAKER_GAIN_DB
        dac.speaker_volume = 0
        dac.dac_volume = -40
        dac.configure_clocks(sample_rate=sample_rate, bit_depth=16)
        dac.speaker_mute = True
        buffer_size = 8192 if sample_rate == 22050 else 16384
        if source == "FLASH":
            buffer_size //= 4
        mixer = audiomixer.Mixer(
            voice_count=9,
            sample_rate=sample_rate,
            channel_count=1,
            bits_per_sample=16,
            samples_signed=True,
            buffer_size=buffer_size,
        )
        audio = audiobusio.I2SOut(board.GP16, board.GP17, board.GP18)
        for voice in mixer.voice:
            voice.level = 0
        gc.collect()
        if source == "SD":
            screen.set_streaming(True)
        audio.play(mixer)
        audio.pause()
        for index, wave in enumerate(waves):
            mixer.voice[index].play(wave, loop=True)
        audio.resume()
        screen.show_stems(0)
        started = time.monotonic()
        previous = started
        next_frame = started
        next_health = started
        next_report = started
        applied_db = -40.0
        muted = True
        library_selection = None
        takeover_enabled = wait_for_zero or library_control is not None
        frame_interval = 1 / (12 if source == "SD" else DISPLAY_FPS)
        if library_control is not None:
            screen.show_library_hold(None)
        if takeover_enabled:
            screen.show_takeover(wait_for_zero)
        print("Shaggy ready.", beat_title, "Place a tile. Turn the knob for volume.")
        while duration_seconds is None or time.monotonic() - started < duration_seconds:
            now = time.monotonic()
            elapsed = now - previous
            previous = now
            for index, sensor in enumerate(sensors):
                present[index] = not sensor.value
            stems.update(present, elapsed)
            for index, voice in enumerate(mixer.voice):
                if stems.changed_mask & (1 << index):
                    voice.level = stems.levels[index]
            raw = read_knob(pot)
            raw_percent = round(raw * 100 / 65535)
            if library_control is not None:
                event = library_control.update(raw_percent, stems.active_mask, elapsed)
                if event == "open":
                    dac.speaker_mute = True
                    muted = True
                    audio.pause()
                    library_selection = None
                if library_control.browsing:
                    selected = library_control.selected_index
                    if selected != library_selection:
                        screen.show_library(banks, selected, source=source)
                        library_selection = selected
                        print(json.dumps({"event": "library", "selected": selected}))
                    time.sleep(CONTROL_INTERVAL)
                    continue
                if event == "select":
                    dac.speaker_mute = True
                    switched = True
                    break
                screen.show_library_hold(library_control.remaining)
            if wait_for_zero and raw_percent <= 1:
                wait_for_zero = False
                volume = VolumeControl(initial_raw=0, ramp_percent_per_second=500)
            if takeover_enabled:
                screen.show_takeover(wait_for_zero)
            volume.update(0 if wait_for_zero else raw, elapsed)
            requested_db = round(volume.db * 2) / 2
            if requested_db != applied_db:
                dac.dac_volume = requested_db
                applied_db = dac.dac_volume
            requested_mute = volume.muted or not any(stems.levels)
            if requested_mute != muted:
                dac.speaker_mute = requested_mute
                muted = requested_mute
            if now >= next_health:
                if dac.speaker_shorted or dac.overtemperature:
                    stage = "SPEAKER FAULT"
                    raise RuntimeError("DAC reports short or overtemperature")
                if not all(voice.playing for voice in mixer.voice):
                    stage = "PLAYBACK STOPPED"
                    raise RuntimeError("A looping stem stopped")
                next_health = now + 1
            if beat_control is not None and beat_control.update(
                round(raw * 100 / 65535), elapsed
            ):
                fade_started = time.monotonic()
                while True:
                    gain = max(0.0, 1 - (time.monotonic() - fade_started) / 0.02)
                    for index, voice in enumerate(mixer.voice):
                        voice.level = stems.levels[index] * gain
                    dac.dac_volume = max(-63, applied_db - 50 * (1 - gain))
                    if gain == 0:
                        break
                    time.sleep(CONTROL_INTERVAL)
                dac.speaker_mute = True
                switched = True
                break
            if now >= next_frame and not stems.changed_mask:
                stage = "SCREEN ERROR"
                screen.show_stems(stems.active_mask)
                screen.show_switch(
                    None if beat_control is None else beat_control.remaining
                )
                screen.animate(now - started)
                screen.update(volume, applied_db)
                next_frame = max(
                    now + frame_interval, time.monotonic() + MIN_CONTROL_WINDOW
                )
                stage = "AUDIO ERROR"
            if now >= next_report:
                if supervisor.runtime.serial_connected:
                    print(
                        json.dumps(
                            {
                                "event": "state",
                                "beat_title": beat_title,
                                "source": source,
                                "sample_rate": sample_rate,
                                "waiting_for_zero": wait_for_zero,
                                "seconds": int(now - started),
                                "tiles": stems.active_mask,
                                "stems": stems.active_count,
                                "volume": volume.percent,
                                "muted": muted,
                                "dac_db": applied_db,
                                "speaker_gain_db": dac.speaker_gain,
                                "speaker_volume_db": dac.speaker_volume,
                                "overflow_raw": dac._page0._read_register(0x27),
                            }
                        )
                    )
                next_report = now + 2
            time.sleep(CONTROL_INTERVAL)
    except Exception as error:
        failure = error
        error_message = stage
        print("Shaggy stopped:", stage, str(error))
    finally:
        def cleanup(action: object, message: str = "CLEANUP ERROR") -> None:
            nonlocal failure, error_message
            try:
                action()
            except Exception as error:
                if failure is None:
                    failure = error
                    error_message = message

        cleanup(lambda: setattr(reset, "value", False))
        if audio is not None:
            cleanup(audio.stop)
            cleanup(audio.deinit)
        if source == "SD" and screen is not None:
            cleanup(lambda: screen.set_streaming(False), "SCREEN ERROR")
        if mixer is not None:
            cleanup(mixer.deinit)
        for wave in waves:
            cleanup(wave.deinit)
        for handle in handles:
            cleanup(handle.close)
        for sensor in sensors:
            cleanup(sensor.deinit)
        if pot is not None:
            cleanup(pot.deinit)
        if i2c is not None:
            cleanup(i2c.deinit)
        if screen is not None:
            if error_message is None:
                cleanup(screen.stop, "SCREEN ERROR")
            else:
                cleanup(lambda: screen.show_error(error_message), "SCREEN ERROR")
            if owns_screen:
                cleanup(screen.close, "SCREEN ERROR")
        cleanup(reset.deinit)
    if failure is not None and raise_errors:
        raise RuntimeError("%s: %s" % (error_message, failure))
    return switched and failure is None


if __name__ == "__main__":
    run()
