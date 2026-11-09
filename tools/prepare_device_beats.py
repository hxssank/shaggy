import json
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RATE = 22050


def prepare() -> None:
    report = {}
    for beat, bpm in (("neon-rush", 138), ("block-party", 140)):
        destination = ROOT / "audio" / "device" / beat
        destination.mkdir(parents=True, exist_ok=True)
        frames = round(RATE * 60 / bpm * 8)
        stems = []
        paths = sorted((ROOT / "audio" / beat).glob("stem*.wav"))
        if len(paths) != 9:
            raise ValueError(f"Expected nine stems for {beat}")
        for path in paths:
            with wave.open(str(path), "rb") as handle:
                assert handle.getparams()[:3] == (1, 2, 44100)
                source = (
                    np.frombuffer(handle.readframes(handle.getnframes()), "<i2") / 32768
                )
            frequencies = np.fft.rfftfreq(len(source), 1 / 44100)
            transition = np.clip((frequencies - 8500) / 1800, 0, 1)
            response = 0.5 + 0.5 * np.cos(np.pi * transition)
            spectrum = np.fft.rfft(source) * response
            resampled = (
                np.fft.irfft(spectrum[: frames // 2 + 1], n=frames)
                * frames
                / len(source)
            )
            pcm = np.rint(resampled * 32768)
            assert np.max(np.abs(pcm)) < 32767
            pcm = pcm.astype("<i2")
            with wave.open(str(destination / path.name), "wb") as handle:
                handle.setparams((1, 2, RATE, frames, "NONE", "not compressed"))
                handle.writeframes(pcm.tobytes())
            stems.append(pcm.astype(float) / 32768)
        stack = np.array(stems)
        mix = np.sum(stack, axis=0) * 0.35
        positive = np.sum(np.maximum(stack, 0), axis=0)
        negative = -np.sum(np.minimum(stack, 0), axis=0)
        subset_bound = float(max(np.max(positive), np.max(negative)) * 0.35)
        assert subset_bound < 0.85
        report[beat] = {
            "frames": frames,
            "sample_rate": RATE,
            "mix_peak": float(np.max(np.abs(mix))),
            "mix_rms": float(np.sqrt(np.mean(mix**2))),
            "worst_of_all_511_subsets_peak": subset_bound,
            "largest_loop_boundary_step": float(
                np.max(np.abs(stack[:, 0] - stack[:, -1]))
            ),
            "bytes": sum(path.stat().st_size for path in destination.glob("*.wav")),
        }
    (ROOT / "audio" / "device" / "metrics.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    prepare()
