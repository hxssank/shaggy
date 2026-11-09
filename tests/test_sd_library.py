import copy
import io
import json
import struct
import wave
from pathlib import Path

import pytest

from tools.prepare_sd_library import (
    FILENAMES,
    align_wav_data,
    prepare,
    read_pcm,
    resample_pcm,
    validate_library,
    validate_manifest,
    wav_data_offset,
)


def wav_bytes(
    frames: int = 32, rate: int = 22050, channels: int = 1, sample: int = 100
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setparams((channels, 2, rate, frames, "NONE", "not compressed"))
        handle.writeframes(struct.pack("<h", sample) * frames * channels)
    return output.getvalue()


def source_bank(root: Path, identifier: str = "house") -> dict:
    folder = root / identifier
    folder.mkdir(parents=True)
    for name in FILENAMES:
        (folder / name).write_bytes(wav_bytes())
    return {"id": identifier, "title": "HOUSE / 124 BPM", "bpm": 124}


def manifest_bank() -> dict:
    return {
        "id": "house",
        "title": "HOUSE / 124 BPM",
        "bpm": 124,
        "sample_rate": 22050,
        "frames": 32,
    }


def test_prepares_more_than_two_banks_without_touching_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "sd"
    banks = [source_bank(source, f"bank-{index}") for index in range(7)]
    originals = {path: path.read_bytes() for path in source.rglob("*.wav")}

    result = prepare(source, destination, banks)

    assert len(result["banks"]) == 7
    assert validate_library(destination) == result["banks"]
    assert all(path.read_bytes() == data for path, data in originals.items())
    for path, data in originals.items():
        output = (destination / path.relative_to(source)).read_bytes()
        assert read_pcm(output) == read_pcm(data)
        assert wav_data_offset(output) == 512


def test_alignment_preserves_every_sample_and_loop_duration() -> None:
    from firmware.audio_assets import inspect_wav

    source = wav_bytes(frames=427, sample=-12345)
    aligned = align_wav_data(source)
    assert len(aligned) == len(source) + 468
    assert aligned[36:44] == b"JUNK" + struct.pack("<I", 460)
    assert aligned[504:512] == source[36:44]
    assert aligned[512:] == source[44:]
    assert read_pcm(aligned) == read_pcm(source)
    assert inspect_wav(io.BytesIO(aligned)) == (22050, 427)
    assert align_wav_data(aligned) == aligned


def test_alignment_preserves_existing_even_metadata_and_trailing_chunks() -> None:
    source = wav_bytes()
    metadata = b"LIST" + struct.pack("<I", 4) + b"tags"
    trailing = b"JUNK" + struct.pack("<I", 2) + b"ok"
    data = source[:36] + metadata + source[36:] + trailing
    data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    aligned = align_wav_data(data)
    assert wav_data_offset(aligned) == 512
    assert metadata in aligned[:512]
    assert aligned.endswith(trailing)
    assert read_pcm(aligned) == read_pcm(source)


@pytest.mark.parametrize(
    "mutation", ["riff", "truncated", "duplicate", "odd", "odd-data"]
)
def test_alignment_rejects_malformed_or_wavefile_incompatible_metadata(
    mutation: str,
) -> None:
    data = wav_bytes()
    if mutation == "riff":
        data = data[:4] + struct.pack("<I", 100000) + data[8:]
    elif mutation == "truncated":
        data = data[:-2]
    elif mutation == "duplicate":
        data += b"data" + struct.pack("<I", 2) + b"\0\0"
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    elif mutation == "odd":
        data = data[:36] + b"LIST" + struct.pack("<I", 1) + b"x\0" + data[36:]
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    else:
        data = data[:40] + struct.pack("<I", len(data) - 44 + 1) + data[44:] + b"x\0"
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    with pytest.raises(ValueError):
        align_wav_data(data)


@pytest.mark.parametrize(
    "identifier", ["../escape", "/absolute", "has space", "A", "x/y"]
)
def test_rejects_bank_ids_that_cannot_be_safe_folder_names(identifier: str) -> None:
    bank = manifest_bank()
    bank["id"] = identifier
    with pytest.raises(ValueError, match="folder names"):
        validate_manifest({"version": 1, "banks": [bank]})


def test_rejects_duplicate_ids() -> None:
    bank = manifest_bank()
    with pytest.raises(ValueError, match="unique"):
        validate_manifest({"version": 1, "banks": [bank, copy.deepcopy(bank)]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "a" * 25),
        ("title", "bad\ntext"),
        ("bpm", float("nan")),
        ("bpm", float("inf")),
        ("bpm", True),
        ("bpm", 0),
        ("frames", True),
        ("frames", 0),
        ("frames", 1.5),
        ("sample_rate", 44100),
        ("instruments", [["KICK", "KICK"]] * 8),
        ("instruments", [["TOO LONG", "KICK"]] * 9),
    ],
)
def test_rejects_invalid_metadata(field: str, value: object) -> None:
    bank = manifest_bank()
    bank[field] = value
    with pytest.raises(ValueError):
        validate_manifest({"version": 1, "banks": [bank]})


@pytest.mark.parametrize("data", [b"", b"not a wave", wav_bytes()[:-2]])
def test_rejects_invalid_or_truncated_audio(data: bytes) -> None:
    with pytest.raises(ValueError):
        read_pcm(data)


@pytest.mark.parametrize("data", [wav_bytes(rate=48000), wav_bytes(channels=2)])
def test_rejects_unsupported_audio(data: bytes) -> None:
    with pytest.raises(ValueError, match="mono 16-bit"):
        read_pcm(data)


@pytest.mark.parametrize("fault", ["missing", "extra", "length", "rate", "clipping"])
def test_rejects_broken_bank_before_creating_output(tmp_path: Path, fault: str) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "sd"
    bank = source_bank(source)
    path = source / "house" / FILENAMES[-1]
    if fault == "missing":
        path.unlink()
    elif fault == "extra":
        (source / "house" / "stem10_extra.wav").write_bytes(wav_bytes())
    elif fault == "length":
        path.write_bytes(wav_bytes(frames=33))
    elif fault == "rate":
        path.write_bytes(wav_bytes(rate=44100))
    else:
        for path in (source / "house").glob("*.wav"):
            path.write_bytes(wav_bytes(sample=30000))

    with pytest.raises(ValueError):
        prepare(source, destination, [bank])

    assert not destination.exists()


def test_destination_cannot_overwrite_source_assets(tmp_path: Path) -> None:
    bank = source_bank(tmp_path)
    with pytest.raises(ValueError, match="overwrite source"):
        prepare(tmp_path, tmp_path / "house", [bank])


def test_manifest_frame_mismatch_is_detected_before_deployment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "sd"
    prepare(source, output, [source_bank(source)])
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["banks"][0]["frames"] += 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not match manifest"):
        validate_library(output)




def test_downsampling_preserves_gain_and_removes_aliasing() -> None:
    np = pytest.importorskip("numpy")
    time = np.arange(44100) / 44100
    low = 8000 * np.sin(2 * np.pi * 1000 * time)
    high = 8000 * np.sin(2 * np.pi * 15000 * time)
    source = np.rint(1200 + low + high).astype("<i2")

    result = np.frombuffer(resample_pcm(source.tobytes(), 44100), dtype="<i2")

    assert len(result) == 22050
    assert abs(float(np.mean(result)) - 1200) < 0.1
    spectrum = np.abs(np.fft.rfft(result.astype(float))) * 2 / len(result)
    assert abs(spectrum[1000] - 8000) < 2
    assert spectrum[7050] < 2


def test_downsampling_an_odd_loop_preserves_its_period_within_one_frame() -> None:
    np = pytest.importorskip("numpy")
    frames = 85355
    source = np.rint(12000 * np.sin(2 * np.pi * np.arange(frames) * 100 / frames))

    result = np.frombuffer(
        resample_pcm(source.astype("<i2").tobytes(), 44100), dtype="<i2"
    )

    assert abs(len(result) / 22050 - frames / 44100) <= 1 / 22050
    assert abs(float(np.max(result)) - 12000) <= 1
