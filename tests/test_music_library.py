import io
import json
from pathlib import Path

import pytest

from firmware import music_library


def bank(**changes: object) -> dict:
    value = {
        "id": "house",
        "title": "HOUSE / 124 BPM",
        "bpm": 124,
        "sample_rate": 44100,
        "frames": 85355,
    }
    value.update(changes)
    return value


def save(tmp_path: Path, document: object) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_load_preserves_order_and_known_metadata_derives_paths(tmp_path: Path) -> None:
    path = save(
        tmp_path,
        {
            "version": 1,
            "banks": [
                bank(audio_root="/untrusted", unexpected="ignored"),
                bank(id="night-drive", bpm=125.5, sample_rate=22050),
            ],
            "extra": "ignored",
        },
    )
    loaded = music_library.load_library(path)
    assert loaded == [
        {**bank(), "audio_root": str(tmp_path / "house")},
        {
            **bank(id="night-drive", bpm=125.5, sample_rate=22050),
            "audio_root": str(tmp_path / "night-drive"),
        },
    ]


def test_instrument_labels_normalize_to_immutable_pairs(tmp_path: Path) -> None:
    pairs = [["KICK", "Kick drum"]] * 9
    path = save(tmp_path, {"version": 1, "banks": [bank(instruments=pairs)]})
    assert music_library.load_library(path)[0]["instruments"] == tuple(
        tuple(pair) for pair in pairs
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "library",
        {},
        {"version": True, "banks": [bank()]},
        {"version": 1.0, "banks": [bank()]},
        {"version": 2, "banks": [bank()]},
        {"version": "1", "banks": [bank()]},
        {"version": 1},
        {"version": 1, "banks": []},
        {"version": 1, "banks": {"house": bank()}},
        {"version": 1, "banks": [None]},
        {"version": 1, "banks": [bank(), bank()]},
    ],
)
def test_invalid_document_rejects_entire_library(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(ValueError):
        music_library.load_library(save(tmp_path, document))


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        ".",
        "..",
        "../house",
        "house/../../code.py",
        "/house",
        "house\\file",
        "house%2fother",
        "HOUSE",
        "house beat",
        "house_beat",
        "housé",
        "house\x00",
        "-house",
        "house-",
        "h" * 49,
        123,
        None,
    ],
)
def test_unsafe_or_invalid_id_rejected(tmp_path: Path, identifier: object) -> None:
    path = save(tmp_path, {"version": 1, "banks": [bank(id=identifier)]})
    with pytest.raises(ValueError, match="invalid id"):
        music_library.load_library(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "   "),
        ("title", "t" * 29),
        ("title", "MUSIC\n"),
        ("title", "MUSIC\x7f"),
        ("title", "MUSÍC"),
        ("title", 123),
        ("bpm", True),
        ("bpm", 0),
        ("bpm", -120),
        ("bpm", float("nan")),
        ("bpm", float("inf")),
        ("bpm", float("-inf")),
        ("bpm", "120"),
        ("sample_rate", 48000),
        ("sample_rate", 44100.0),
        ("sample_rate", "44100"),
        ("sample_rate", True),
        ("frames", 0),
        ("frames", -1),
        ("frames", 12.0),
        ("frames", True),
        ("frames", "12"),
        ("frames", None),
    ],
)
def test_invalid_bank_metadata_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    path = save(tmp_path, {"version": 1, "banks": [bank(**{field: value})]})
    with pytest.raises(ValueError, match="Library bank 1"):
        music_library.load_library(path)


@pytest.mark.parametrize("field", ["id", "title", "bpm", "sample_rate", "frames"])
def test_required_bank_field_missing_rejected(tmp_path: Path, field: str) -> None:
    value = bank()
    del value[field]
    with pytest.raises(ValueError):
        music_library.load_library(save(tmp_path, {"version": 1, "banks": [value]}))


@pytest.mark.parametrize(
    "instruments",
    [
        None,
        "123456789",
        [],
        [["KICK", "Kick"]] * 8,
        [["KICK", "Kick"]] * 10,
        ["AB"] * 9,
        [["KICK"]] * 9,
        [["KICK", "Kick", "third"]] * 9,
        [["", "Kick"]] * 9,
        [["1234567", "Kick"]] * 9,
        [["KICK", "t" * 17]] * 9,
        [["KICK", "Kick\n"]] * 9,
        [["KICK", "Kick é"]] * 9,
        [["KICK", 123]] * 9,
    ],
)
def test_invalid_instruments_rejected(tmp_path: Path, instruments: object) -> None:
    path = save(tmp_path, {"version": 1, "banks": [bank(instruments=instruments)]})
    with pytest.raises(ValueError, match="instrument"):
        music_library.load_library(path)


def test_valid_boundary_lengths_and_bank_count(tmp_path: Path) -> None:
    banks = [bank(id="bank-%d" % index) for index in range(64)]
    banks[-1] = bank(
        id="a" * 48,
        title="T" * 28,
        frames=1,
        instruments=[["S" * 6, "F" * 16]] * 9,
    )
    assert (
        len(music_library.load_library(save(tmp_path, {"version": 1, "banks": banks})))
        == 64
    )
    banks.append(bank(id="extra"))
    with pytest.raises(ValueError, match="1-64 banks"):
        music_library.load_library(save(tmp_path, {"version": 1, "banks": banks}))


@pytest.mark.parametrize("payload", [b"", b"{", b"{\xff}"])
def test_invalid_utf8_or_json_rejected(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        music_library.load_library(str(path))


def test_manifest_read_is_bounded_in_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = []

    class File(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return super().read(size)

    def open_file(path: str, mode: str) -> File:
        assert path == "/sd/shaggy/library/manifest.json" and mode == "rb"
        return File(b"x" * (music_library.MAX_MANIFEST_BYTES + 5000))

    monkeypatch.setattr(music_library, "open", open_file, raising=False)
    with pytest.raises(ValueError, match="64 KiB"):
        music_library.load_library()
    assert reads == [65537]


def test_exact_size_limit_accepted_and_next_byte_rejected(tmp_path: Path) -> None:
    content = json.dumps({"version": 1, "banks": [bank()]}).encode("utf-8")
    path = tmp_path / "manifest.json"
    path.write_bytes(content + b" " * (65536 - len(content)))
    assert len(music_library.load_library(str(path))) == 1
    with path.open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="64 KiB"):
        music_library.load_library(str(path))


def test_missing_manifest_preserves_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        music_library.load_library(str(tmp_path / "missing.json"))


def test_invalid_later_bank_does_not_return_partial_library(tmp_path: Path) -> None:
    path = save(tmp_path, {"version": 1, "banks": [bank(), bank(id="../bad")]})
    with pytest.raises(ValueError, match="Library bank 2"):
        music_library.load_library(path)
