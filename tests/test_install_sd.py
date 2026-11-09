import builtins
import contextlib
import hashlib
import io
import json
import shutil
import struct
import sys
import types
import wave
from pathlib import Path

import pytest

from tools import install_sd as installer

TOKEN = "20261029-184500-a1b2c3d4"


def make_project(root: Path) -> None:
    library = root / "audio" / "sd-library"
    bank = library / "house"
    bank.mkdir(parents=True)
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setparams((1, 2, 22050, 32, "NONE", "not compressed"))
        handle.writeframes(struct.pack("<h", 100) * 32)
    for name in installer.FILENAMES:
        (bank / name).write_bytes(output.getvalue())
    manifest = {
        "version": 1,
        "banks": [
            {
                "id": "house",
                "title": "HOUSE / 124 BPM",
                "bpm": 124,
                "sample_rate": 22050,
                "frames": 32,
            }
        ],
    }
    (library / "manifest.json").write_text(json.dumps(manifest))
    (root / "firmware").mkdir()
    for name in installer.MODULES:
        (root / "firmware" / name).write_text("value = 1\n")


class RemoteFilesystem:
    def __init__(self, root: Path, prefix: str = "/sd/") -> None:
        self.root = root
        self.prefix = prefix
        self.failed_rename = None

    def path(self, remote: str) -> Path:
        assert remote.startswith(self.prefix)
        path = self.root / remote.removeprefix(self.prefix)
        assert path.resolve().is_relative_to(self.root.resolve())
        return path

    def rename(self, source: str, target: str) -> None:
        if (source, target) == self.failed_rename:
            raise OSError("Injected rename failure")
        self.path(source).rename(self.path(target))

    def execute(self, source: str) -> None:
        real_import = builtins.__import__
        remote_os = types.SimpleNamespace(
            stat=lambda path: self.path(path).stat(),
            rename=self.rename,
            remove=lambda path: self.path(path).unlink(),
            sync=lambda: None,
        )

        def remote_import(name: str, *args: object, **kwargs: object) -> object:
            return remote_os if name == "os" else real_import(name, *args, **kwargs)

        def remote_open(path: str, *args: object, **kwargs: object) -> object:
            return self.path(path).open(*args, **kwargs)

        def remote_hash(path: str) -> tuple[int, str]:
            data = self.path(path).read_bytes()
            return len(data), hashlib.sha256(data).hexdigest()

        namespace = {
            "__builtins__": {**vars(builtins), "__import__": remote_import},
            "open": remote_open,
            "_shaggy_hash": remote_hash,
        }
        exec(source, namespace)

    def stage(self, paths: installer.StagePaths) -> None:
        self.path(paths.stage).mkdir(parents=True)
        self.path(paths.stage + "/" + installer.MARKER).write_text(
            json.dumps({"token": paths.token})
        )
        self.path(paths.stage + "/new.wav").write_bytes(b"new song")

    def previous(self) -> None:
        self.path(installer.LIBRARY).mkdir(parents=True)
        self.path(installer.LIBRARY + "/old.wav").write_bytes(b"old song")


@pytest.mark.parametrize(
    "token", ["../escape", "20261029-184500-deadbeef/../x", "20261029", "x'\n"]
)
def test_stage_paths_rejects_untrusted_tokens(token: str) -> None:
    with pytest.raises(ValueError):
        installer.stage_paths(token)


@pytest.mark.parametrize(
    "relative",
    ["../code.py", "/code.py", "house//x.wav", "house/./x.wav", "", "x\n", "x'y"],
)
def test_uploads_cannot_escape_the_unique_staging_folder(relative: str) -> None:
    with pytest.raises(ValueError):
        installer.remote_path(installer.stage_paths(TOKEN).stage, relative)


def test_uploads_cannot_target_live_library() -> None:
    with pytest.raises(ValueError, match="staging"):
        installer.remote_path(installer.LIBRARY, "manifest.json")


def test_prepare_validates_audio_and_places_entrypoint_last(tmp_path: Path) -> None:
    make_project(tmp_path)
    plan = installer.prepare(tmp_path)
    assert len(plan.audio) == 10
    assert list(plan.firmware)[-1] == "code.py"
    assert "metrics.json" not in plan.audio
    stem = tmp_path / "audio" / "sd-library" / "house" / installer.FILENAMES[0]
    stem.write_bytes(stem.read_bytes()[:-2])
    with pytest.raises(ValueError):
        installer.prepare(tmp_path)


def test_prepare_rejects_firmware_syntax_errors(tmp_path: Path) -> None:
    make_project(tmp_path)
    (tmp_path / "firmware" / "code.py").write_text("if:\n")
    with pytest.raises(SyntaxError):
        installer.prepare(tmp_path)


def test_dry_run_does_not_access_device(tmp_path: Path, monkeypatch, capsys) -> None:
    make_project(tmp_path)
    plan = installer.prepare(tmp_path)
    monkeypatch.setattr(installer, "prepare", lambda: plan)
    monkeypatch.setattr(
        installer, "install", lambda *args: pytest.fail("Dry run accessed USB")
    )
    monkeypatch.setattr(sys, "argv", ["install_sd", "--dry-run"])
    installer.main()
    assert json.loads(capsys.readouterr().out)["banks"] == ["house"]


def test_wrong_board_identity_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    (tmp_path / "boot_out.txt").write_text(
        "Board ID:raspberry_pi_pico2\nUID:WRONG_DEVICE"
    )
    with pytest.raises(OSError, match="selected Pico"):
        installer.check_device(tmp_path, installer.DEVICE_ID)


def test_atomic_replacement_needs_space_for_entire_new_file(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "code.py").write_bytes(b"x" * 30000)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: types.SimpleNamespace(free=20000)
    )
    with pytest.raises(OSError, match="internal flash"):
        installer.check_flash_space(tmp_path, {"code.py": b"y" * 30000})


def test_sd_activation_preserves_previous_library_and_rollback_preserves_both(
    tmp_path: Path,
) -> None:
    fs = RemoteFilesystem(tmp_path)
    paths = installer.stage_paths(TOKEN)
    fs.stage(paths)
    fs.previous()
    fs.execute(installer.activation_script(paths))
    assert fs.path(paths.backup + "/old.wav").read_bytes() == b"old song"
    assert fs.path(installer.LIBRARY + "/new.wav").read_bytes() == b"new song"
    fs.execute(installer.rollback_script(paths))
    assert fs.path(installer.LIBRARY + "/old.wav").read_bytes() == b"old song"
    assert fs.path(paths.failed + "/new.wav").read_bytes() == b"new song"


def test_activation_rename_failure_restores_previous_library(tmp_path: Path) -> None:
    fs = RemoteFilesystem(tmp_path)
    paths = installer.stage_paths(TOKEN)
    fs.stage(paths)
    fs.previous()
    fs.failed_rename = (paths.stage, installer.LIBRARY)
    with pytest.raises(OSError, match="rename failure"):
        fs.execute(installer.activation_script(paths))
    assert fs.path(installer.LIBRARY + "/old.wav").exists()
    assert fs.path(paths.stage + "/new.wav").exists()


def test_first_install_rollback_preserves_uploaded_files(tmp_path: Path) -> None:
    fs = RemoteFilesystem(tmp_path)
    paths = installer.stage_paths(TOKEN)
    fs.stage(paths)
    fs.execute(installer.activation_script(paths))
    fs.execute(installer.rollback_script(paths))
    assert not fs.path(installer.LIBRARY).exists()
    assert fs.path(paths.failed + "/new.wav").exists()


def test_rollback_refuses_to_replace_library_from_another_installer(
    tmp_path: Path,
) -> None:
    fs = RemoteFilesystem(tmp_path)
    paths = installer.stage_paths(TOKEN)
    fs.stage(paths)
    fs.previous()
    fs.execute(installer.activation_script(paths))
    fs.path(installer.LIBRARY + "/" + installer.MARKER).write_text(
        json.dumps({"token": "someone-else"})
    )
    with pytest.raises(AssertionError, match="Live library changed"):
        fs.execute(installer.rollback_script(paths))
    assert fs.path(installer.LIBRARY + "/new.wav").exists()
    assert fs.path(paths.backup + "/old.wav").exists()


def test_content_hash_detects_same_length_corruption(tmp_path: Path) -> None:
    fs = RemoteFilesystem(tmp_path)
    paths = installer.stage_paths(TOKEN)
    fs.stage(paths)
    path = paths.stage + "/new.wav"
    fs.execute(installer.verification_script(path, b"new song"))
    with pytest.raises(AssertionError, match="readback mismatch"):
        fs.execute(installer.verification_script(path, b"bad song"))


@pytest.fixture
def device(tmp_path: Path, monkeypatch):
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()
    (mount / "boot_out.txt").write_text(
        "Board ID:raspberry_pi_pico2\nUID:" + installer.DEVICE_ID
    )
    (mount / "code.py").write_bytes(b"old_code = 1\n")
    (mount / "sd_storage.py").write_bytes(b"old_storage = 1\n")
    (mount / "boot.py").write_bytes(b"old_boot = 1\n")
    (mount / "unrelated.txt").write_bytes(b"keep")
    fallback = mount / "beats" / "house"
    fallback.mkdir(parents=True)
    for name in installer.FILENAMES:
        (fallback / name).write_bytes(b"internal audio")
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)
    fs = RemoteFilesystem(tmp_path / "sd")
    fs.previous()

    class FakeRemote:
        instances = []
        corrupt_copy = False
        fail_reset = 0
        flash_owned = False
        fail_firmware = None

        def __init__(self, device_id: str, log: Path) -> None:
            self.commands = []
            self.resets = 0
            self.mounts = 0
            self.serial_writes = []
            self.instances.append(self)

        def reset(self, selected: Path) -> None:
            assert selected == mount
            self.resets += 1
            if self.resets == self.fail_reset:
                raise RuntimeError("Injected reconnect failure")

        def reset_serial(self) -> None:
            self.reset(mount)

        def execute(self, source: str, timeout: int = 30, **kwargs) -> str:
            self.commands.append((source, kwargs))
            if "Internal fallback changed" in source:
                def local_hash(path: str) -> tuple[int, str]:
                    data = (mount / path.lstrip("/")).read_bytes()
                    return len(data), hashlib.sha256(data).hexdigest()
                exec(source, {"_shaggy_hash": local_hash})
            elif source.startswith("import os\nif 'sd'"):
                (mount / "sd").mkdir(exist_ok=True)
            elif not source.startswith(("import supervisor", "_shaggy_sd.close")):
                fs.execute(source)
            return ""

        def device_owns_flash(self) -> bool:
            return self.flash_owned

        def verify_firmware(self, name: str, data: bytes | None) -> None:
            path = mount / name
            actual = path.read_bytes() if path.exists() else None
            assert actual == data, "Firmware changed"

        def write_firmware(self, name: str, data: bytes) -> None:
            self.serial_writes.append((name, data))
            if (name, data) == self.fail_firmware:
                raise OSError("Injected serial flash failure")
            (mount / name).write_bytes(data)

        def remove_firmware(self, name: str, expected: bytes) -> None:
            path = mount / name
            if path.exists():
                assert path.read_bytes() == expected
                path.unlink()

        def mount_sd(self, source: bytes) -> None:
            self.mounts += 1

        def create_stage(self, paths, audio) -> None:
            fs.path(paths.stage).mkdir(parents=True)
            for name in audio:
                fs.path(paths.stage + "/" + name).parent.mkdir(exist_ok=True)

        def copy(self, local: Path, destination: str) -> None:
            data = local.read_bytes()
            fs.path(destination).write_bytes(b"bad" if self.corrupt_copy else data)

    monkeypatch.setattr(installer, "Remote", FakeRemote)
    plan = installer.InstallPlan(
        audio={"manifest.json": b"{}", "house/stem1_kick.wav": b"new audio"},
        firmware={
            "sd_storage.py": b"new_storage = 2\n",
            "new_module.py": b"new_module = 2\n",
            "code.py": b"new_code = 2\n",
        },
        banks=[],
    )
    return mount, fs, FakeRemote, plan


def test_install_verifies_audio_then_writes_modules_before_code(
    tmp_path: Path, monkeypatch, device
) -> None:
    mount, fs, remote, plan = device
    real_write = installer.atomic_write
    writes = []

    def track(path: Path, data: bytes) -> None:
        if path.parent == mount:
            writes.append(path.name)
            assert fs.path(installer.LIBRARY + "/manifest.json").exists()
        real_write(path, data)

    monkeypatch.setattr(installer, "atomic_write", track)
    backup = installer.install(plan, mount=mount, root=tmp_path)
    assert writes == ["sd_storage.py", "new_module.py", "code.py"]
    assert (backup / "firmware-before" / "boot.py").read_bytes() == b"old_boot = 1\n"
    assert json.loads((backup / "install.json").read_text())["status"] == "installed"
    assert (mount / "unrelated.txt").read_bytes() == b"keep"
    assert (mount / "beats" / "house" / installer.FILENAMES[0]).read_bytes() == (
        b"internal audio"
    )
    assert remote.instances[0].commands[0][1] == {"fresh": True}


def test_failed_sd_readback_leaves_original_firmware_and_library(
    tmp_path: Path, device
) -> None:
    mount, fs, remote, plan = device
    remote.corrupt_copy = True
    with pytest.raises(AssertionError, match="readback mismatch"):
        installer.install(plan, mount=mount, root=tmp_path)
    assert (mount / "code.py").read_bytes() == b"old_code = 1\n"
    assert not (mount / "new_module.py").exists()
    assert fs.path(installer.LIBRARY + "/old.wav").read_bytes() == b"old song"


def test_firmware_write_failure_restores_modules_code_and_sd(
    tmp_path: Path, monkeypatch, device
) -> None:
    mount, fs, remote, plan = device
    real_write = installer.atomic_write

    def fail_new_code(path: Path, data: bytes) -> None:
        if path == mount / "code.py" and data == plan.firmware["code.py"]:
            raise OSError("Injected flash failure")
        real_write(path, data)

    monkeypatch.setattr(installer, "atomic_write", fail_new_code)
    with pytest.raises(OSError, match="flash failure"):
        installer.install(plan, mount=mount, root=tmp_path)
    assert (mount / "code.py").read_bytes() == b"old_code = 1\n"
    assert (mount / "sd_storage.py").read_bytes() == b"old_storage = 1\n"
    assert not (mount / "new_module.py").exists()
    assert fs.path(installer.LIBRARY + "/old.wav").exists()


def test_reconnect_failure_after_sd_close_remounts_and_rolls_back(
    tmp_path: Path, device
) -> None:
    mount, fs, remote, plan = device
    remote.fail_reset = 2
    with pytest.raises(RuntimeError, match="Injected reconnect failure"):
        installer.install(plan, mount=mount, root=tmp_path)
    assert remote.instances[0].mounts == 2
    assert (mount / "code.py").read_bytes() == b"old_code = 1\n"
    assert fs.path(installer.LIBRARY + "/old.wav").exists()


@pytest.fixture
def serial_transport(monkeypatch):
    from mpremote import transport_serial
    from serial.tools import list_ports

    class FakeSerialTransport:
        instances = []

        def __init__(self, device: str, timeout: int) -> None:
            self.namespace = {}
            self.files = {}
            self.entries = []
            self.closed = False
            self.instances.append(self)

        def enter_raw_repl(self, **kwargs) -> None:
            self.entries.append(kwargs)

        def exec_raw(self, source: str, timeout: int) -> tuple[bytes, bytes]:
            assert not self.closed
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(source, self.namespace)
            return output.getvalue().encode(), b""

        def fs_writefile(self, destination: str, data: bytes, chunk_size: int) -> None:
            assert not self.closed
            assert chunk_size == 1024
            self.files[destination] = data

        def close(self) -> None:
            self.closed = True
            self.namespace.clear()

    monkeypatch.setattr(transport_serial, "SerialTransport", FakeSerialTransport)
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            types.SimpleNamespace(
                device="/dev/fake-pico", serial_number=installer.DEVICE_ID
            )
        ],
    )
    return FakeSerialTransport


def test_one_raw_repl_connection_preserves_globals_across_copy_and_exec(
    tmp_path: Path, serial_transport
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    (tmp_path / "song.wav").write_bytes(b"song")
    remote.execute("value = 1", fresh=True)
    remote.copy(tmp_path / "song.wav", "/sd/shaggy/test.wav")
    remote.execute("assert value == 1")
    assert len(serial_transport.instances) == 1
    connection = serial_transport.instances[0]
    assert connection.files == {"/sd/shaggy/test.wav": b"song"}
    assert connection.entries == [{"soft_reset": True, "timeout_overall": 20}]
    assert not connection.closed
    remote.close()
    assert connection.closed


def test_clean_exec_cannot_clear_an_existing_sd_session(
    tmp_path: Path, serial_transport
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    remote.execute("value = 1729", fresh=True)
    with pytest.raises(RuntimeError, match="active SD transfer"):
        remote.execute("value = 0", fresh=True)
    remote.execute("assert value == 1729")


def test_reset_closes_persistent_connection_before_transient_reset_command(
    tmp_path: Path, monkeypatch, serial_transport
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    remote.execute("value = 1", fresh=True)

    def reset_command(arguments: list[str], timeout: int) -> str:
        assert serial_transport.instances[0].closed
        assert remote._transport is None
        assert "microcontroller.reset()" in arguments[-1]
        raise RuntimeError("Stop before hardware polling")

    monkeypatch.setattr(remote, "_run", reset_command)
    with pytest.raises(RuntimeError, match="Stop before hardware"):
        remote.reset(tmp_path)


def test_copy_requires_existing_raw_repl_session(tmp_path: Path) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    with pytest.raises(RuntimeError, match="active raw REPL"):
        remote.copy(tmp_path / "song.wav", "/sd/shaggy/test.wav")


@pytest.mark.parametrize(
    ("stdout", "stderr", "message"),
    [
        (b"incomplete\n", b"", "did not complete"),
        (installer.SUCCESS.encode() + b"\n", b"Traceback", "command failed"),
    ],
)
def test_incomplete_or_errored_remote_exec_cannot_count_as_success(
    tmp_path: Path, monkeypatch, stdout: bytes, stderr: bytes, message: str
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    transport = types.SimpleNamespace(exec_raw=lambda *args, **kwargs: (stdout, stderr))
    monkeypatch.setattr(remote, "_connect", lambda **kwargs: transport)
    with pytest.raises(RuntimeError, match=message):
        remote.execute("assert False")


@pytest.mark.parametrize("name", ["../code.py", "/code.py", "code.py/x", "bad'\n.py"])
def test_firmware_names_cannot_escape_flash_root(name: str) -> None:
    with pytest.raises(ValueError, match="filename"):
        installer.firmware_path(name)


def test_serial_firmware_activation_keeps_old_file_until_staged_hash_passes(
    tmp_path: Path,
) -> None:
    fs = RemoteFilesystem(tmp_path, prefix="/")
    fs.path("/code.py").write_bytes(b"old")
    fs.path("/.stage.tmp").write_bytes(b"bad")
    source = installer.firmware_activation_script(
        "/code.py", "/.stage.tmp", "/.previous.bak", b"new"
    )
    with pytest.raises(AssertionError, match="readback mismatch"):
        fs.execute(source)
    assert fs.path("/code.py").read_bytes() == b"old"
    assert not fs.path("/.previous.bak").exists()


def test_serial_firmware_activation_restores_old_file_if_rename_fails(
    tmp_path: Path,
) -> None:
    fs = RemoteFilesystem(tmp_path, prefix="/")
    fs.path("/code.py").write_bytes(b"old")
    fs.path("/.stage.tmp").write_bytes(b"new")
    fs.failed_rename = ("/.stage.tmp", "/code.py")
    source = installer.firmware_activation_script(
        "/code.py", "/.stage.tmp", "/.previous.bak", b"new"
    )
    with pytest.raises(OSError, match="rename failure"):
        fs.execute(source)
    assert fs.path("/code.py").read_bytes() == b"old"
    assert fs.path("/.stage.tmp").read_bytes() == b"new"
    assert not fs.path("/.previous.bak").exists()


def test_serial_firmware_activation_verifies_replacement_and_removes_backup(
    tmp_path: Path,
) -> None:
    fs = RemoteFilesystem(tmp_path, prefix="/")
    fs.path("/code.py").write_bytes(b"old")
    fs.path("/.stage.tmp").write_bytes(b"new")
    fs.execute(
        installer.firmware_activation_script(
            "/code.py", "/.stage.tmp", "/.previous.bak", b"new"
        )
    )
    assert fs.path("/code.py").read_bytes() == b"new"
    assert not fs.path("/.stage.tmp").exists()
    assert not fs.path("/.previous.bak").exists()


def test_device_owned_flash_installs_without_host_writes(
    tmp_path: Path, monkeypatch, device
) -> None:
    mount, fs, remote, plan = device
    remote.flash_owned = True
    real_write = installer.atomic_write

    def reject_host_write(path: Path, data: bytes) -> None:
        assert path.parent != mount, "Host tried to write device-owned flash"
        real_write(path, data)

    monkeypatch.setattr(installer, "atomic_write", reject_host_write)
    backup = installer.install(plan, mount=mount, root=tmp_path)
    assert remote.instances[0].serial_writes == list(plan.firmware.items())
    assert (mount / "code.py").read_bytes() == plan.firmware["code.py"]
    assert json.loads((backup / "install.json").read_text())["status"] == "installed"


def test_device_owned_flash_failure_rolls_back_over_serial(
    tmp_path: Path, monkeypatch, device
) -> None:
    mount, fs, remote, plan = device
    remote.flash_owned = True
    remote.fail_firmware = ("code.py", plan.firmware["code.py"])
    real_write = installer.atomic_write

    def reject_host_write(path: Path, data: bytes) -> None:
        assert path.parent != mount, "Rollback tried to write from host"
        real_write(path, data)

    monkeypatch.setattr(installer, "atomic_write", reject_host_write)
    with pytest.raises(OSError, match="serial flash failure"):
        installer.install(plan, mount=mount, root=tmp_path)
    assert (mount / "code.py").read_bytes() == b"old_code = 1\n"
    assert (mount / "sd_storage.py").read_bytes() == b"old_storage = 1\n"
    assert not (mount / "new_module.py").exists()
    assert fs.path(installer.LIBRARY + "/old.wav").exists()


@pytest.mark.parametrize("readonly", [False, True])
def test_flash_ownership_uses_device_mount_state(
    tmp_path: Path, monkeypatch, serial_transport, readonly: bool
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "storage",
        types.SimpleNamespace(
            getmount=lambda path: types.SimpleNamespace(readonly=readonly)
        ),
    )
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    assert remote.device_owns_flash() is not readonly
    assert len(serial_transport.instances) == 1


def test_rollback_rechecks_flash_owner_after_new_boot_file_runs(
    tmp_path: Path, monkeypatch, device
) -> None:
    mount, fs, remote, plan = device
    plan.firmware["boot.py"] = b"new_boot = 2\n"
    remote.fail_reset = 2
    monkeypatch.setattr(
        remote, "device_owns_flash", lambda self: self.resets >= 2
    )
    with pytest.raises(RuntimeError, match="Injected reconnect failure"):
        installer.install(plan, mount=mount, root=tmp_path)
    assert ("boot.py", b"old_boot = 1\n") in remote.instances[0].serial_writes
    assert (mount / "boot.py").read_bytes() == b"old_boot = 1\n"
    assert (mount / "code.py").read_bytes() == b"old_code = 1\n"
    assert fs.path(installer.LIBRARY + "/old.wav").exists()


@pytest.fixture
def serial_update_device(monkeypatch):
    files = {
        "boot.py": b"old_boot = 1\n",
        "shaggy.py": b"old_player = 1\n",
        "code.py": b"old_code = 1\n",
    }

    class FakeRemote:
        instances = []
        flash_owned = True
        fail_write = None
        fail_reset = 0

        def __init__(self, device_id: str, log: Path) -> None:
            assert device_id == installer.DEVICE_ID
            self.reads = []
            self.writes = []
            self.resets = 0
            self.closed = False
            self.instances.append(self)

        def execute(self, source: str, **kwargs) -> str:
            assert source.startswith("import supervisor")
            self.closed = False
            return ""

        def device_owns_flash(self) -> bool:
            return self.flash_owned

        def read_firmware(self, name: str) -> bytes | None:
            self.reads.append(name)
            return files.get(name)

        def verify_firmware(self, name: str, data: bytes | None) -> None:
            assert files.get(name) == data

        def check_firmware_space(self, firmware, original) -> None:
            assert set(original) == set(installer.MODULES)
            assert set(firmware) <= set(original)

        def write_firmware(self, name: str, data: bytes) -> None:
            self.writes.append((name, data))
            if (name, data) == self.fail_write:
                raise OSError("Injected firmware-only failure")
            files[name] = data

        def remove_firmware(self, name: str, expected: bytes) -> None:
            if name in files:
                assert files[name] == expected
                del files[name]

        def reset_serial(self) -> None:
            self.resets += 1
            if self.resets == self.fail_reset:
                raise RuntimeError("Injected serial reconnect failure")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(installer, "Remote", FakeRemote)
    monkeypatch.setattr(
        installer, "check_device", lambda *args: pytest.fail("Accessed USB volume")
    )
    return files, FakeRemote


def test_firmware_only_update_backs_up_all_modules_and_needs_no_volume(
    tmp_path: Path, serial_update_device
) -> None:
    files, remote = serial_update_device
    firmware = {
        "code.py": b"new_code = 2\n",
        "shaggy.py": b"new_player = 2\n",
        "bank_cache.py": b"new_cache = 2\n",
    }
    backup = installer.install_firmware(firmware, root=tmp_path)
    instance = remote.instances[0]
    assert instance.reads == list(installer.MODULES)
    assert [name for name, _ in instance.writes] == [
        "bank_cache.py", "shaggy.py", "code.py"
    ]
    assert (backup / "firmware-before/boot.py").read_bytes() == b"old_boot = 1\n"
    assert (backup / "firmware-before/code.py").read_bytes() == b"old_code = 1\n"
    report = json.loads((backup / "install.json").read_text())
    assert report["status"] == "installed"
    assert report["before_sha256"]["bank_cache.py"] is None
    assert instance.resets == 1 and instance.closed
    assert files["code.py"] == firmware["code.py"]


def test_firmware_only_failure_restores_original_modules_and_removes_new_ones(
    tmp_path: Path, serial_update_device
) -> None:
    files, remote = serial_update_device
    original = dict(files)
    firmware = {
        "bank_cache.py": b"new_cache = 2\n",
        "shaggy.py": b"new_player = 2\n",
        "code.py": b"new_code = 2\n",
    }
    remote.fail_write = ("code.py", firmware["code.py"])
    with pytest.raises(OSError, match="firmware-only failure"):
        installer.install_firmware(firmware, root=tmp_path)
    assert files == original
    instance = remote.instances[0]
    assert instance.writes[-1] == ("code.py", original["code.py"])
    assert instance.resets == 1 and instance.closed
    report_path = next((tmp_path / ".hardware-tests").glob("*/install.json"))
    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["recovery_errors"] == []


def test_firmware_only_reconnect_failure_restores_original_firmware(
    tmp_path: Path, serial_update_device
) -> None:
    files, remote = serial_update_device
    original = dict(files)
    remote.fail_reset = 1
    with pytest.raises(RuntimeError, match="serial reconnect failure"):
        installer.install_firmware({"code.py": b"new_code = 2\n"}, root=tmp_path)
    assert files == original
    assert remote.instances[0].resets == 2


def test_firmware_only_refuses_host_owned_flash_without_writes(
    tmp_path: Path, serial_update_device
) -> None:
    files, remote = serial_update_device
    remote.flash_owned = False
    with pytest.raises(RuntimeError, match="device-owned flash"):
        installer.install_firmware({"code.py": b"new_code = 2\n"}, root=tmp_path)
    assert remote.instances[0].reads == []
    assert remote.instances[0].writes == []
    assert remote.instances[0].resets == 1


def test_firmware_only_unchanged_files_do_not_wear_flash(
    tmp_path: Path, serial_update_device
) -> None:
    files, remote = serial_update_device
    installer.install_firmware({"code.py": files["code.py"]}, root=tmp_path)
    assert remote.instances[0].writes == []


def test_firmware_only_dry_run_does_not_load_audio_or_open_usb(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    firmware = {"code.py": b"code = 1\n"}
    monkeypatch.setattr(installer, "prepare_firmware", lambda: firmware)
    monkeypatch.setattr(installer, "prepare", lambda: pytest.fail("Loaded audio"))
    monkeypatch.setattr(
        installer, "install_firmware", lambda *args: pytest.fail("Opened USB")
    )
    monkeypatch.setattr(
        sys, "argv", ["install_sd", "--firmware-only", "--dry-run"]
    )
    installer.main()
    assert json.loads(capsys.readouterr().out) == {"firmware_files": ["code.py"]}


def test_serial_reset_uses_current_connection_and_polls_only_usb_identity(
    tmp_path: Path, monkeypatch, serial_transport
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    remote.execute("value = 1", fresh=True)
    connection = serial_transport.instances[0]
    commands = []
    connection.exec_raw_no_follow = commands.append
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        installer, "check_device", lambda *args: pytest.fail("Needed USB volume")
    )
    remote.reset_serial()
    assert len(serial_transport.instances) == 1
    assert len(commands) == 1 and "microcontroller.reset()" in commands[0]
    assert connection.closed


def test_serial_backup_read_verifies_received_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    remote = installer.Remote(installer.DEVICE_ID, tmp_path / "serial.log")
    transport = types.SimpleNamespace(
        fs_stat=lambda path: types.SimpleNamespace(st_size=7),
        fs_readfile=lambda path, chunk_size: bytearray(b"code=1\n"),
    )
    verified = []
    monkeypatch.setattr(remote, "_connect", lambda: transport)
    monkeypatch.setattr(
        remote, "verify_firmware", lambda name, data: verified.append((name, data))
    )
    assert remote.read_firmware("code.py") == b"code=1\n"
    assert verified == [("code.py", b"code=1\n")]
