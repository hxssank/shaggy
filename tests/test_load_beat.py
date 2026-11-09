import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import load_beat


@pytest.fixture
def beat_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(load_beat, "ROOT", tmp_path)
    audio = tmp_path / "audio" / "device"
    banks = [
        {
            "id": "neon-rush",
            "title": "NEON RUSH",
            "bpm": 138,
            "sample_rate": 22050,
            "frames": round(22050 * 60 / 138 * 8),
        },
        {
            "id": "block-party",
            "title": "BLOCK PARTY",
            "bpm": 140,
            "sample_rate": 22050,
            "frames": 75600,
        },
    ]
    for bank in banks:
        beat, bpm = bank["id"], bank["bpm"]
        directory = audio / beat
        directory.mkdir(parents=True)
        frames = round(22050 * 60 / bpm * 8)
        for index, name in enumerate(load_beat.STEMS, 1):
            with wave.open(str(directory / f"stem{index}_{name}.wav"), "wb") as handle:
                handle.setparams((1, 2, 22050, frames, "NONE", "not compressed"))
                handle.writeframes(b"\0\0" * frames)
    (audio / "manifest.json").write_text(json.dumps({"banks": banks}))
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    for name in load_beat.MODULES:
        (firmware / name).write_text("pass\n")
    (firmware / "code.py").write_text("run(banks=BEATS)\n")
    return audio


def test_prepares_two_complete_banks_and_copies_boot_code_last(
    beat_files: Path,
) -> None:
    files = load_beat.prepare()
    assert len(files) == 26
    assert len([name for name in files if name.startswith("beats/")]) == 18
    assert list(files)[-1] == "code.py"
    assert files["code.py"] == b"run(banks=BEATS)\n"


@pytest.mark.parametrize("damage", ["truncated", "missing", "extra"])
def test_bad_bank_is_rejected_before_device_access(
    beat_files: Path, damage: str
) -> None:
    stem = beat_files / "block-party" / "stem5_bass.wav"
    if damage == "truncated":
        stem.write_bytes(stem.read_bytes()[:-100])
    elif damage == "missing":
        stem.unlink()
    else:
        (stem.parent / "stem10_extra.wav").write_bytes(stem.read_bytes())
    with pytest.raises(ValueError):
        load_beat.prepare()


def test_failed_replacement_restores_root_stems_and_existing_bank(
    beat_files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "device"
    mount.mkdir()
    originals = {
        "code.py": b"previous boot",
        "stem1_kick.wav": b"previous kick",
        "beats/neon-rush/stem1_kick.wav": b"previous bank kick",
        "beats/sandstorm/stem1_kick.wav": b"obsolete bank kick",
        "beats/sandstorm/._stem1_kick.wav": b"old metadata",
        "beats/custom/stem1_kick.wav": b"unrelated custom bank",
        "beats/sandstorm/notes.txt": b"unrelated bank note",
        "unrelated.txt": b"do not touch",
    }
    for name, data in originals.items():
        path = mount / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)
    monkeypatch.setattr(
        load_beat.shutil, "disk_usage", lambda path: SimpleNamespace(free=10_000_000)
    )
    commands = []
    monkeypatch.setattr(
        load_beat, "remote", lambda *args, **kwargs: commands.append(args)
    )
    write = load_beat.atomic_write
    attempts = 0

    def fail_once(path: Path, data: bytes) -> None:
        nonlocal attempts
        if path.is_relative_to(mount):
            attempts += 1
            if attempts == 2:
                path.write_bytes(b"partial replacement")
                raise OSError("Disconnected during upload")
        write(path, data)

    monkeypatch.setattr(load_beat, "atomic_write", fail_once)
    with pytest.raises(OSError, match="Disconnected"):
        load_beat.install(mount, "test-device")
    actual = {
        str(path.relative_to(mount)): path.read_bytes()
        for path in mount.rglob("*")
        if path.is_file()
    }
    assert actual == originals
    assert len(commands) == 1
    assert list((tmp_path / ".hardware-tests").glob("*/manifest.json"))


def test_capacity_failure_happens_before_backup_or_remote_command(
    beat_files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "device"
    mount.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)
    monkeypatch.setattr(
        load_beat.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024)
    )
    monkeypatch.setattr(
        load_beat, "remote", lambda *args, **kwargs: pytest.fail("Device was touched")
    )
    with pytest.raises(OSError, match="Not enough space"):
        load_beat.install(mount, "test-device")
    assert not (tmp_path / ".hardware-tests").exists()


def test_single_44100_bank_uses_manifest_frame_count(beat_files: Path) -> None:
    bank = {
        "id": "sandstorm",
        "title": "SANDSTORM",
        "bpm": 136,
        "sample_rate": 44100,
        "frames": 155647,
    }
    directory = beat_files / "sandstorm"
    directory.mkdir()
    for index, name in enumerate(load_beat.STEMS, 1):
        with wave.open(str(directory / f"stem{index}_{name}.wav"), "wb") as handle:
            handle.setparams((1, 2, 44100, bank["frames"], "NONE", "not compressed"))
            handle.writeframes(b"\0\0" * bank["frames"])
    (beat_files / "manifest.json").write_text(json.dumps({"banks": [bank]}))
    files = load_beat.prepare()
    assert len(files) == 17
    assert all(
        name.startswith("beats/sandstorm/") for name in files if name.endswith(".wav")
    )


@pytest.mark.parametrize(
    "identifier", ["../sandstorm", "/sandstorm", "sandstorm/extra", ""]
)
def test_rejects_manifest_path_traversal(beat_files: Path, identifier: str) -> None:
    path = beat_files / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["banks"][0]["id"] = identifier
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="metadata"):
        load_beat.prepare()


def test_frees_backed_up_banks_before_writing_when_flash_is_almost_full(
    beat_files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "device"
    mount.mkdir()
    files = load_beat.prepare()
    for name, data in files.items():
        path = mount / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    obsolete = mount / "beats" / "sandstorm" / "stem1_kick.wav"
    obsolete.parent.mkdir()
    obsolete.write_bytes(b"old bank")
    unrelated = obsolete.parent / "notes.txt"
    unrelated.write_bytes(b"keep this")
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)
    monkeypatch.setattr(
        load_beat.shutil, "disk_usage", lambda path: SimpleNamespace(free=144 * 1024)
    )
    commands = []
    monkeypatch.setattr(
        load_beat, "remote", lambda *args, **kwargs: commands.append((args, kwargs))
    )
    backup = load_beat.install(mount, "test-device")
    assert (backup / "beats/sandstorm/stem1_kick.wav").read_bytes() == b"old bank"
    assert not obsolete.exists()
    assert unrelated.read_bytes() == b"keep this"
    assert all((mount / name).read_bytes() == data for name, data in files.items())
    assert len(commands) == 2
    assert commands[-1][1] == {"reset": True}
