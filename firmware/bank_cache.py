import binascii
import hashlib
import json
import os

from audio_assets import inspect_wav

FILENAMES = (
    "stem1_kick.wav",
    "stem2_clap.wav",
    "stem3_closedhat.wav",
    "stem4_openhat.wav",
    "stem5_bass.wav",
    "stem6_chords.wav",
    "stem7_perc.wav",
    "stem8_arp.wav",
    "stem9_pad.wav",
)
READY = "READY.json"
MAX_MARKER_BYTES = 4096
RESERVE_BYTES = 65536
COPY_BYTES = 4096


def _sync() -> None:
    sync = getattr(os, "sync", None)
    if sync is not None:
        sync()


def _stat(path: str) -> tuple | None:
    try:
        return os.stat(path)
    except OSError as error:
        if error.args and error.args[0] == 2:
            return None
        raise


def _root(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("Audio directories must be absolute paths")
    value = value.rstrip("/")
    if not value or any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise ValueError("Audio directories must have normalized paths")
    return value


def _overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _metadata(bank: dict) -> dict:
    identifier = bank.get("id")
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 48
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in identifier
        )
    ):
        raise ValueError("Invalid bank id")
    rate, frames = bank.get("sample_rate"), bank.get("frames")
    if type(rate) is not int or rate not in (22050, 44100):
        raise ValueError("Invalid bank sample rate")
    if type(frames) is not int or frames <= 0:
        raise ValueError("Invalid bank frame count")
    return {"version": 1, "id": identifier, "sample_rate": rate, "frames": frames}


def _inspect(directory: str, metadata: dict) -> dict:
    sizes = {}
    for name in FILENAMES:
        path = directory + "/" + name
        info = os.stat(path)
        if info[0] & 0xF000 != 0x8000:
            raise ValueError("Audio path is not a regular file: " + path)
        with open(path, "rb") as handle:
            if inspect_wav(handle) != (metadata["sample_rate"], metadata["frames"]):
                raise ValueError("Audio does not match the bank: " + path)
        sizes[name] = info[6]
    return sizes


def _digest(path: str) -> tuple[int, str]:
    digest = hashlib.new("sha256")
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(COPY_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, binascii.hexlify(digest.digest()).decode()


def _read_marker(directory: str, metadata: dict, sizes: dict) -> dict | None:
    path = directory + "/" + READY
    if _stat(path) is None:
        return None
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_MARKER_BYTES + 1)
        if len(payload) > MAX_MARKER_BYTES:
            return None
        marker = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(marker, dict) or any(
        marker.get(key) != value for key, value in metadata.items()
    ):
        return None
    files = marker.get("files")
    if not isinstance(files, dict) or set(files) != set(FILENAMES):
        return None
    for name, size in sizes.items():
        entry = files[name]
        if not isinstance(entry, dict) or entry.get("size") != size:
            return None
        digest = entry.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return None
    return files


def _matches(directory: str, source: str, files: dict, metadata: dict) -> bool:
    try:
        _inspect(directory, metadata)
        for name in FILENAMES:
            expected = files[name]["size"], files[name]["sha256"]
            if _digest(source + "/" + name) != expected:
                return False
            if _digest(directory + "/" + name) != expected:
                return False
    except (OSError, ValueError):
        return False
    return True


def _managed_files(directory: str) -> list[str]:
    info = _stat(directory)
    if info is None:
        return []
    if info[0] & 0xF000 != 0x4000:
        raise ValueError("Cache path is not a directory")
    names = os.listdir(directory)
    for name in names:
        if name not in FILENAMES and name != READY:
            raise ValueError("Unrecognized cache file: " + name)
        if os.stat(directory + "/" + name)[0] & 0xF000 != 0x8000:
            raise ValueError("Cache entries must be regular files")
    return names


def _clear(directory: str) -> None:
    names = _managed_files(directory)
    if READY in names:
        os.remove(directory + "/" + READY)
        _sync()
        names.remove(READY)
    for name in names:
        os.remove(directory + "/" + name)
    if _stat(directory) is not None:
        os.rmdir(directory)


def _allocated(size: int, block_size: int) -> int:
    return (size + block_size - 1) // block_size * block_size


def _preflight_space(cache_root: str, sizes: dict, directories: tuple) -> None:
    info = os.statvfs(cache_root)
    block_size = info[1] or info[0]
    free = block_size * info[4]
    required = sum(_allocated(size, block_size) for size in sizes.values())
    required += _allocated(MAX_MARKER_BYTES, block_size) + block_size + RESERVE_BYTES
    reclaim = 0
    for directory in directories:
        for name in _managed_files(directory):
            reclaim += _allocated(os.stat(directory + "/" + name)[6], block_size)
    if required > free + reclaim:
        raise OSError("Not enough internal flash for this bank")


def _copy_file(source: str, destination: str, size: int) -> dict:
    digest = hashlib.new("sha256")
    copied = 0
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        while True:
            chunk = reader.read(COPY_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > size:
                raise ValueError("Source audio changed during copying")
            if writer.write(chunk) != len(chunk):
                raise OSError("Incomplete flash write")
            digest.update(chunk)
        writer.flush()
    if copied != size:
        raise ValueError("Source audio changed during copying")
    return {"size": copied, "sha256": binascii.hexlify(digest.digest()).decode()}


def prepare_bank(
    bank: dict,
    *,
    cache_root: str = "/shaggy-cache",
    fallback_root: str = "/beats/house",
    progress: object | None = None,
) -> str:
    metadata = _metadata(bank)
    fallback_root = _root(fallback_root)
    if progress is not None:
        progress("CHECKING BEAT")
    if metadata["id"] == "house":
        _inspect(fallback_root, metadata)
        return fallback_root
    source = _root(bank.get("audio_root"))
    cache_root = _root(cache_root)
    if _overlaps(cache_root, source) or _overlaps(cache_root, fallback_root):
        raise ValueError("Cache must be separate from source and fallback audio")
    sizes = _inspect(source, metadata)
    active, staging = cache_root + "/active", cache_root + "/staging"
    files = _read_marker(active, metadata, sizes)
    if files is not None and _matches(active, source, files, metadata):
        return active
    if _stat(cache_root) is None:
        os.mkdir(cache_root)
    _preflight_space(cache_root, sizes, (active, staging))
    _clear(staging)
    _clear(active)
    _sync()
    os.mkdir(staging)
    files = {}
    for index, name in enumerate(FILENAMES):
        if progress is not None:
            progress("LOADING %d/9" % (index + 1))
        files[name] = _copy_file(source + "/" + name, staging + "/" + name, sizes[name])
    _sync()
    if progress is not None:
        progress("CHECKING BEAT")
    if _inspect(staging, metadata) != sizes:
        raise ValueError("Cached audio length changed")
    for name in FILENAMES:
        expected = files[name]["size"], files[name]["sha256"]
        if _digest(staging + "/" + name) != expected:
            raise OSError("Cached audio readback failed: " + name)
    marker = dict(metadata)
    marker["files"] = files
    payload = json.dumps(marker).encode("utf-8")
    if len(payload) > MAX_MARKER_BYTES:
        raise ValueError("Cache marker exceeds size limit")
    with open(staging + "/" + READY, "wb") as handle:
        if handle.write(payload) != len(payload):
            raise OSError("Incomplete cache marker write")
        handle.flush()
    _sync()
    os.rename(staging, active)
    _sync()
    return active
