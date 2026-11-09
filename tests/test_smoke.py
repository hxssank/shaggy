import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from firmware import smoke


def test_rejects_desktop_python() -> None:
    with pytest.raises(RuntimeError, match="not desktop Python"):
        smoke.main()


@pytest.fixture(params=["micropython", "circuitpython"])
def board(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    led = MagicMock()
    led.__enter__.return_value = led

    def exit_context(*args: object) -> bool:
        led.deinit()
        return False

    led.__exit__.side_effect = exit_context
    pin = Mock(return_value=led)
    pin.OUT = 1
    digital_in_out = Mock(return_value=led)
    board_name = (
        "Raspberry Pi Pico2 with RP2350"
        if request.param == "micropython"
        else "Raspberry Pi Pico 2 with rp2350a"
    )
    runtime = SimpleNamespace(
        implementation=SimpleNamespace(name=request.param, _machine=board_name),
        version="test runtime",
    )
    clock = SimpleNamespace(sleep=Mock())
    monkeypatch.setattr(smoke, "sys", runtime)
    monkeypatch.setattr(smoke, "time", clock)
    monkeypatch.setitem(
        sys.modules, "machine", SimpleNamespace(Pin=pin, freq=lambda: 150_000_000)
    )
    monkeypatch.setitem(sys.modules, "board", SimpleNamespace(LED="board LED"))
    monkeypatch.setitem(
        sys.modules, "digitalio", SimpleNamespace(DigitalInOut=digital_in_out)
    )
    monkeypatch.setitem(
        sys.modules,
        "microcontroller",
        SimpleNamespace(cpu=SimpleNamespace(frequency=150_000_000)),
    )
    monkeypatch.setitem(
        sys.modules,
        "gc",
        SimpleNamespace(collect=lambda: None, mem_free=lambda: 100_000),
    )
    return SimpleNamespace(
        led=led,
        pin=pin,
        digital_in_out=digital_in_out,
        runtime=runtime,
        clock=clock,
    )


def test_smoke_completes(board: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    smoke.main()
    if board.runtime.implementation.name == "micropython":
        board.pin.assert_called_once_with("LED", 1)
        assert board.led.on.call_count == 5
        assert board.led.method_calls[-1][0] == "off"
        board.digital_in_out.assert_not_called()
    else:
        board.digital_in_out.assert_called_once_with("board LED")
        board.led.switch_to_output.assert_called_once_with(value=False)
        assert board.led.value is False
        board.led.deinit.assert_called_once()
        board.pin.assert_not_called()
    assert board.clock.sleep.call_count == 10
    assert "PASS:" in capsys.readouterr().out


def test_rejects_wrong_board_before_gpio(board: SimpleNamespace) -> None:
    board.runtime.implementation._machine = "Raspberry Pi Pico with RP2040"
    with pytest.raises(RuntimeError, match="Expected a Pico 2"):
        smoke.main()
    board.pin.assert_not_called()
    board.digital_in_out.assert_not_called()


def test_led_turns_off_on_interrupt(board: SimpleNamespace) -> None:
    board.clock.sleep.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        smoke.main()
    if board.runtime.implementation.name == "micropython":
        assert board.led.method_calls[-1][0] == "off"
    else:
        assert board.led.value is False
        board.led.deinit.assert_called_once()
        assert board.led.__exit__.call_args.args[0] is KeyboardInterrupt
