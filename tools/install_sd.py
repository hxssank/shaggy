import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from tools.load_beat import atomic_write
from tools.prepare_sd_library import FILENAMES, validate_library

ROOT = Path(__file__).resolve().parents[1]
DEVICE_ID = "CFC0A25E7FFC743C"
SD_ROOT = "/sd/shaggy"
LIBRARY = SD_ROOT + "/library"
MARKER = ".shaggy-install.json"
SUCCESS = "SHAGGY_INSTALL_COMMAND_OK"
MODULES = (
    "audio_assets.py",
    "volume_control.py",
    "volume_test.py",
    "stem_control.py",
    "beat_control.py",
    "player_display.py",
    "sd_storage.py",
    "music_library.py",
    "library_control.py",
    "bank_cache.py",
    "shaggy.py",
    "shaggy_os.py",
    "boot.py",
    "code.py",
)


@dataclass(frozen=True)
class InstallPlan:
    audio: dict[str, bytes]
    firmware: dict[str, bytes]
    banks: list[dict]


@dataclass(frozen=True)
class StagePaths:
    token: str
    stage: str
    backup: str
    failed: str


def stage_paths(token: str) -> StagePaths:
    if re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", token) is None:
        raise ValueError("Invalid installation token")
    return StagePaths(
        token,
        SD_ROOT + "/library-stage-" + token,
        SD_ROOT + "/library-backup-" + token,
        SD_ROOT + "/library-failed-" + token,
    )


def remote_path(stage: str, relative: str) -> str:
    if not stage.startswith(SD_ROOT + "/library-stage-"):
        raise ValueError("Uploads must target a staging directory")
    stage_paths(stage.removeprefix(SD_ROOT + "/library-stage-"))
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or str(path) != relative
        or any(part in ("", ".", "..") for part in path.parts)
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-._/"
            for character in relative
        )
    ):
        raise ValueError("Invalid relative library path")
    return stage + "/" + relative


def prepare(root: Path = ROOT) -> InstallPlan:
    directory = root / "audio" / "sd-library"
    banks = validate_library(directory)
    audio = {"manifest.json": (directory / "manifest.json").read_bytes()}
    for bank in banks:
        for name in FILENAMES:
            relative = bank["id"] + "/" + name
            audio[relative] = (directory / relative).read_bytes()
    return InstallPlan(audio, prepare_firmware(root), banks)


def prepare_firmware(root: Path = ROOT) -> dict[str, bytes]:
    firmware = {}
    for name in MODULES:
        data = (root / "firmware" / name).read_bytes()
        compile(data, name, "exec")
        firmware[name] = data
    return firmware


def check_device(mount: Path, device_id: str) -> None:
    if not mount.is_mount():
        raise OSError(f"Pico drive is not mounted: {mount}")
    boot = (mount / "boot_out.txt").read_text()
    if "Board ID:raspberry_pi_pico2" not in boot or f"UID:{device_id}" not in boot:
        raise OSError("Mounted CircuitPython drive is not the selected Pico 2")


def check_flash_space(mount: Path, firmware: dict[str, bytes]) -> None:
    free = shutil.disk_usage(mount).free
    for name, data in firmware.items():
        required = ((len(data) + 1023) // 1024) * 1024
        if required + 16384 > free:
            raise OSError(f"Insufficient internal flash to replace {name}")
        previous = mount / name
        old_size = previous.stat().st_size if previous.exists() else 0
        free += ((old_size + 1023) // 1024) * 1024 - required


def verification_script(path: str, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return (
        f"_shaggy_actual = _shaggy_hash({path!r})\n"
        f"assert _shaggy_actual == ({len(data)}, {digest!r}), "
        f"'SD readback mismatch: ' + {path!r}\n"
    )


def firmware_path(name: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]*\.py", name) is None:
        raise ValueError("Firmware name must be a Python filename")
    return "/" + name


def firmware_activation_script(
    target: str, stage: str, backup: str, data: bytes
) -> str:
    return f"""import os
def _shaggy_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
assert not _shaggy_exists({backup!r}), 'Firmware backup already exists'
{verification_script(stage, data)}
_shaggy_had_previous = _shaggy_exists({target!r})
if _shaggy_had_previous:
    os.rename({target!r}, {backup!r})
try:
    os.rename({stage!r}, {target!r})
    {verification_script(target, data).replace(chr(10), chr(10) + '    ').rstrip()}
    os.sync()
except BaseException:
    if _shaggy_exists({target!r}):
        os.remove({target!r})
    if _shaggy_had_previous:
        os.rename({backup!r}, {target!r})
    os.sync()
    raise
if _shaggy_had_previous:
    os.remove({backup!r})
os.sync()
"""


def activation_script(paths: StagePaths) -> str:
    return f"""import os, json
def _shaggy_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
assert _shaggy_exists({paths.stage!r}), 'Staged library missing'
assert not _shaggy_exists({paths.backup!r}), 'Backup path already exists'
with open({(paths.stage + "/" + MARKER)!r}) as handle:
    assert json.load(handle)['token'] == {paths.token!r}, 'Wrong staged library'
_shaggy_had_previous = _shaggy_exists({LIBRARY!r})
if _shaggy_had_previous:
    os.rename({LIBRARY!r}, {paths.backup!r})
try:
    os.rename({paths.stage!r}, {LIBRARY!r})
except BaseException:
    if _shaggy_had_previous:
        os.rename({paths.backup!r}, {LIBRARY!r})
    raise
"""


def rollback_script(paths: StagePaths) -> str:
    return f"""import os, json
def _shaggy_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
def _shaggy_owns_library():
    try:
        with open({(LIBRARY + "/" + MARKER)!r}) as handle:
            return json.load(handle).get('token') == {paths.token!r}
    except (OSError, ValueError):
        return False
if _shaggy_exists({paths.backup!r}):
    if _shaggy_exists({LIBRARY!r}):
        assert _shaggy_owns_library(), 'Live library changed; keep both copies'
        assert not _shaggy_exists({paths.failed!r}), 'Failed library path exists'
        os.rename({LIBRARY!r}, {paths.failed!r})
    os.rename({paths.backup!r}, {LIBRARY!r})
elif _shaggy_exists({LIBRARY!r}) and _shaggy_owns_library():
    assert not _shaggy_exists({paths.failed!r}), 'Failed library path exists'
    os.rename({LIBRARY!r}, {paths.failed!r})
"""


class Remote:
    def __init__(self, device_id: str, log: Path) -> None:
        self.device_id = device_id
        self.log = log
        self._transport = None
        self._hashing_ready = False

    def _connect(self, fresh: bool = False) -> Any:
        if self._transport is not None:
            if fresh:
                raise RuntimeError("Cannot reset an active SD transfer session")
            return self._transport
        from mpremote.transport_serial import SerialTransport
        from serial.tools import list_ports

        ports = [
            port.device
            for port in list_ports.comports()
            if port.serial_number == self.device_id
        ]
        if len(ports) != 1:
            raise RuntimeError("Expected exactly one serial port for the selected Pico")
        transport = SerialTransport(ports[0], timeout=2)
        try:
            transport.enter_raw_repl(soft_reset=fresh, timeout_overall=20)
        except BaseException:
            transport.close()
            raise
        self._transport = transport
        return transport

    def close(self) -> None:
        self._hashing_ready = False
        if self._transport is not None:
            transport, self._transport = self._transport, None
            transport.close()

    def _run(self, arguments: list[str], timeout: int) -> str:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mpremote",
                "connect",
                "id:" + self.device_id,
                "resume",
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        with self.log.open("a") as handle:
            handle.write(output + "\n")
        if result.returncode:
            raise RuntimeError(f"Device command failed: {output[-3000:]}")
        return output

    def execute(self, source: str, timeout: int = 30, *, fresh: bool = False) -> str:
        transport = self._connect(fresh=fresh)
        stdout, stderr = transport.exec_raw(
            source + f"\nprint({SUCCESS!r})", timeout=timeout
        )
        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        with self.log.open("a") as handle:
            handle.write(output + "\n")
        if stderr:
            raise RuntimeError(f"Device command failed: {output[-3000:]}")
        if SUCCESS not in output.splitlines():
            raise RuntimeError(f"Device command did not complete: {output[-3000:]}")
        return output

    def copy(self, local: Path, destination: str) -> None:
        if self._transport is None:
            raise RuntimeError("SD copy requires an active raw REPL session")
        self._transport.fs_writefile(destination, local.read_bytes(), chunk_size=1024)
        with self.log.open("a") as handle:
            handle.write(f"Copied {local.name} to {destination}\n")

    def _prepare_hashing(self) -> None:
        if self._hashing_ready:
            return
        self.execute(
            "import hashlib, binascii\n"
            "def _shaggy_hash(path):\n"
            "    digest = hashlib.new('sha256')\n"
            "    count = 0\n"
            "    with open(path, 'rb') as handle:\n"
            "        while True:\n"
            "            block = handle.read(4096)\n"
            "            if not block:\n"
            "                break\n"
            "            count += len(block)\n"
            "            digest.update(block)\n"
            "    return count, binascii.hexlify(digest.digest()).decode()\n"
        )
        self._hashing_ready = True

    def device_owns_flash(self) -> bool:
        output = self.execute(
            "import storage\n"
            "print('SHAGGY_FLASH_OWNER=' + "
            "('host' if storage.getmount('/').readonly else 'device'))"
        )
        lines = output.splitlines()
        if "SHAGGY_FLASH_OWNER=device" in lines:
            return True
        if "SHAGGY_FLASH_OWNER=host" in lines:
            return False
        raise RuntimeError("Device did not report flash ownership")

    def read_firmware(self, name: str) -> bytes | None:
        target = firmware_path(name)
        transport = self._connect()
        try:
            size = transport.fs_stat(target).st_size
        except OSError as error:
            if error.errno == 2:
                return None
            raise
        if size > 512 * 1024:
            raise ValueError(f"Firmware backup exceeds 512 KiB: {name}")
        data = bytes(transport.fs_readfile(target, chunk_size=1024))
        if len(data) != size:
            raise RuntimeError(f"Incomplete firmware backup: {name}")
        self.verify_firmware(name, data)
        return data

    def check_firmware_space(
        self, firmware: dict[str, bytes], original: dict[str, bytes | None]
    ) -> None:
        sizes = [
            (name, len(data), len(original.get(name) or b""))
            for name, data in firmware.items()
        ]
        self.execute(
            "import os\n"
            "_shaggy_stat = os.statvfs('/')\n"
            "_shaggy_block = _shaggy_stat[0]\n"
            "_shaggy_free = _shaggy_block * _shaggy_stat[3]\n"
            f"for _name, _new, _old in {sizes!r}:\n"
            "    _need = ((_new + _shaggy_block - 1) // "
            "_shaggy_block) * _shaggy_block\n"
            "    assert _need + 16384 <= _shaggy_free, "
            "'Insufficient internal flash for ' + _name\n"
            "    _shaggy_free += ((_old + _shaggy_block - 1) // "
            "_shaggy_block) * _shaggy_block - _need\n"
        )

    def verify_firmware(self, name: str, data: bytes | None) -> None:
        target = firmware_path(name)
        if data is None:
            self.execute(
                "import os\n"
                f"assert {name!r} not in os.listdir('/'), 'Firmware appeared: {name}'"
            )
        else:
            self._prepare_hashing()
            self.execute(verification_script(target, data))

    def write_firmware(self, name: str, data: bytes) -> None:
        target = firmware_path(name)
        if self._transport is None:
            raise RuntimeError("Firmware copy requires an active raw REPL session")
        self._prepare_hashing()
        token = uuid.uuid4().hex[:12]
        stage = f"/.shaggy-{token}-{name}.tmp"
        backup = f"/.shaggy-{token}-{name}.bak"
        try:
            self._transport.fs_writefile(stage, data, chunk_size=1024)
            self.execute(firmware_activation_script(target, stage, backup, data))
        except BaseException:
            try:
                self.execute(
                    "import os\n"
                    "try:\n"
                    f"    os.remove({stage!r})\n"
                    "except OSError:\n"
                    "    pass\n"
                )
            except Exception:
                pass
            raise

    def remove_firmware(self, name: str, expected: bytes) -> None:
        target = firmware_path(name)
        self._prepare_hashing()
        check = verification_script(target, expected).replace("\n", "\n    ").rstrip()
        self.execute(
            "import os\n"
            f"if {name!r} in os.listdir('/'):\n"
            f"    {check}\n"
            f"    os.remove({target!r})\n"
            "    os.sync()"
        )

    def reset_serial(self) -> None:
        try:
            self._connect().exec_raw_no_follow(
                "import time, microcontroller\n"
                "time.sleep(0.2)\n"
                "microcontroller.reset()"
            )
        finally:
            self.close()
        time.sleep(2)
        from serial.tools import list_ports

        for _ in range(70):
            if any(
                port.serial_number == self.device_id for port in list_ports.comports()
            ):
                return
            time.sleep(0.5)
        raise RuntimeError("Pico serial port did not reconnect after reset")

    def reset(self, mount: Path) -> None:
        self.close()
        self._run(
            [
                "exec",
                "--no-follow",
                "import time, microcontroller\n"
                "time.sleep(0.2)\n"
                "microcontroller.reset()",
            ],
            timeout=15,
        )
        time.sleep(2)
        from serial.tools import list_ports

        for _ in range(70):
            if any(
                port.serial_number == self.device_id for port in list_ports.comports()
            ):
                try:
                    check_device(mount, self.device_id)
                    return
                except OSError:
                    pass
            time.sleep(0.5)
        raise RuntimeError("Pico did not reconnect after reset")

    def mount_sd(self, source: bytes) -> None:
        self.execute(
            "import supervisor, displayio, gc, hashlib, binascii\n"
            "supervisor.runtime.autoreload = False\n"
            "displayio.release_displays()\n"
            "gc.collect()\n"
            "_shaggy_probe = hashlib.new('sha256')\n"
            "_shaggy_probe.update(b'test')\n"
            "assert binascii.hexlify(_shaggy_probe.digest()).decode() == "
            "'9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08'\n"
            "_shaggy_ns = {'__name__': '_shaggy_installer'}\n"
            f"exec({source.decode()!r}, _shaggy_ns)\n"
            "_shaggy_sd = _shaggy_ns['SDStorage'](baudrate=16000000)\n"
            "assert _shaggy_sd.mount(readonly=False), _shaggy_sd.error\n"
            "def _shaggy_hash(path):\n"
            "    digest = hashlib.new('sha256')\n"
            "    count = 0\n"
            "    with open(path, 'rb') as handle:\n"
            "        while True:\n"
            "            block = handle.read(4096)\n"
            "            if not block:\n"
            "                break\n"
            "            count += len(block)\n"
            "            digest.update(block)\n"
            "    return count, binascii.hexlify(digest.digest()).decode()\n",
            timeout=45,
        )

    def create_stage(self, paths: StagePaths, audio: dict[str, bytes]) -> None:
        folders = sorted(
            {relative.split("/")[0] for relative in audio if "/" in relative}
        )
        for relative in audio:
            remote_path(paths.stage, relative)
        self.execute(
            "import os\n"
            "assert _shaggy_sd.mounted and not _shaggy_sd.readonly, "
            "'SD write mount is not active'\n"
            "_shaggy_stat = os.statvfs('/sd')\n"
            f"_shaggy_required = {sum(map(len, audio.values())) + 65536}\n"
            "assert _shaggy_stat[0] * _shaggy_stat[3] >= _shaggy_required, "
            "'Not enough SD space for staged library'\n"
            "try:\n"
            f"    _shaggy_root_stat = os.stat({SD_ROOT!r})\n"
            "except OSError:\n"
            f"    os.mkdir({SD_ROOT!r})\n"
            "else:\n"
            "    assert _shaggy_root_stat[0] & 0x4000, "
            "'SD project path is not a directory'\n"
            f"os.mkdir({paths.stage!r})\n"
            + "".join(
                f"os.mkdir({(paths.stage + '/' + folder)!r})\n" for folder in folders
            )
        )


def install(
    plan: InstallPlan,
    mount: Path = Path("/Volumes/CIRCUITPY"),
    device_id: str = DEVICE_ID,
    root: Path = ROOT,
) -> Path:
    check_device(mount, device_id)
    check_flash_space(mount, plan.firmware)
    stamp = datetime.now(ZoneInfo("America/Toronto")).strftime("%Y%m%d-%H%M%S")
    paths = stage_paths(stamp + "-" + uuid.uuid4().hex[:8])
    backup = root / ".hardware-tests" / ("sd-install-" + paths.token)
    backup.mkdir(parents=True)
    original = {
        path.name: path.read_bytes()
        for path in mount.iterdir()
        if path.is_file()
        and (path.suffix == ".py" or path.name in ("boot_out.txt", "settings.toml"))
    }
    for name, data in original.items():
        target = backup / "firmware-before" / name
        target.parent.mkdir(exist_ok=True)
        atomic_write(target, data)
    fallback = mount / "beats" / "house"
    fallback_hashes = {}
    for name in FILENAMES:
        path = fallback / name
        if not path.is_file():
            raise ValueError("Internal house fallback is incomplete")
        fallback_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    audio = dict(plan.audio)
    audio[MARKER] = (json.dumps({"token": paths.token, "version": 1}) + "\n").encode()
    report = {
        "token": paths.token,
        "stage": paths.stage,
        "previous_library_backup": paths.backup,
        "failed_library": paths.failed,
        "before_sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in original.items()
        },
        "firmware_sha256": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in plan.firmware.items()
        },
        "audio_sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in audio.items()
        },
        "internal_house_sha256": fallback_hashes,
        "status": "prepared",
    }
    atomic_write(
        backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
    )
    for name, data in audio.items():
        path = backup / "sd-payload" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
    remote = Remote(device_id, backup / "serial.log")
    changed = []
    activation_attempted = False
    mounted = False
    serial_flash = False
    try:
        remote.reset(mount)
        remote.execute(
            "import supervisor; supervisor.runtime.autoreload = False", fresh=True
        )
        serial_flash = remote.device_owns_flash()
        if serial_flash:
            remote.execute(
                "import os\n"
                "if 'sd' not in os.listdir('/'):\n"
                "    os.mkdir('/sd')\n"
            )
        else:
            (mount / "sd").mkdir(exist_ok=True)
        remote.mount_sd(plan.firmware["sd_storage.py"])
        mounted = True
        remote.create_stage(paths, audio)
        for index, (name, data) in enumerate(audio.items(), 1):
            target = remote_path(paths.stage, name)
            remote.copy(backup / "sd-payload" / name, target)
            remote.execute(verification_script(target, data), timeout=45)
            print(f"Verified SD file {index}/{len(audio)}: {name}", flush=True)
        activation_attempted = True
        remote.execute(activation_script(paths))
        for name, data in plan.firmware.items():
            expected = original.get(name)
            if serial_flash:
                remote.verify_firmware(name, expected)
            else:
                target = mount / name
                current = target.read_bytes() if target.exists() else None
                if current != expected:
                    raise RuntimeError(
                        f"Installed firmware changed during upload: {name}"
                    )
            changed.append(name)
            if serial_flash:
                remote.write_firmware(name, data)
            else:
                atomic_write(mount / name, data)
        for name, data in plan.firmware.items():
            if serial_flash:
                remote.verify_firmware(name, data)
            else:
                assert (mount / name).read_bytes() == data
        for name, digest in fallback_hashes.items():
            if serial_flash:
                target = "/beats/house/" + name
                remote.execute(
                    f"assert _shaggy_hash({target!r})[1] == {digest!r}, "
                    "'Internal fallback changed'"
                )
            else:
                actual = hashlib.sha256((fallback / name).read_bytes()).hexdigest()
                assert actual == digest
        remote.execute("_shaggy_sd.close()")
        mounted = False
        remote.reset_serial()
        report["status"] = "installed"
    except BaseException as error:
        failures = []
        if activation_attempted and not mounted:
            try:
                remote.reset_serial()
                remote.execute(
                    "import supervisor; supervisor.runtime.autoreload = False",
                    fresh=True,
                )
                serial_flash = remote.device_owns_flash()
                remote.mount_sd(plan.firmware["sd_storage.py"])
                mounted = True
            except Exception as restore_error:
                failures.append(f"SD remount for rollback: {restore_error}")
        restore_order = [name for name in reversed(changed) if name != "code.py"]
        if "code.py" in changed:
            restore_order.append("code.py")
        for name in restore_order:
            try:
                if name in original:
                    if serial_flash:
                        remote.write_firmware(name, original[name])
                    else:
                        atomic_write(mount / name, original[name])
                elif serial_flash:
                    remote.remove_firmware(name, plan.firmware[name])
                else:
                    (mount / name).unlink(missing_ok=True)
            except Exception as restore_error:
                failures.append(f"Firmware {name}: {restore_error}")
        if activation_attempted and mounted:
            try:
                remote.execute(rollback_script(paths))
            except Exception as restore_error:
                failures.append(f"SD library: {restore_error}")
        if mounted:
            try:
                remote.execute("_shaggy_sd.close()")
            except Exception as close_error:
                failures.append(f"SD close: {close_error}")
        try:
            remote.reset_serial()
        except Exception as reset_error:
            failures.append(f"Pico restart: {reset_error}")
        report.update(status="failed", error=repr(error), recovery_errors=failures)
        atomic_write(
            backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
        )
        if failures:
            raise RuntimeError(
                f"Installation failed; recovery needs attention: {failures}. "
                f"Backup: {backup}"
            ) from error
        raise
    atomic_write(
        backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
    )
    return backup


def install_firmware(
    firmware: dict[str, bytes], device_id: str = DEVICE_ID, root: Path = ROOT
) -> Path:
    if not firmware or any(name not in MODULES for name in firmware):
        raise ValueError("Firmware update must contain known Shaggy modules")
    firmware = {name: firmware[name] for name in MODULES if name in firmware}
    for name, data in firmware.items():
        compile(data, name, "exec")
    stamp = datetime.now(ZoneInfo("America/Toronto")).strftime("%Y%m%d-%H%M%S")
    token = stamp + "-" + uuid.uuid4().hex[:8]
    backup = root / ".hardware-tests" / ("firmware-install-" + token)
    (backup / "firmware-before").mkdir(parents=True)
    report = {
        "token": token,
        "device_id": device_id,
        "status": "prepared",
        "firmware_sha256": {
            name: hashlib.sha256(data).hexdigest() for name, data in firmware.items()
        },
    }
    remote = Remote(device_id, backup / "serial.log")
    original = {}
    changed = []
    try:
        remote.execute(
            "import supervisor; supervisor.runtime.autoreload = False", fresh=True
        )
        if not remote.device_owns_flash():
            raise RuntimeError("Serial firmware updates require device-owned flash")
        for name in MODULES:
            data = remote.read_firmware(name)
            original[name] = data
            if data is not None:
                atomic_write(backup / "firmware-before" / name, data)
        report["before_sha256"] = {
            name: None if data is None else hashlib.sha256(data).hexdigest()
            for name, data in original.items()
        }
        atomic_write(
            backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
        )
        remote.check_firmware_space(firmware, original)
        for name, data in firmware.items():
            remote.verify_firmware(name, original[name])
            if original[name] == data:
                continue
            changed.append(name)
            remote.write_firmware(name, data)
        for name, data in firmware.items():
            remote.verify_firmware(name, data)
        remote.reset_serial()
        report["status"] = "installed"
    except BaseException as error:
        failures = []
        if changed:
            try:
                remote.close()
                remote.execute(
                    "import supervisor; supervisor.runtime.autoreload = False",
                    fresh=True,
                )
                if not remote.device_owns_flash():
                    raise RuntimeError("Flash ownership changed during recovery")
                order = [name for name in reversed(changed) if name != "code.py"]
                if "code.py" in changed:
                    order.append("code.py")
                for name in order:
                    try:
                        if original[name] is None:
                            remote.remove_firmware(name, firmware[name])
                        else:
                            remote.write_firmware(name, original[name])
                    except Exception as restore_error:
                        failures.append(f"Firmware {name}: {restore_error}")
            except Exception as restore_error:
                failures.append(f"Recovery connection: {restore_error}")
        try:
            remote.reset_serial()
        except Exception as reset_error:
            failures.append(f"Pico restart: {reset_error}")
        report.update(status="failed", error=repr(error), recovery_errors=failures)
        atomic_write(
            backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
        )
        if failures:
            raise RuntimeError(
                f"Firmware update failed; recovery needs attention: {failures}. "
                f"Backup: {backup}"
            ) from error
        raise
    finally:
        remote.close()
    atomic_write(
        backup / "install.json", (json.dumps(report, indent=2) + "\n").encode()
    )
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount", type=Path, default=Path("/Volumes/CIRCUITPY"))
    parser.add_argument("--device-id", default=DEVICE_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--firmware-only", action="store_true")
    args = parser.parse_args()
    if args.firmware_only:
        firmware = prepare_firmware()
        if args.dry_run:
            print(json.dumps({"firmware_files": list(firmware)}, indent=2))
        else:
            backup = install_firmware(firmware, args.device_id)
            print(f"Installed firmware over USB serial. Backup: {backup}")
        return
    plan = prepare()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "banks": [bank["id"] for bank in plan.banks],
                    "sd_files": len(plan.audio),
                    "sd_bytes": sum(map(len, plan.audio.values())),
                    "firmware_files": list(plan.firmware),
                },
                indent=2,
            )
        )
        return
    if not args.mount.is_mount():
        parser.error(
            "Full SD installs require a mounted CIRCUITPY drive. "
            "Use --firmware-only for the serial-only player."
        )
    backup = install(plan, args.mount, args.device_id)
    print(f"Installed SD library and firmware. Backup: {backup}")


if __name__ == "__main__":
    main()
