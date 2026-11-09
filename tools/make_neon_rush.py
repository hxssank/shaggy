import json
import math
import wave
from collections import deque
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
BPM = 138
BEAT_SECONDS = 60 / BPM
FRAMES = round(SAMPLE_RATE * BEAT_SECONDS * 8)
RUNTIME_LEVEL = 0.35
OUTPUT = Path(__file__).resolve().parents[1] / "audio" / "neon-rush"
NAMES = ("kick", "clap", "closedhat", "openhat", "bass", "chords", "perc", "arp", "pad")


def pitch(note: float) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def tone_time(seconds: float) -> np.ndarray:
    return np.arange(round(seconds * SAMPLE_RATE), dtype=np.float64) / SAMPLE_RATE


def filter_audio(
    audio: np.ndarray, low: float = 0, high: float = 20000, circular: bool = False
) -> np.ndarray:
    size = len(audio) if circular else 1 << (len(audio) - 1).bit_length()
    frequencies = np.fft.rfftfreq(size, 1 / SAMPLE_RATE)
    response = 1 / np.sqrt(1 + (frequencies / high) ** 8)
    if low:
        response *= (frequencies / low) ** 2 / np.sqrt(1 + (frequencies / low) ** 4)
    return np.fft.irfft(np.fft.rfft(audio, size) * response, size)[: len(audio)]


def envelope(
    time: np.ndarray, attack: float, release: float, decay: float = 0
) -> np.ndarray:
    result = np.minimum(time / attack, 1)
    result *= np.minimum((time[-1] - time) / release, 1)
    if decay:
        result *= np.exp(-time / decay)
    return np.maximum(result, 0)


def place(loop: np.ndarray, sound: np.ndarray, beat: float, gain: float = 1) -> None:
    start = round(beat * BEAT_SECONDS * SAMPLE_RATE)
    indices = (start + np.arange(len(sound))) % FRAMES
    np.add.at(loop, indices, sound * gain)


def saw(
    note: float,
    time: np.ndarray,
    cutoff: float,
    detunes: tuple[float, ...] = (-5, 0, 5),
) -> np.ndarray:
    sound = np.zeros(len(time))
    for voice, cents in enumerate(detunes):
        frequency = pitch(note + cents / 100)
        harmonics = min(48, int(18000 / frequency))
        phase = 0.11 * voice
        for harmonic in range(1, harmonics + 1):
            amplitude = (
                1 / harmonic / math.sqrt(1 + (frequency * harmonic / cutoff) ** 6)
            )
            sound += amplitude * np.sin(
                2 * np.pi * harmonic * (frequency * time + phase)
            )
    return sound * (0.62 / len(detunes))


def ambience(sound: np.ndarray, amount: float) -> np.ndarray:
    result = sound.copy()
    dark = filter_audio(sound, low=260, high=3900, circular=True)
    for delay, gain in ((0.037, 0.32), (0.061, -0.23), (0.097, 0.19), (0.149, 0.13)):
        result += np.roll(dark, round(delay * SAMPLE_RATE)) * gain * amount
    return result


def compose() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(13820261029)
    stems = {name: np.zeros(FRAMES) for name in NAMES}

    t = tone_time(0.39)
    frequency = 46.25 + 125 * np.exp(-t / 0.018) + 19 * np.exp(-t / 0.075)
    phase = 2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE
    body = np.sin(phase) * np.exp(-t / 0.125)
    body += 0.15 * np.sin(phase * 2) * np.exp(-t / 0.033)
    click = filter_audio(rng.standard_normal(len(t)), low=1900, high=6400)
    kick = np.tanh(1.65 * body) * envelope(t, 0.0007, 0.025)
    kick += 0.095 * click * np.exp(-t / 0.004) * envelope(t, 0.0002, 0.02)
    for beat in range(8):
        place(stems["kick"], kick, beat)

    t = tone_time(0.24)
    noise = filter_audio(rng.standard_normal(len(t)), low=650, high=7400)
    bursts = np.zeros(len(t))
    for delay, strength in ((0, 0.6), (0.011, 0.8), (0.022, 1.0)):
        shifted = np.maximum(t - delay, 0)
        bursts += strength * (t >= delay) * np.exp(-shifted / 0.017)
    clap = noise * (bursts + 0.3 * np.exp(-t / 0.065))
    clap += 0.08 * np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.055)
    clap *= envelope(t, 0.0004, 0.025)
    for beat in (1, 3, 5, 7):
        place(stems["clap"], clap, beat, 0.96 if beat % 4 == 1 else 1)

    def hat(seconds: float, decay: float) -> np.ndarray:
        time = tone_time(seconds)
        noise = rng.standard_normal(len(time))
        for frequency in (3181, 4553, 6137, 7973, 10313):
            noise += 0.17 * np.sin(2 * np.pi * frequency * time)
        noise = filter_audio(noise, low=3300, high=10100)
        return noise * envelope(time, 0.0007, 0.012, decay)

    for step in range(32):
        if step % 2 == 0 or step in (7, 15, 23, 29, 31):
            gain = 0.8 if step % 4 == 0 else (0.55 if step % 2 == 0 else 0.25)
            place(stems["closedhat"], hat(0.075, 0.021), step / 4, gain)
    for beat in range(8):
        place(stems["openhat"], hat(0.19, 0.069), beat + 0.5, 0.85 + 0.05 * (beat % 3))

    bass_notes = [30, 42, 30, 37, 30, 42, 40, 37, 38, 50, 38, 45, 38, 50, 49, 45]
    for index, note in enumerate(bass_notes):
        for offset, gain in ((0.25, 1.0), (0.5, 0.72)):
            beat = index / 2 + offset / 2
            t = tone_time(0.17)
            fundamental = np.sin(2 * np.pi * pitch(note) * t)
            sound = 0.78 * fundamental + 0.5 * saw(note + 12, t, 1000, (-2, 2))
            sound = np.tanh(sound * 1.5) * envelope(t, 0.003, 0.025, 0.10)
            place(stems["bass"], sound, beat, gain)

    for beat, notes in (
        (0.5, (54, 57, 61)),
        (2.5, (54, 57, 61)),
        (4.5, (50, 54, 57)),
        (6.5, (50, 54, 57)),
    ):
        t = tone_time(0.36)
        chord = sum(saw(note + 12, t, 1450, (-4, 4)) for note in notes)
        place(stems["chords"], chord * envelope(t, 0.008, 0.08, 0.15), beat)
    stems["chords"] = ambience(stems["chords"], 0.6)

    for beat, frequency in (
        (0.75, 760),
        (1.75, 550),
        (2.75, 760),
        (3.5, 430),
        (4.75, 760),
        (5.75, 550),
        (6.75, 760),
        (7.75, 430),
    ):
        t = tone_time(0.10)
        click = np.sin(2 * np.pi * frequency * t)
        click += 0.4 * np.sin(2 * np.pi * frequency * 2.31 * t)
        click += 0.25 * filter_audio(rng.standard_normal(len(t)), low=1300, high=5200)
        place(stems["perc"], click * envelope(t, 0.001, 0.02, 0.023), beat)

    steps = (0, 2, 3, 6, 8, 10, 11, 14, 16, 18, 19, 22, 24, 26, 27, 30)
    notes = (66, 69, 73, 76, 73, 71, 69, 73, 74, 73, 69, 66, 69, 71, 73, 69)
    for index, (step, note) in enumerate(zip(steps, notes, strict=True)):
        t = tone_time(0.165 if index % 4 != 3 else 0.22)
        sound = saw(note, t, 3400, (-9, -5, -2, 0, 2, 5, 9))
        sound += 0.10 * np.sin(2 * np.pi * pitch(note - 12) * t)
        sound = np.tanh(sound * 1.35) * envelope(t, 0.004, 0.034)
        place(stems["arp"], sound, step / 4, 1 if step % 4 == 0 else 0.87)
    lead = stems["arp"].copy()
    for beats, gain in ((0.75, 0.19), (1.5, 0.095), (2.25, 0.04)):
        stems["arp"] += np.roll(lead, round(beats * BEAT_SECONDS * SAMPLE_RATE)) * gain
    stems["arp"] = ambience(stems["arp"], 0.38)

    for beat, notes in ((0, (54, 57, 61, 68)), (4, (50, 54, 57, 61))):
        t = tone_time(BEAT_SECONDS * 5.2)
        pad = sum(saw(note + 12, t, 950, (-7, 7)) for note in notes)
        pad *= envelope(t, 0.45, 0.65)
        pad *= 0.85 + 0.15 * np.sin(2 * np.pi * 0.43 * t)
        place(stems["pad"], pad, beat)
    stems["pad"] = ambience(stems["pad"], 1)

    phase = (np.arange(FRAMES) * 8 / FRAMES % 1) * BEAT_SECONDS
    pre_duck = np.clip((phase - (BEAT_SECONDS - 0.008)) / 0.008, 0, 1)
    for name, depth in (("bass", 0.72), ("arp", 0.30), ("chords", 0.48), ("pad", 0.55)):
        stems[name] *= 1 - depth * np.maximum(np.exp(-phase / 0.065), pre_duck)
    peaks = (0.93, 0.37, 0.115, 0.18, 0.56, 0.20, 0.15, 0.60, 0.12)
    for name, peak in zip(NAMES, peaks, strict=True):
        stems[name] -= np.mean(stems[name])
        stems[name] *= peak / np.max(np.abs(stems[name]))
    return stems


def local_absolute_max(audio: np.ndarray, radius: int) -> np.ndarray:
    padded = np.pad(np.abs(audio), (radius, radius), mode="wrap")
    width = radius * 2 + 1
    queue: deque[int] = deque()
    result = np.empty(len(audio))
    for index, value in enumerate(padded):
        while queue and queue[0] <= index - width:
            queue.popleft()
        while queue and padded[queue[-1]] <= value:
            queue.pop()
        queue.append(index)
        if index >= width - 1:
            result[index - width + 1] = padded[queue[0]]
    return result


def write_wav(path: Path, audio: np.ndarray) -> np.ndarray:
    pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm.tobytes())
    return pcm.astype(np.float64) / 32768


def arrange_preview(stems: dict[str, np.ndarray]) -> np.ndarray:
    sections = (
        (1, 0, 0.6, 0, 0, 0.4, 0.4, 0.55, 1),
        (1, 0.7, 1, 0.7, 0.8, 0.6, 0.6, 0.8, 1),
        (1, 1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 1, 1, 1, 1, 0.7, 1, 1),
        (1, 1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 0.8, 1, 1, 0.8, 1, 1, 1),
        (0, 0, 0.3, 0, 0, 1, 0.2, 0.55, 1),
        (0.6, 0.8, 1, 0.3, 0.6, 1, 1, 0.85, 1),
        (1, 1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 1, 1, 1, 0.8, 0.7, 1, 1),
        (1, 1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 1, 1, 1, 1, 1, 1, 1),
    )
    pans = (0, 0, -0.26, 0.25, 0, -0.20, 0.31, 0, -0.12)
    preview = np.zeros((FRAMES * len(sections), 2))
    ramp = np.linspace(0, 1, round(SAMPLE_RATE * 0.008))
    for index, levels in enumerate(sections):
        for stem_index, name in enumerate(NAMES):
            gain = np.full(FRAMES, levels[stem_index])
            before = sections[index - 1][stem_index] if index else 0
            gain[: len(ramp)] = before + (levels[stem_index] - before) * ramp
            sound = stems[name] * gain * RUNTIME_LEVEL
            angle = (pans[stem_index] + 1) * np.pi / 4
            stereo = np.column_stack((sound * np.cos(angle), sound * np.sin(angle)))
            preview[index * FRAMES : (index + 1) * FRAMES] += stereo
    preview = np.tanh(preview * 1.6)
    preview *= 0.92 / np.max(np.abs(preview))
    fade = round(SAMPLE_RATE * 0.35)
    preview[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return preview


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stems = compose()
    upper_bound = sum(local_absolute_max(sound, 1024) for sound in stems.values())
    global_gain = min(1, 0.82 / (np.max(upper_bound) * RUNTIME_LEVEL))
    metrics = {}
    for index, (name, sound) in enumerate(stems.items()):
        pcm = write_wav(
            OUTPUT / ("stem%d_%s.wav" % (index + 1, name)), sound * global_gain
        )
        stems[name] = pcm
        metrics[name] = {
            "peak": float(np.max(np.abs(pcm))),
            "rms": float(np.sqrt(np.mean(pcm**2))),
            "wrap_step": float(abs(pcm[0] - pcm[-1])),
            "largest_adjacent_step": float(np.max(np.abs(np.diff(pcm)))),
        }
    aligned = sum(stems.values()) * RUNTIME_LEVEL
    bound = float(
        np.max(sum(local_absolute_max(sound, 1024) for sound in stems.values()))
        * RUNTIME_LEVEL
    )
    assert bound <= 0.85
    assert all(values["peak"] <= 0.95 for values in metrics.values())
    preview = arrange_preview(stems)
    write_wav(OUTPUT / "neon-rush-preview.wav", preview)
    report = {
        "title": "Neon Rush",
        "bpm": BPM,
        "key": "F# minor",
        "bars": 2,
        "frames": FRAMES,
        "seconds": FRAMES / SAMPLE_RATE,
        "runtime_stem_level": RUNTIME_LEVEL,
        "aligned_mix_peak": float(np.max(np.abs(aligned))),
        "independent_offset_1024_sample_absolute_upper_bound": bound,
        "global_gain": float(global_gain),
        "preview_seconds": len(preview) / SAMPLE_RATE,
        "preview_peak": float(np.max(np.abs(preview))),
        "stems": metrics,
    }
    (OUTPUT / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
