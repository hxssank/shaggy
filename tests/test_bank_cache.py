import importlib.util
import io
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmware import audio_assets


def wav_bytes(frames: int = 2500, rate: int = 22050, sample: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setparams((1, 2, rate, frames, "NONE", "not compressed"))
        handle.writeframes(sample.to_bytes(2, "little", signed=True) * frames)
    return buffer.getvalue()


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setitem(sys.modules, "audio_assets", audio_assets)
    path = Path(__file__).parents[1] / "firmware" / "bank_cache.py"
    spec = importlib.util.spec_from_file_location("bank_cache_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "sync", lambda: None)
    source, fallback, root = (tmp_path / name for name in ("source", "house", "cache"))
    source.mkdir()
    fallback.mkdir()
    for index, name in enumerate(module.FILENAMES, 1):
        (source / name).write_bytes(wav_bytes(sample=index))
        (fallback / name).write_bytes(wav_bytes(sample=-index))
    bank = {
        "id": "neon-rush",
        "title": "NEON RUSH / 138 BPM",
        "sample_rate": 22050,
        "frames": 2500,
        "audio_root": str(source),
    }

    def prepare(
        selected: dict | None = None, *, progress: object | None = None
    ) -> Path:
        return Path(
            module.prepare_bank(
                bank if selected is None else selected,
                cache_root=str(root),
                fallback_root=str(fallback),
                progress=progress,
            )
        )

    return SimpleNamespace(
        module=module,
        source=source,
        fallback=fallback,
        root=root,
        bank=bank,
        prepare=prepare,
    )


def test_cache_copies_and_verifies_every_stem_before_becoming_active(
    cache: SimpleNamespace,
) -> None:
    active = cache.prepare()
    assert active == cache.root / "active"
    assert not (cache.root / "staging").exists()
    marker = json.loads((active / "READY.json").read_text())
    assert marker["id"] == cache.bank["id"]
    assert set(marker["files"]) == set(cache.module.FILENAMES)
    for name in cache.module.FILENAMES:
        assert (active / name).read_bytes() == (cache.source / name).read_bytes()
        assert cache.module._digest(str(active / name)) == (
            marker["files"][name]["size"],
            marker["files"][name]["sha256"],
        )


def test_cache_hit_after_reboot_reads_hashes_without_writes(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = cache.prepare()
    before = {path.name: path.stat().st_mtime_ns for path in active.iterdir()}

    def forbid_copy(*args):
        raise AssertionError("A verified cache hit must not rewrite flash")

    monkeypatch.setattr(cache.module, "_copy_file", forbid_copy)
    assert cache.prepare() == active
    assert before == {path.name: path.stat().st_mtime_ns for path in active.iterdir()}


def test_progress_reports_each_copy_before_readback(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = []
    original_copy = cache.module._copy_file
    original_digest = cache.module._digest

    def copy(source: str, destination: str, size: int) -> dict:
        index = cache.module.FILENAMES.index(Path(source).name) + 1
        assert updates[-1] == "LOADING %d/9" % index
        return original_copy(source, destination, size)

    def digest(path: str) -> tuple[int, str]:
        assert updates[-1] == "CHECKING BEAT"
        return original_digest(path)

    monkeypatch.setattr(cache.module, "_copy_file", copy)
    monkeypatch.setattr(cache.module, "_digest", digest)
    cache.prepare(progress=updates.append)
    assert updates == [
        "CHECKING BEAT",
        *["LOADING %d/9" % index for index in range(1, 10)],
        "CHECKING BEAT",
    ]


@pytest.mark.parametrize("house", [False, True])
def test_progress_reports_verification_without_copy_for_cached_bank_or_house(
    cache: SimpleNamespace, house: bool
) -> None:
    cache.prepare()
    updates = []
    bank = dict(cache.bank, id="house") if house else cache.bank
    cache.prepare(bank, progress=updates.append)
    assert updates == ["CHECKING BEAT"]


def test_house_keeps_cached_bank_and_needs_no_sd_source(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = cache.prepare()
    marker = (active / "READY.json").read_bytes()
    selected = dict(cache.bank, id="house", audio_root="/missing-sd")
    monkeypatch.setattr(cache.module, "_digest", lambda _: pytest.fail("SD hash read"))
    assert cache.prepare(selected) == cache.fallback
    assert (active / "READY.json").read_bytes() == marker


@pytest.mark.parametrize("change", ["rate", "frames", "missing"])
def test_house_rejects_incomplete_or_mismatched_fallback(
    cache: SimpleNamespace, change: str
) -> None:
    first = cache.fallback / cache.module.FILENAMES[0]
    if change == "missing":
        first.unlink()
    else:
        first.write_bytes(
            wav_bytes(
                rate=44100 if change == "rate" else 22050,
                frames=2501 if change == "frames" else 2500,
            )
        )
    with pytest.raises((OSError, ValueError)):
        cache.prepare(dict(cache.bank, id="house"))
    assert not cache.root.exists()


def test_changed_source_with_identical_metadata_invalidates_cache(
    cache: SimpleNamespace,
) -> None:
    active = cache.prepare()
    first = cache.module.FILENAMES[0]
    previous = (active / "READY.json").read_bytes()
    (cache.source / first).write_bytes(wav_bytes(sample=77))
    assert cache.prepare() == active
    assert (active / first).read_bytes() == (cache.source / first).read_bytes()
    assert (active / "READY.json").read_bytes() != previous


@pytest.mark.parametrize("corruption", ["samples", "truncated", "missing"])
def test_cached_corruption_is_rebuilt(cache: SimpleNamespace, corruption: str) -> None:
    active = cache.prepare()
    first = active / cache.module.FILENAMES[0]
    if corruption == "missing":
        first.unlink()
    else:
        first.write_bytes(wav_bytes(sample=99) if corruption == "samples" else b"RIFF")
    assert cache.prepare() == active
    assert first.read_bytes() == (cache.source / first.name).read_bytes()


@pytest.mark.parametrize("payload", [b"{", b"x" * 4097, b"[]", b'{"version": 9}'])
def test_incomplete_or_invalid_marker_never_admits_old_audio(
    cache: SimpleNamespace, payload: bytes
) -> None:
    active = cache.prepare()
    (active / "READY.json").write_bytes(payload)
    first = active / cache.module.FILENAMES[0]
    first.write_bytes(wav_bytes(sample=99))
    assert cache.prepare() == active
    assert first.read_bytes() == (cache.source / first.name).read_bytes()


def test_missing_source_keeps_existing_cache_and_fallback(
    cache: SimpleNamespace,
) -> None:
    active = cache.prepare()
    before = (active / "READY.json").read_bytes()
    (cache.source / cache.module.FILENAMES[-1]).unlink()
    with pytest.raises(OSError):
        cache.prepare(dict(cache.bank, id="sandstorm"))
    assert (active / "READY.json").read_bytes() == before
    assert len(list(cache.fallback.iterdir())) == 9


def test_space_failure_keeps_existing_cache(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = cache.prepare()
    previous = (active / "READY.json").read_bytes()
    monkeypatch.setattr(cache.module.os, "statvfs", lambda _: (1024, 1024, 500, 0, 0))
    with pytest.raises(OSError, match="Not enough internal flash"):
        cache.prepare(dict(cache.bank, id="sandstorm"))
    assert (active / "READY.json").read_bytes() == previous


def test_interrupted_copy_is_not_playable_and_next_load_recovers(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.prepare()
    fallback_before = {
        path.name: path.read_bytes() for path in cache.fallback.iterdir()
    }
    original = cache.module._copy_file
    copied = []

    def interrupt(source, destination, size):
        if len(copied) == 3:
            raise OSError("SD disconnected")
        copied.append(destination)
        return original(source, destination, size)

    monkeypatch.setattr(cache.module, "_copy_file", interrupt)
    selected = dict(cache.bank, id="sandstorm")
    with pytest.raises(OSError, match="SD disconnected"):
        cache.prepare(selected)
    assert not (cache.root / "active").exists()
    assert not (cache.root / "staging" / "READY.json").exists()
    assert fallback_before == {
        path.name: path.read_bytes() for path in cache.fallback.iterdir()
    }
    monkeypatch.setattr(cache.module, "_copy_file", original)
    assert cache.prepare(selected) == cache.root / "active"


def test_readback_failure_does_not_publish_cache(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cache.module._digest

    def damaged(path):
        size, digest = original(path)
        return size, "0" * 64 if "/staging/" in path else digest

    monkeypatch.setattr(cache.module, "_digest", damaged)
    with pytest.raises(OSError, match="readback failed"):
        cache.prepare()
    assert not (cache.root / "active").exists()
    assert not (cache.root / "staging" / "READY.json").exists()


def test_failed_activation_leaves_ready_staging_unplayable_then_recovers(
    cache: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cache.module.os.rename

    def fail_rename(*args):
        raise OSError("rename failed")

    monkeypatch.setattr(cache.module.os, "rename", fail_rename)
    with pytest.raises(OSError, match="rename failed"):
        cache.prepare()
    assert not (cache.root / "active").exists()
    assert (cache.root / "staging" / "READY.json").exists()
    monkeypatch.setattr(cache.module.os, "rename", original)
    assert cache.prepare() == cache.root / "active"


@pytest.mark.parametrize("directory", ["active", "staging"])
def test_cleanup_refuses_unknown_files_without_deleting_them(
    cache: SimpleNamespace, directory: str
) -> None:
    folder = cache.root / directory
    folder.mkdir(parents=True)
    protected = folder / "notes.txt"
    protected.write_text("keep")
    with pytest.raises(ValueError, match="Unrecognized cache file"):
        cache.prepare()
    assert protected.read_text() == "keep"


def test_cleanup_refuses_directory_disguised_as_stem(cache: SimpleNamespace) -> None:
    protected = cache.root / "staging" / cache.module.FILENAMES[0]
    protected.mkdir(parents=True)
    (protected / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="regular files"):
        cache.prepare()
    assert (protected / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("target", ["source", "fallback"])
def test_cache_path_cannot_overwrite_source_or_fallback(
    cache: SimpleNamespace, target: str
) -> None:
    folder = getattr(cache, target)
    with pytest.raises(ValueError, match="separate"):
        cache.module.prepare_bank(
            cache.bank, cache_root=str(folder), fallback_root=str(cache.fallback)
        )
    assert len(list(folder.iterdir())) == 9


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../../escape"),
        ("sample_rate", True),
        ("sample_rate", 48000),
        ("frames", 0),
        ("frames", True),
        ("audio_root", "/sd/../beats"),
    ],
)
def test_invalid_bank_is_rejected_before_writes(
    cache: SimpleNamespace, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        cache.prepare(dict(cache.bank, **{field: value}))
    assert not cache.root.exists()
