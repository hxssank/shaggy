import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def hardware(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(events=[], pins={}, fail={}, mounted=None)

    def event(name: str, *values: object) -> None:
        state.events.append((name, *values))
        error = state.fail.pop(name, None)
        if error is not None:
            raise error

    class Output:
        def __init__(self, pin: str) -> None:
            if pin in state.pins:
                raise ValueError("%s already in use" % pin)
            self.pin = pin
            self.value = None
            state.pins[pin] = self
            event("claim", pin)

        def switch_to_output(self, *, value: bool) -> None:
            event("output_" + self.pin, value)
            self.value = value

        def deinit(self) -> None:
            if state.pins.get(self.pin) is self:
                del state.pins[self.pin]
                event("release", self.pin)

    class SPI:
        def __init__(self, **pins: str) -> None:
            assert pins == {"clock": "GP22", "MOSI": "GP19", "MISO": "GP20"}
            assert state.pins["GP12"].value is True
            assert state.pins["GP21"].value is True
            self.closed = False
            event("spi_create")

        def deinit(self) -> None:
            self.closed = True
            event("spi_close")

    class Card:
        def __init__(self, spi: SPI, cs: str, *, baudrate: int) -> None:
            assert not spi.closed
            assert state.pins["GP12"].value is True
            assert cs == "GP21"
            self.cs = Output(cs)
            self.cs.switch_to_output(value=True)
            self.closed = False
            try:
                event("card_create", baudrate)
            except Exception:
                self.cs.deinit()
                raise

        def deinit(self) -> None:
            self.closed = True
            self.cs.deinit()
            event("card_close")

    def vfs(card: Card) -> object:
        assert not card.closed
        event("vfs_create")
        return object()

    def mount(filesystem: object, path: str, *, readonly: bool) -> None:
        event("mount", path, readonly)
        state.mounted = filesystem

    def umount(path: str) -> None:
        state.mounted = None
        event("umount", path)

    for name, fake in {
        "board": SimpleNamespace(
            **{pin: pin for pin in ("GP12", "GP21", "GP22", "GP19", "GP20")}
        ),
        "busio": SimpleNamespace(SPI=SPI),
        "digitalio": SimpleNamespace(DigitalInOut=Output),
        "sdcardio": SimpleNamespace(SDCard=Card),
        "storage": SimpleNamespace(VfsFat=vfs, mount=mount, umount=umount),
    }.items():
        monkeypatch.setitem(sys.modules, name, fake)
    spec = importlib.util.spec_from_file_location(
        "sd_storage_test", Path(__file__).parents[1] / "firmware/sd_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state.module = module
    state.output = Output
    return state


def test_chip_selects_are_high_before_bus_and_card_setup(
    hardware: SimpleNamespace,
) -> None:
    device = hardware.module.SDStorage(baudrate=12_000_000)
    assert hardware.events[:5] == [
        ("claim", "GP12"),
        ("output_GP12", True),
        ("claim", "GP21"),
        ("output_GP21", True),
        ("spi_create",),
    ]
    assert device.mount()
    assert ("card_create", 12_000_000) in hardware.events
    assert hardware.events[-1] == ("mount", "/sd", True)
    assert device.mounted and device.readonly is True
    assert device.error is None
    device.close()
    assert not hardware.pins


@pytest.mark.parametrize("readonly", [True, False])
def test_mount_access_is_explicit_and_cannot_change_in_place(
    hardware: SimpleNamespace, readonly: bool
) -> None:
    device = hardware.module.SDStorage()
    assert device.mount(readonly=readonly)
    events = list(hardware.events)
    assert device.mount(readonly=readonly)
    assert hardware.events == events
    assert ("mount", "/sd", readonly) in hardware.events
    with pytest.raises(RuntimeError, match="different access"):
        device.mount(readonly=not readonly)
    device.close()


@pytest.mark.parametrize("failure", ["card_create", "vfs_create", "mount"])
def test_missing_or_unmountable_card_keeps_bus_and_sd_deselected(
    hardware: SimpleNamespace, failure: str
) -> None:
    hardware.fail[failure] = OSError("card unavailable")
    device = hardware.module.SDStorage()
    spi = device.spi
    assert not device.mount()
    assert "card unavailable" in device.error
    assert not device.mounted
    assert device.readonly is None
    assert device.spi is spi and not spi.closed
    assert hardware.pins["GP21"].value is True
    device.release_display_cs()
    display_cs = hardware.output("GP12")
    display_cs.switch_to_output(value=True)
    display_cs.deinit()
    device.park_display_cs()
    assert device.mount()
    assert device.error is None
    device.close()
    assert not hardware.pins


def test_display_can_release_and_reclaim_cs_across_bank_switches(
    hardware: SimpleNamespace,
) -> None:
    device = hardware.module.SDStorage()
    assert device.mount()
    spi = device.spi
    card = device._card
    for _ in range(3):
        device.release_display_cs()
        device.release_display_cs()
        display_cs = hardware.output("GP12")
        display_cs.switch_to_output(value=True)
        assert device.spi is spi and not spi.closed
        assert device._card is card and not card.closed
        display_cs.deinit()
        device.park_display_cs()
    assert sum(event[0] == "spi_create" for event in hardware.events) == 1
    assert sum(event[0] == "card_create" for event in hardware.events) == 1
    device.close()


def test_mount_requires_parked_display(hardware: SimpleNamespace) -> None:
    device = hardware.module.SDStorage()
    device.release_display_cs()
    with pytest.raises(RuntimeError, match="Park the display"):
        device.mount()
    assert not any(event[0] == "card_create" for event in hardware.events)
    device.close()


@pytest.mark.parametrize("failure", ["output_GP21", "spi_create"])
def test_partial_startup_releases_allocated_pins(
    hardware: SimpleNamespace, failure: str
) -> None:
    hardware.fail[failure] = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        hardware.module.SDStorage()
    assert not hardware.pins


def test_close_orders_cleanup_and_is_idempotent(hardware: SimpleNamespace) -> None:
    device = hardware.module.SDStorage()
    assert device.mount()
    hardware.events.clear()
    device.close()
    assert hardware.events == [
        ("umount", "/sd"),
        ("release", "GP21"),
        ("card_close",),
        ("spi_close",),
        ("release", "GP12"),
    ]
    events = list(hardware.events)
    device.close()
    assert hardware.events == events
    assert not device.mounted and device.spi is None
    with pytest.raises(RuntimeError, match="closed"):
        device.mount()


@pytest.mark.parametrize("failure", ["umount", "card_close", "spi_close"])
def test_cleanup_attempts_remaining_resources_after_error(
    hardware: SimpleNamespace, failure: str
) -> None:
    device = hardware.module.SDStorage()
    assert device.mount()
    hardware.fail[failure] = OSError("cleanup failed")
    with pytest.raises(OSError, match="cleanup failed"):
        device.close()
    assert not hardware.pins
    assert hardware.mounted is None
    assert device.spi is None


def test_close_refuses_to_destroy_bus_while_display_owns_cs(
    hardware: SimpleNamespace,
) -> None:
    device = hardware.module.SDStorage()
    assert device.mount()
    device.release_display_cs()
    display_cs = hardware.output("GP12")
    with pytest.raises(ValueError, match="already in use"):
        device.close()
    assert device.mounted and not device.spi.closed
    display_cs.deinit()
    device.close()
    assert not hardware.pins
