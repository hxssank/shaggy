import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWN_BANKS = ("house", "neon-rush", "block-party", "sandstorm")
STEMS = ("kick", "clap", "closedhat", "openhat", "bass", "chords", "perc", "arp", "pad")
MODULES = (
    "volume_control.py",
    "volume_test.py",
    "stem_control.py",
    "beat_control.py",
    "audio_assets.py",
    "player_display.py",
    "shaggy.py",
    "code.py",
)


def bank_manifest() -> list[dict]:
    manifest = json.loads((ROOT / "audio" / "device" / "manifest.json").read_text())
    banks = manifest.get("banks") if isinstance(manifest, dict) else None
    if not isinstance(banks, list) or not 1 <= len(banks) <= 2:
        raise ValueError("Manifest must contain one or two banks")
    identifiers = set()
    for bank in banks:
        if not isinstance(bank, dict):
            raise ValueError("Invalid bank metadata")
        identifier = bank.get("id")
        title, bpm = bank.get("title"), bank.get("bpm")
        rate, frames = bank.get("sample_rate"), bank.get("frames")
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) is None
            or identifier in identifiers
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(bpm, (int, float))
            or not 0 < bpm < float("inf")
            or rate not in (22050, 44100)
            or not isinstance(frames, int)
            or isinstance(frames, bool)
            or frames <= 0
        ):
            raise ValueError("Invalid bank metadata")
        identifiers.add(identifier)
    return banks


def prepare() -> dict[str, bytes]:
    files = {}
    names = {f"stem{index}_{name}.wav" for index, name in enumerate(STEMS, 1)}
    for bank in bank_manifest():
        beat = bank["id"]
        source_dir = ROOT / "audio" / "device" / beat
        if {path.name for path in source_dir.glob("stem*.wav")} != names:
            raise ValueError(f"Beat needs exactly nine instrument stems: {source_dir}")
        expected_frames = bank["frames"]
        for index, instrument in enumerate(STEMS, 1):
            name = f"stem{index}_{instrument}.wav"
            source = source_dir / name
            with wave.open(str(source), "rb") as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() != bank["sample_rate"]
                    or audio.getnframes() != expected_frames
                    or audio.getcomptype() != "NONE"
                    or len(audio.readframes(expected_frames)) != expected_frames * 2
                ):
                    raise ValueError(f"Invalid bank stem: {source}")
            files[f"beats/{beat}/{name}"] = source.read_bytes()
    for name in MODULES:
        files[name] = (ROOT / "firmware" / name).read_bytes()
    return files


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".new")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if path.read_bytes() != data:
            raise OSError(f"Readback mismatch: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def remote(command: str, device_id: str, *, reset: bool = False) -> None:
    arguments = [
        sys.executable,
        "-m",
        "mpremote",
        "connect",
        f"id:{device_id}",
        "resume",
        "exec",
    ]
    if reset:
        arguments.append("--no-follow")
    subprocess.run([*arguments, command], check=True, timeout=25)


def allocated(size: int) -> int:
    return ((size + 1023) // 1024) * 1024


def install(mount: Path, device_id: str) -> Path:
    files = prepare()
    if not mount.is_mount():
        raise OSError(f"Pico drive is not mounted: {mount}")
    stem_names = [f"stem{index}_{name}.wav" for index, name in enumerate(STEMS, 1)]
    removable = set(stem_names)
    removable.update(
        f"beats/{beat}/{name}" for beat in KNOWN_BANKS for name in stem_names
    )
    removable.update(name for name in files if name.endswith(".wav"))
    removable.update(
        str(Path(name).with_name("._" + Path(name).name)) for name in list(removable)
    )
    original = {}
    for name in [*files, *sorted(removable)]:
        path = mount / name
        original[name] = path.read_bytes() if path.exists() else None
    available = shutil.disk_usage(mount).free + sum(
        allocated(len(original[name]))
        for name in removable
        if original[name] is not None
    )
    for name, data in files.items():
        required = allocated(len(data))
        if required + 16384 > available:
            raise OSError(f"Not enough space to replace {name}")
        previous = None if name in removable else original[name]
        available += (
            allocated(len(previous)) if previous is not None else 0
        ) - required
    backup = (
        ROOT
        / ".hardware-tests"
        / (
            time.strftime("beat-install-%Y%m%d-%H%M%S-")
            + str(time.time_ns() % 1_000_000)
        )
    )
    backup.mkdir(parents=True)
    for name, data in original.items():
        if data is not None:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, data)
    manifest = {
        "banks": bank_manifest(),
        "original_sha256": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in original.items()
            if data is not None
        },
        "installed_sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    atomic_write(
        backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode()
    )
    remote("import supervisor; supervisor.runtime.autoreload = False", device_id)
    changed = {}
    try:
        for name in sorted(removable):
            if original[name] is not None:
                changed[name] = None
                (mount / name).unlink()
        for name, data in files.items():
            path = mount / name
            path.parent.mkdir(parents=True, exist_ok=True)
            changed[name] = None
            atomic_write(path, data)
    except Exception:
        for name in changed:
            (mount / name).unlink(missing_ok=True)
        for name in changed:
            if original[name] is not None:
                path = mount / name
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, original[name])
        raise
    remote(
        "import time, microcontroller; time.sleep(0.2); microcontroller.reset()",
        device_id,
        reset=True,
    )
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount", type=Path, default=Path("/Volumes/CIRCUITPY"))
    parser.add_argument("--device-id", default="CFC0A25E7FFC743C")
    args = parser.parse_args()
    backup = install(args.mount, args.device_id)
    print(f"Installed configured beats. Previous files: {backup}")


if __name__ == "__main__":
    main()
