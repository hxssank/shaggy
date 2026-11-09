import argparse
import hashlib
import io
import json
import math
import os
import re
import struct
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RATE = 22050
STEM_LEVEL = 0.35
STEMS = ("kick", "clap", "closedhat", "openhat", "bass", "chords", "perc", "arp", "pad")
FILENAMES = tuple(f"stem{index}_{name}.wav" for index, name in enumerate(STEMS, 1))
DEFAULT_BANKS = (
    {"id": "house", "title": "HOUSE / 124 BPM", "bpm": 124},
    {"id": "neon-rush", "title": "NEON RUSH / 138 BPM", "bpm": 138},
    {"id": "block-party", "title": "BLOCK PARTY / 140 BPM", "bpm": 140},
    {
        "id": "sandstorm",
        "title": "SANDSTORM / 136 BPM",
        "bpm": 136,
        "instruments": [
            ["D.LOW", "DRUM LOW"],
            ["D.BODY", "DRUM BODY"],
            ["D.SNAP", "DRUM SNAP"],
            ["D.AIR", "DRUM AIR"],
            ["B.LOW", "BASS LOW"],
            ["B.EDGE", "BASS EDGE"],
            ["SYNTH", "SYNTH"],
            ["LOW SY", "LOW SYNTH"],
            ["DETAIL", "MIX DETAIL"],
        ],
    },
)


def _printable(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and value.strip() == value
        and all(32 <= ord(char) <= 126 for char in value)
    )


def validate_manifest(manifest: object) -> list[dict]:
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise ValueError("Library needs an integer version")
    if manifest["version"] != 1:
        raise ValueError("Unsupported library version")
    banks = manifest.get("banks")
    if not isinstance(banks, list) or not banks:
        raise ValueError("Library needs at least one bank")
    identifiers = set()
    for bank in banks:
        if not isinstance(bank, dict):
            raise ValueError("Invalid bank metadata")
        identifier = bank.get("id")
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) is None
            or identifier in identifiers
        ):
            raise ValueError("Bank IDs must be unique folder names")
        identifiers.add(identifier)
        if not _printable(bank.get("title"), 24):
            raise ValueError("Bank title needs 1-24 printable ASCII characters")
        bpm = bank.get("bpm")
        if type(bpm) not in (int, float) or not math.isfinite(bpm) or bpm <= 0:
            raise ValueError("Bank tempo must be finite and positive")
        if type(bank.get("sample_rate")) is not int or bank["sample_rate"] != RATE:
            raise ValueError("SD library must use 22050 Hz audio")
        if type(bank.get("frames")) is not int or bank["frames"] <= 0:
            raise ValueError("Bank frame count must be a positive integer")
        instruments = bank.get("instruments")
        if instruments is not None:
            if not isinstance(instruments, (list, tuple)) or len(instruments) != 9:
                raise ValueError("Bank needs nine instrument labels")
            for pair in instruments:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError("Instrument needs short and full labels")
                if not _printable(pair[0], 6) or not _printable(pair[1], 16):
                    raise ValueError("Invalid instrument label")
    return banks


def read_pcm(data: bytes) -> tuple[int, int, bytes]:
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels, width, rate, frames, compression, _ = handle.getparams()
            if (
                channels != 1
                or width != 2
                or rate not in (RATE, RATE * 2)
                or compression != "NONE"
                or frames <= 0
            ):
                raise ValueError("Expected mono 16-bit PCM at 22050 or 44100 Hz")
            pcm = handle.readframes(frames)
            if len(pcm) != frames * width:
                raise ValueError("Truncated WAV samples")
            return rate, frames, pcm
    except (wave.Error, EOFError) as error:
        raise ValueError("Invalid WAV file") from error


def wav_data_offset(data: bytes) -> int:
    read_pcm(data)
    if data[12:20] != b"fmt \x10\x00\x00\x00":
        raise ValueError("WAV must begin with a 16-byte PCM format chunk")
    end = 8 + struct.unpack_from("<I", data, 4)[0]
    if end != len(data):
        raise ValueError("WAV RIFF length must match the complete file")
    offset = 36
    start = None
    while offset < end:
        if end - offset < 8:
            raise ValueError("Truncated WAV chunk header")
        kind, size = struct.unpack_from("<4sI", data, offset)
        chunk_end = offset + 8 + size + (size & 1)
        if chunk_end > end:
            raise ValueError("Truncated WAV chunk")
        if kind == b"fmt ":
            raise ValueError("WAV has multiple format chunks")
        if kind == b"data":
            if start is not None:
                raise ValueError("WAV has multiple data chunks")
            if size == 0 or size & 1:
                raise ValueError("WAV data must contain complete 16-bit samples")
            start = offset + 8
        elif start is None and size & 1:
            raise ValueError("CircuitPython needs even metadata sizes before WAV data")
        offset = chunk_end
    if start is None:
        raise ValueError("WAV has no data chunk")
    return start


def align_wav_data(data: bytes) -> bytes:
    offset = wav_data_offset(data)
    padding = -offset % 512
    if padding == 0:
        return data
    if padding < 8:
        padding += 512
    insertion = offset - 8
    junk = b"JUNK" + struct.pack("<I", padding - 8) + bytes(padding - 8)
    result = data[:4] + struct.pack("<I", len(data) - 8 + padding) + data[8:insertion]
    return result + junk + data[insertion:]


def resample_pcm(pcm: bytes, source_rate: int) -> bytes:
    if source_rate == RATE:
        return pcm
    if source_rate != RATE * 2 or not pcm or len(pcm) % 2:
        raise ValueError("Expected complete 44100 Hz PCM samples")
    import numpy as np

    source = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    frames = round(len(source) * RATE / source_rate)
    frequencies = np.fft.rfftfreq(len(source), 1 / source_rate)
    transition = np.clip((frequencies - 8500) / 1800, 0, 1)
    response = (1 + np.cos(np.pi * transition)) / 2
    spectrum = np.fft.rfft(source) * response
    samples = np.fft.irfft(spectrum[: frames // 2 + 1], n=frames)
    samples *= frames / len(source)
    rounded = np.rint(samples)
    if np.min(rounded) < -32768 or np.max(rounded) > 32767:
        raise ValueError("Resampling would clip; source gain needs review")
    return rounded.astype("<i2").tobytes()


def _wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setparams((1, 2, RATE, len(pcm) // 2, "NONE", "not compressed"))
        handle.writeframes(pcm)
    return output.getvalue()


def _metrics(stems: list[bytes]) -> dict:
    decoded = [array("h", pcm) for pcm in stems]
    if sys.byteorder != "little":
        for samples in decoded:
            samples.byteswap()
    subset_peak = 0
    mix_peak = 0
    squared_sum = 0
    for frame in zip(*decoded, strict=True):
        mixed = sum(frame)
        mix_peak = max(mix_peak, abs(mixed))
        squared_sum += mixed * mixed
        subset_peak = max(
            subset_peak,
            sum(sample for sample in frame if sample > 0),
            -sum(sample for sample in frame if sample < 0),
        )
    bound = subset_peak / 32768 * STEM_LEVEL
    if bound >= 0.98:
        raise ValueError("A tile combination can clip at the existing mixer gain")
    return {
        "mix_peak_at_0_35": mix_peak / 32768 * STEM_LEVEL,
        "mix_rms_at_0_35": math.sqrt(squared_sum / len(decoded[0]))
        / 32768
        * STEM_LEVEL,
        "all_subsets_peak_at_0_35": bound,
        "largest_loop_boundary_step": max(
            abs(samples[0] - samples[-1]) for samples in decoded
        )
        / 32768,
    }


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".new")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if path.read_bytes() != data:
            raise OSError(f"Readback mismatch: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def prepare(
    source_root: Path = ROOT / "audio" / "device",
    destination: Path = ROOT / "audio" / "sd-library",
    banks: tuple[dict, ...] | list[dict] = DEFAULT_BANKS,
) -> dict:
    if destination.resolve().is_relative_to(source_root.resolve()):
        raise ValueError("Destination must not overwrite source assets")
    metadata = [dict(bank, sample_rate=RATE, frames=1) for bank in banks]
    validate_manifest({"version": 1, "banks": metadata})
    files = {}
    report = {}
    for bank in metadata:
        identifier = bank["id"]
        source_dir = source_root / identifier
        if {path.name for path in source_dir.glob("stem*.wav")} != set(FILENAMES):
            raise ValueError(f"Expected exactly nine instrument stems: {identifier}")
        expected = None
        converted = []
        hashes = {}
        for name in FILENAMES:
            source = (source_dir / name).read_bytes()
            source_rate, source_frames, pcm = read_pcm(source)
            if expected is not None and expected != (source_rate, source_frames):
                raise ValueError(f"Stem rates and lengths must match: {identifier}")
            expected = source_rate, source_frames
            samples = resample_pcm(pcm, source_rate)
            converted.append(samples)
            files[f"{identifier}/{name}"] = align_wav_data(
                source if source_rate == RATE else _wav(samples)
            )
            hashes[name] = hashlib.sha256(source).hexdigest()
        bank["frames"] = len(converted[0]) // 2
        report[identifier] = {
            **_metrics(converted),
            "source_sample_rate": expected[0],
            "source_frames": expected[1],
            "sample_rate": RATE,
            "frames": bank["frames"],
            "source_sha256": hashes,
            "bytes": sum(len(files[f"{identifier}/{name}"]) for name in FILENAMES),
        }
    manifest = {"version": 1, "banks": metadata}
    validate_manifest(manifest)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)
    _atomic_write(
        destination / "metrics.json", (json.dumps(report, indent=2) + "\n").encode()
    )
    _atomic_write(
        destination / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode()
    )
    return manifest


def validate_library(directory: Path) -> list[dict]:
    banks = validate_manifest(json.loads((directory / "manifest.json").read_text()))
    for bank in banks:
        folder = directory / bank["id"]
        if {path.name for path in folder.glob("stem*.wav")} != set(FILENAMES):
            raise ValueError(f"Expected exactly nine instrument stems: {bank['id']}")
        for name in FILENAMES:
            rate, frames, _ = read_pcm((folder / name).read_bytes())
            if (rate, frames) != (bank["sample_rate"], bank["frames"]):
                raise ValueError(f"WAV does not match manifest: {bank['id']}/{name}")
    return banks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "audio" / "device")
    parser.add_argument(
        "--destination", type=Path, default=ROOT / "audio" / "sd-library"
    )
    args = parser.parse_args()
    manifest = prepare(args.source, args.destination)
    validate_library(args.destination)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
