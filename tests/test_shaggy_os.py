import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from firmware import library_control


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        events=[],
        cards=[],
        screens=[],
        outcomes=[False],
        mount_result=True,
        screen_error=None,
        close_error=None,
        close_before_release_error=None,
        display_pin_owner=None,
    )

    class Card:
        def __init__(self) -> None:
            assert state.display_pin_owner is None
            self.index = len(state.cards)
            self.spi = SimpleNamespace(closed=False)
            self.released = False
            self.error = "no SD card"
            self.mount_count = 0
            self.closed = False
            state.cards.append(self)
            state.events.append(("card_create", self.index))

        def mount(self) -> bool:
            self.mount_count += 1
            state.events.append(("mount", self.index))
            return state.mount_result

        def release_display_cs(self) -> None:
            assert not self.closed
            self.released = True
            state.events.append(("release_display_cs", self.index))

        def close(self) -> None:
            if state.display_pin_owner is not None:
                raise ValueError("GP12 already in use")
            assert all(screen.closed for screen in state.screens if screen.card is self)
            self.closed = True
            self.spi.closed = True
            state.events.append(("card_close", self.index))

    class Screen:
        def __init__(self, *, spi: object) -> None:
            self.card = next(card for card in state.cards if card.spi is spi)
            assert self.card.released
            assert not spi.closed
            assert state.display_pin_owner is None
            self.closed = False
            state.display_pin_owner = self
            state.events.append(("display_pin_claim", self.card.index))
            if state.screen_error is not None:
                error, state.screen_error = state.screen_error, None
                raise error
            self.set_bank = Mock(
                side_effect=lambda title, **kwargs: state.events.append(
                    ("set_bank", self.card.index, title)
                )
            )
            self.set_state = Mock(
                side_effect=lambda message: state.events.append(
                    ("set_state", self.card.index, message)
                )
            )
            self.show_error = Mock()
            state.screens.append(self)
            state.events.append(("screen_create", self.card.index))

        def close(self) -> None:
            if state.close_before_release_error is not None:
                error, state.close_before_release_error = (
                    state.close_before_release_error,
                    None,
                )
                raise error
            self.closed = True
            state.display_pin_owner = None
            state.events.append(("screen_close", self.card.index))
            if state.close_error is not None:
                error, state.close_error = state.close_error, None
                raise error

    def release_displays() -> None:
        if state.display_pin_owner is not None:
            owner = state.display_pin_owner
            owner.closed = True
            state.display_pin_owner = None
            state.events.append(("release_displays", owner.card.index))

    def play(duration: float | None, **settings: object) -> bool:
        screen = settings["screen"]
        assert not screen.closed and not screen.card.spi.closed
        state.events.append(("play", screen.card.index, settings["audio_root"]))
        outcome = state.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(settings)
        return outcome

    def prepare(bank: dict, *, progress: object) -> str:
        state.events.append(("prepare_bank", bank["id"]))
        progress("CHECKING BEAT")
        if bank["id"] == "house":
            return "/beats/house"
        for index in range(1, 10):
            progress("LOADING %d/9" % index)
        progress("CHECKING BEAT")
        return "/verified-cache/" + bank["id"]

    banks = [
        {
            "id": "neon-rush",
            "title": "NEON RUSH / 138 BPM",
            "audio_root": "/sd/shaggy/library/neon-rush",
        },
        {
            "id": "house",
            "title": "HOUSE / 124 BPM",
            "audio_root": "/sd/shaggy/library/house",
            "instruments": (("KICK", "Kick drum"),) * 9,
        },
        {
            "id": "block-party",
            "title": "BLOCK PARTY / 140 BPM",
            "audio_root": "/sd/shaggy/library/block-party",
        },
    ]
    state.load = Mock(return_value=banks)
    state.play = Mock(side_effect=play)
    state.prepare = Mock(side_effect=prepare)
    state.release_displays = Mock(side_effect=release_displays)
    for name, module in {
        "displayio": SimpleNamespace(release_displays=state.release_displays),
        "bank_cache": SimpleNamespace(prepare_bank=state.prepare),
        "library_control": library_control,
        "music_library": SimpleNamespace(load_library=state.load),
        "player_display": SimpleNamespace(PlayerDisplay=Screen),
        "sd_storage": SimpleNamespace(SDStorage=Card),
        "shaggy": SimpleNamespace(run=state.play),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "shaggy_os_test", Path(__file__).parents[1] / "firmware/shaggy_os.py"
    )
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    state.program = program
    state.banks = banks
    return state


def select_last_bank(settings: dict) -> bool:
    control = settings["library_control"]
    control.update(0, 0, 0)
    assert control.update(0, 0, 2) == "open"
    control.update(100, 0, 0)
    control.update(100, 0, 0.2)
    assert control.update(100, 1, 0) == "select"
    return True


def test_bank_switch_reuses_display_bus_card_and_controller(
    system: SimpleNamespace,
) -> None:
    system.outcomes = [select_last_bank, False]
    system.program.run(0.2)
    assert len(system.cards) == len(system.screens) == 1
    assert system.cards[0].mount_count == 1
    first, second = [call.kwargs for call in system.play.call_args_list]
    assert first["audio_root"] == "/beats/house"
    assert second["audio_root"] == "/verified-cache/block-party"
    assert first["screen"] is second["screen"] is system.screens[0]
    assert first["library_control"] is second["library_control"]
    assert first["wait_for_zero"] is False and second["wait_for_zero"] is True
    assert first["instruments"] == system.banks[1]["instruments"]
    assert first["source"] == second["source"] == "FLASH"
    assert first["raise_errors"] and second["raise_errors"]
    assert system.events[-2:] == [("screen_close", 0), ("card_close", 0)]
    assert system.screens[0].set_bank.call_count == 2
    assert all(
        call.kwargs["source"] == "SD"
        for call in system.screens[0].set_bank.call_args_list
    )
    assert [call.args[0] for call in system.prepare.call_args_list] == [
        system.banks[1],
        system.banks[2],
    ]
    assert all(
        call.kwargs["progress"] is system.screens[0].set_state
        for call in system.prepare.call_args_list
    )


def test_missing_card_plays_internal_house_without_loading_manifest(
    system: SimpleNamespace,
) -> None:
    system.mount_result = False
    system.program.run(0.1)
    settings = system.play.call_args.kwargs
    assert settings["source"] == "INTERNAL"
    assert settings["audio_root"] == "/beats/house"
    assert settings["library_control"] is None
    system.load.assert_not_called()
    system.prepare.assert_not_called()
    system.screens[0].set_state.assert_called_once_with("SD UNAVAILABLE")
    assert system.cards[0].closed


def test_loading_message_precedes_cache_preparation_and_flash_playback(
    system: SimpleNamespace,
) -> None:
    system.outcomes = [select_last_bank, False]
    system.program.run(0.1)
    assert [
        event
        for event in system.events
        if event[0] in ("set_bank", "set_state", "prepare_bank", "play")
    ] == [
        ("set_bank", 0, "HOUSE / 124 BPM"),
        ("set_state", 0, "LOADING BEAT"),
        ("prepare_bank", "house"),
        ("set_state", 0, "CHECKING BEAT"),
        ("play", 0, "/beats/house"),
        ("set_bank", 0, "BLOCK PARTY / 140 BPM"),
        ("set_state", 0, "LOADING BEAT"),
        ("prepare_bank", "block-party"),
        ("set_state", 0, "CHECKING BEAT"),
        *[("set_state", 0, "LOADING %d/9" % index) for index in range(1, 10)],
        ("set_state", 0, "CHECKING BEAT"),
        ("play", 0, "/verified-cache/block-party"),
    ]


@pytest.mark.parametrize(
    "error",
    [
        OSError("flash is read-only"),
        OSError("insufficient cache space"),
        OSError("SD read failed"),
        ValueError("cache verification failed"),
    ],
)
def test_cache_failure_uses_internal_fallback_after_zero_takeover(
    system: SimpleNamespace, error: Exception
) -> None:
    system.prepare.side_effect = error
    system.program.run(0.1)

    system.play.assert_called_once()
    settings = system.play.call_args.kwargs
    assert settings["audio_root"] == "/beats/house"
    assert settings["source"] == "INTERNAL"
    assert settings["wait_for_zero"] is True
    assert settings["library_control"] is None
    system.prepare.assert_called_once_with(
        system.banks[1], progress=system.screens[0].set_state
    )
    system.screens[0].set_state.assert_called_once_with("LOADING BEAT")
    assert system.events.index(("card_close", 0)) < system.events.index(
        ("play", 1, "/beats/house")
    )
    assert all(card.closed for card in system.cards)
    assert all(screen.closed for screen in system.screens)


def test_cache_failure_after_selection_does_not_play_the_wrong_bank(
    system: SimpleNamespace,
) -> None:
    system.outcomes = [select_last_bank, False]
    system.prepare.side_effect = ["/beats/house", ValueError("verification failed")]
    system.program.run(0.1)

    first, fallback = [call.kwargs for call in system.play.call_args_list]
    assert first["source"] == "FLASH"
    assert first["wait_for_zero"] is False
    assert fallback["source"] == "INTERNAL"
    assert fallback["audio_root"] == "/beats/house"
    assert fallback["beat_title"] == "HOUSE / 124 BPM"
    assert fallback["wait_for_zero"] is True
    assert [call.args[0] for call in system.prepare.call_args_list] == [
        system.banks[1],
        system.banks[2],
    ]


@pytest.mark.parametrize(
    "error", [OSError("missing manifest"), ValueError("bad manifest")]
)
def test_invalid_library_falls_back_to_internal_and_closes_mounted_card(
    system: SimpleNamespace, error: Exception
) -> None:
    system.load.side_effect = error
    system.program.run(0.1)
    assert system.play.call_args.kwargs["source"] == "INTERNAL"
    assert system.play.call_args.kwargs["audio_root"] == "/beats/house"
    assert len(system.cards) == 1 and system.cards[0].closed
    system.prepare.assert_not_called()
    system.screens[0].set_state.assert_called_once_with("LIBRARY ERROR")


def test_sd_runtime_failure_closes_resources_before_internal_retry(
    system: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    system.outcomes = [OSError("card removed"), False]
    system.program.run(0.1)
    calls = system.play.call_args_list
    assert [call.kwargs["source"] for call in calls] == ["FLASH", "INTERNAL"]
    assert calls[1].kwargs["audio_root"] == "/beats/house"
    assert calls[1].kwargs["wait_for_zero"] is True
    assert calls[1].kwargs["library_control"] is None
    assert [card.mount_count for card in system.cards] == [1, 0]
    assert system.events.index(("card_close", 0)) < system.events.index(
        ("card_create", 1)
    )
    assert all(card.closed for card in system.cards)
    assert all(screen.closed for screen in system.screens)
    reports = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert any(
        report.get("event") == "system_error"
        and report["source"] == "SD"
        and report["error"] == "card removed"
        for report in reports
    )


def test_internal_runtime_failure_stops_without_retry_loop(
    system: SimpleNamespace,
) -> None:
    system.outcomes = [OSError("SD failed"), OSError("internal failed")]
    system.program.run(0.1)
    assert system.play.call_count == 2
    assert len(system.cards) == 2
    system.screens[1].show_error.assert_called_once_with("RESTART REQUIRED")
    assert all(card.closed for card in system.cards)


def test_screen_startup_failure_releases_card_before_fallback(
    system: SimpleNamespace,
) -> None:
    system.screen_error = RuntimeError("display allocation failed")
    system.program.run(0.1)
    assert len(system.cards) == 2 and len(system.screens) == 1
    assert system.cards[0].closed
    assert system.events.index(("display_pin_claim", 0)) < system.events.index(
        ("release_displays", 0)
    )
    assert system.events.index(("release_displays", 0)) < system.events.index(
        ("card_close", 0)
    )
    assert system.display_pin_owner is None
    assert system.play.call_args.kwargs["source"] == "INTERNAL"


def test_interrupt_propagates_after_closing_display_and_card(
    system: SimpleNamespace,
) -> None:
    system.outcomes = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        system.program.run()
    assert system.events[-2:] == [("screen_close", 0), ("card_close", 0)]
    assert all(card.closed for card in system.cards)


def test_display_close_failure_still_closes_card(
    system: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    system.close_error = RuntimeError("screen close failed")
    system.program.run(0.1)
    assert system.cards[0].closed
    assert '"event": "cleanup_error"' in capsys.readouterr().out


def test_display_close_failure_before_pin_release_does_not_leak_card_bus(
    system: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    system.close_before_release_error = RuntimeError(
        "display failed before releasing GP12"
    )
    system.program.run(0.1)
    assert system.display_pin_owner is None
    assert system.cards[0].closed and system.cards[0].spi.closed
    assert system.events.index(("release_displays", 0)) < system.events.index(
        ("card_close", 0)
    )
    assert '"event": "cleanup_error"' in capsys.readouterr().out


def test_session_limit_stops_after_requested_switches(system: SimpleNamespace) -> None:
    system.outcomes = [select_last_bank, select_last_bank]
    system.program.run(0.1, session_limit=2)
    assert system.play.call_count == 2
    assert len(system.cards) == 1 and system.cards[0].closed


def test_library_without_house_starts_first_bank(system: SimpleNamespace) -> None:
    system.load.return_value = [system.banks[0], system.banks[2]]
    system.program.run(0.1)
    assert system.play.call_args.kwargs["audio_root"] == "/verified-cache/neon-rush"


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_does_not_touch_hardware(
    system: SimpleNamespace, duration: float
) -> None:
    with pytest.raises(ValueError, match="Duration"):
        system.program.run(duration)
    assert not system.cards
    system.release_displays.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "1"])
def test_invalid_session_limit_does_not_touch_hardware(
    system: SimpleNamespace, limit: object
) -> None:
    with pytest.raises(ValueError, match="Session limit"):
        system.program.run(session_limit=limit)
    assert not system.cards
    system.release_displays.assert_not_called()
