import json
import wave
from pathlib import Path

import numpy as np

RATE = 44100
BPM = 140
BEAT = 60 / BPM
FRAMES = round(RATE * BEAT * 8)
NAMES = (
    "kick",
    "clap",
    "closedhat",
    "openhat",
    "bass",
    "chords",
    "perc",
    "arp",
    "pad",
)
OUTPUT = Path(__file__).resolve().parents[1] / "audio" / "block-party"


def envelope(
    t: np.ndarray, duration: float, attack: float, release: float
) -> np.ndarray:
    return np.minimum(t / attack, 1) * np.clip((duration - t) / release, 0, 1)


def noise(rng: np.random.Generator, size: int, low: float, high: float) -> np.ndarray:
    frequencies = np.fft.rfftfreq(size, 1 / RATE)
    response = 1 / (1 + (low / np.maximum(frequencies, 1)) ** 8)
    response /= 1 + (frequencies / high) ** 12
    response[0] = 0
    result = np.fft.irfft(np.fft.rfft(rng.standard_normal(size)) * response, n=size)
    return result / max(float(np.std(result)), 1e-9)


def add(stem: np.ndarray, beat: float, sound: np.ndarray, gain: float = 1) -> None:
    start = round(beat * BEAT * RATE) % FRAMES
    end = min(len(sound), FRAMES - start)
    stem[start : start + end] += sound[:end] * gain
    if end < len(sound):
        stem[: len(sound) - end] += sound[end:] * gain


def kick(rng: np.random.Generator) -> np.ndarray:
    duration = 0.25
    t = np.arange(round(duration * RATE)) / RATE
    frequency = 43 + 115 * np.exp(-t / 0.016)
    phase = 2 * np.pi * np.cumsum(frequency) / RATE
    body = np.sin(phase) * np.exp(-t / 0.072)
    click = 0.075 * noise(rng, len(t), 500, 3700) * np.exp(-t / 0.0025)
    return np.tanh(1.4 * (body + click)) * envelope(t, duration, 0.0005, 0.025)


def clap(rng: np.random.Generator) -> np.ndarray:
    duration = 0.23
    t = np.arange(round(duration * RATE)) / RATE
    bursts = np.zeros_like(t)
    for delay, weight in ((0, 1), (0.011, 0.8), (0.024, 0.65)):
        age = np.maximum(t - delay, 0)
        bursts += (t >= delay) * weight * np.exp(-age / 0.027)
    hiss = noise(rng, len(t), 650, 5300) * bursts
    body = 0.55 * np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.04)
    return (hiss + body) * envelope(t, duration, 0.0007, 0.035)


def hat(rng: np.random.Generator, opened: bool = False) -> np.ndarray:
    duration, decay = (0.19, 0.045) if opened else (0.065, 0.014)
    t = np.arange(round(duration * RATE)) / RATE
    metal = noise(rng, len(t), 3300, 6900)
    return metal * np.exp(-t / decay) * envelope(t, duration, 0.0004, 0.015)


def bass(start: float, finish: float, beats: float) -> np.ndarray:
    duration = beats * BEAT
    t = np.arange(round(duration * RATE)) / RATE
    frequency = finish + (start - finish) * np.exp(-t / 0.045)
    phase = 2 * np.pi * np.cumsum(frequency) / RATE
    body = np.sin(phase) + 0.18 * np.sin(2 * phase) + 0.04 * np.sin(3 * phase)
    body = np.tanh(1.65 * body) * np.exp(-t / 1.2)
    return body * envelope(t, duration, 0.004, min(0.055, duration / 3))


def brass(frequencies: tuple[float, ...], beats: float) -> np.ndarray:
    duration = beats * BEAT
    t = np.arange(round(duration * RATE)) / RATE
    sound = np.zeros_like(t)
    for index, frequency in enumerate(frequencies):
        phase = 2 * np.pi * frequency * t + index * 0.23
        sound += np.sin(phase + 1.55 * np.exp(-t / 0.065) * np.sin(2 * phase))
        sound += 0.16 * np.sin(3 * phase)
    return (
        np.tanh(sound * 0.55) * np.exp(-t / 0.14) * envelope(t, duration, 0.003, 0.035)
    )


def percussion(rng: np.random.Generator, frequency: float) -> np.ndarray:
    duration = 0.14
    t = np.arange(round(duration * RATE)) / RATE
    phase = 2 * np.pi * np.cumsum(frequency * (1 + 0.4 * np.exp(-t / 0.008))) / RATE
    body = np.sin(phase) * np.exp(-t / 0.035)
    body += 0.12 * noise(rng, len(t), 600, 2300) * np.exp(-t / 0.006)
    return body * envelope(t, duration, 0.001, 0.025)


def lead(frequency: float, beats: float, bend: float) -> np.ndarray:
    duration = beats * BEAT
    t = np.arange(round(duration * RATE)) / RATE
    pitch = frequency * (
        1 + bend * np.exp(-t / 0.022) + 0.006 * np.sin(2 * np.pi * 6 * t)
    )
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    vowel = 0.6 + 1.5 * np.exp(-t / 0.055)
    sound = np.sin(phase + vowel * np.sin(2 * phase)) + 0.13 * np.sin(3 * phase)
    return np.tanh(sound * 1.3) * envelope(t, duration, 0.004, 0.04) * np.exp(-t / 0.22)


def cyclic_maximum(values: np.ndarray, radius: int) -> np.ndarray:
    width = 2 * radius + 1
    padded = np.pad(values, (radius, radius), mode="wrap")
    padded = np.pad(padded, (0, (-len(padded)) % width))
    blocks = padded.reshape(-1, width)
    prefix = np.maximum.accumulate(blocks, axis=1).ravel()
    suffix = np.maximum.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].ravel()
    return np.maximum(
        suffix[: len(values)], prefix[width - 1 : width - 1 + len(values)]
    )


def compose() -> list[np.ndarray]:
    rng = np.random.default_rng(1402026)
    stems = [np.zeros(FRAMES) for _ in NAMES]
    for beat, gain in (
        (0, 1),
        (1.5, 0.88),
        (3.25, 0.82),
        (4, 1),
        (4.75, 0.75),
        (6.75, 0.85),
        (7.5, 0.9),
    ):
        add(stems[0], beat, kick(rng), gain)
    for beat in (2, 6):
        add(stems[1], beat, clap(rng))
    for step in range(32):
        beat = step / 4
        if 3.5 <= beat < 4 or beat >= 7.5:
            continue
        if step % 2 == 0 or step in (7, 13, 23, 29):
            add(
                stems[2],
                beat + (0.025 if step % 2 else 0),
                hat(rng),
                1 if step % 4 == 0 else 0.67,
            )
    for beginning in (3.5, 7.5):
        for index in range(3):
            add(stems[2], beginning + index / 6, hat(rng), 0.56 + index * 0.13)
    for beat, gain in ((0.5, 0.8), (4.5, 1), (6.5, 0.65)):
        add(stems[3], beat, hat(rng, True), gain)
    d, f, a, c = 36.7081, 43.6535, 55.0, 65.4064
    for beat, start, finish, length in (
        (0, d, d, 1.2),
        (1.5, a, d, 0.8),
        (3.25, f, d, 0.6),
        (4, d, d, 1.1),
        (5.25, a, f, 0.65),
        (6.75, c, a, 0.45),
        (7.5, d * 2, d, 0.55),
    ):
        add(stems[4], beat, bass(start, finish, length))
    for beat, notes, length in (
        (0.75, (146.832, 220.0, 261.626), 0.38),
        (3.0, (174.614, 220.0, 293.665), 0.45),
        (4.75, (146.832, 220.0, 261.626), 0.35),
        (6.5, (174.614, 220.0, 293.665), 0.4),
    ):
        add(stems[5], beat, brass(notes, length))
    for beat, frequency, gain in (
        (0.75, 390, 0.65),
        (2.75, 260, 0.9),
        (3.75, 190, 0.55),
        (5.75, 390, 0.7),
        (7.0, 260, 1),
    ):
        add(stems[6], beat, percussion(rng, frequency), gain)
    for beat, frequency, length, bend in (
        (1, 293.665, 0.27, 0.15),
        (1.75, 349.228, 0.22, -0.12),
        (2.75, 311.127, 0.35, 0.08),
        (5, 391.995, 0.2, 0.13),
        (5.5, 349.228, 0.25, -0.14),
        (6.75, 293.665, 0.32, 0.10),
        (7.25, 220.0, 0.27, 0.25),
    ):
        sound = lead(frequency, length, bend)
        add(stems[7], beat, sound)
        add(stems[7], beat + 0.375, sound, 0.18)
    for beat, reverse in ((0, False), (3.5, True), (7.5, True)):
        duration = 0.5 * BEAT
        t = np.arange(round(duration * RATE)) / RATE
        air = noise(rng, len(t), 220, 1900)
        air += 0.35 * np.sin(2 * np.pi * 146.832 * t)
        shape = envelope(
            t, duration, 0.16 if reverse else 0.005, 0.025 if reverse else 0.16
        )
        add(stems[8], beat, air * shape)
    peaks = (0.78, 0.57, 0.16, 0.15, 0.65, 0.37, 0.25, 0.34, 0.12)
    for index, stem in enumerate(stems):
        stem -= np.mean(stem)
        stem *= peaks[index] / max(float(np.max(np.abs(stem))), 1e-9)
    bound = np.max(sum(cyclic_maximum(np.abs(stem), 1024) for stem in stems)) * 0.35
    if bound > 0.82:
        for stem in stems:
            stem *= 0.82 / bound
    return stems


def write_wav(path: Path, samples: np.ndarray) -> np.ndarray:
    pcm = np.rint(np.clip(samples, -0.95, 0.95) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())
    return pcm.astype(float) / 32768


def arranged_preview(stems: list[np.ndarray]) -> np.ndarray:
    sections = (
        (0, 2, 6, 8),
        (0, 2, 3, 4, 5, 6),
        tuple(range(9)),
        tuple(range(9)),
        tuple(range(9)),
        (1, 2, 5, 8),
        (0, 2, 4, 7, 8),
        tuple(range(9)),
        tuple(range(9)),
        tuple(range(9)),
        (0, 1, 2, 3, 4, 6, 7),
        tuple(range(9)),
    )
    mixed = np.zeros(FRAMES * len(sections))
    ramp = round(0.02 * RATE)
    for index, stem in enumerate(stems):
        gain = np.repeat([float(index in section) for section in sections], FRAMES)
        previous = 0.0
        for loop, section in enumerate(sections):
            target = float(index in section)
            start = loop * FRAMES
            gain[start : start + ramp] = np.linspace(previous, target, ramp)
            previous = target
        mixed += np.tile(stem, len(sections)) * gain * 0.35
    fade = round(0.12 * RATE)
    mixed[-fade:] *= np.linspace(1, 0, fade)
    return mixed


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stems = compose()
    decoded = []
    tracks = []
    for index, (name, stem) in enumerate(zip(NAMES, stems, strict=True)):
        path = OUTPUT / f"stem{index + 1}_{name}.wav"
        samples = write_wav(path, stem)
        decoded.append(samples)
        tracks.append(
            {
                "file": path.name,
                "peak": float(np.max(np.abs(samples))),
                "rms": float(np.sqrt(np.mean(samples**2))),
                "boundary_step": float(abs(samples[0] - samples[-1])),
            }
        )
    mixture = sum(decoded) * 0.35
    offset_bound = sum(cyclic_maximum(np.abs(stem), 1024) for stem in decoded) * 0.35
    preview = write_wav(OUTPUT / "block-party-preview.wav", arranged_preview(decoded))
    metrics = {
        "name": "Block Party",
        "bpm": BPM,
        "bars": 2,
        "sample_rate": RATE,
        "channels": 1,
        "bits": 16,
        "frames_per_stem": FRAMES,
        "loop_seconds": FRAMES / RATE,
        "mix_peak_at_0_35": float(np.max(np.abs(mixture))),
        "conservative_peak_with_1024_sample_offsets": float(np.max(offset_bound)),
        "preview_seconds": len(preview) / RATE,
        "preview_peak": float(np.max(np.abs(preview))),
        "preview_rms": float(np.sqrt(np.mean(preview**2))),
        "stems": tracks,
    }
    assert metrics["conservative_peak_with_1024_sample_offsets"] <= 0.85
    assert max(track["peak"] for track in tracks) <= 0.95
    (OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
