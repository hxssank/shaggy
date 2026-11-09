import gc
import json

import displayio
from bank_cache import prepare_bank
from library_control import LibraryControl
from music_library import load_library
from player_display import PlayerDisplay
from sd_storage import SDStorage
from shaggy import run as play

VERSION = "2.1"
INTERNAL_BANK = {
    "id": "house",
    "title": "HOUSE / 124 BPM",
    "audio_root": "/beats/house",
}


def _close(screen: object | None, card: object | None) -> None:
    try:
        if screen is not None:
            screen.close()
    finally:
        try:
            displayio.release_displays()
        finally:
            if card is not None:
                card.close()


def run(
    duration_seconds: float | None = None, *, session_limit: int | None = None
) -> None:
    if duration_seconds is not None and not 0 < duration_seconds < float("inf"):
        raise ValueError("Duration must be finite and positive")
    if session_limit is not None and (
        not isinstance(session_limit, int)
        or isinstance(session_limit, bool)
        or session_limit < 1
    ):
        raise ValueError("Session limit must be a positive integer")
    displayio.release_displays()
    internal_only = False
    while True:
        card = None
        screen = None
        source = "INTERNAL"
        retry_internal = False
        try:
            card = SDStorage()
            banks = [dict(INTERNAL_BANK)]
            notice = None
            if not internal_only:
                if card.mount():
                    try:
                        banks = load_library()
                        source = "SD"
                    except (OSError, ValueError) as error:
                        notice = "LIBRARY ERROR"
                        print(
                            json.dumps({"event": "library_error", "error": str(error)})
                        )
                else:
                    notice = "SD UNAVAILABLE"
                    print(json.dumps({"event": "sd_unavailable", "error": card.error}))
            selected = 0
            for index, bank in enumerate(banks):
                if bank["id"] == "house":
                    selected = index
                    break
            control = LibraryControl(len(banks), selected) if len(banks) > 1 else None
            card.release_display_cs()
            screen = PlayerDisplay(spi=card.spi)
            print(
                json.dumps(
                    {
                        "event": "system_ready",
                        "version": VERSION,
                        "source": source,
                        "banks": len(banks),
                        "notice": notice,
                    }
                )
            )
            waiting = internal_only
            sessions = 0
            while session_limit is None or sessions < session_limit:
                bank = banks[selected]
                screen.set_bank(
                    bank["title"],
                    instruments=bank.get("instruments"),
                    index=selected,
                    total=len(banks),
                    source=source,
                )
                if notice:
                    screen.set_state(notice)
                audio_root = bank["audio_root"]
                playback_source = source
                if source == "SD":
                    screen.set_state("LOADING BEAT")
                    audio_root = prepare_bank(bank, progress=screen.set_state)
                    playback_source = "FLASH"
                switched = play(
                    duration_seconds,
                    audio_root=audio_root,
                    beat_title=bank["title"],
                    instruments=bank.get("instruments"),
                    screen=screen,
                    library_control=control,
                    banks=banks,
                    wait_for_zero=waiting,
                    source=playback_source,
                    raise_errors=True,
                )
                sessions += 1
                if not switched:
                    return
                selected = control.selected_index
                waiting = True
                gc.collect()
        except Exception as error:
            print(
                json.dumps(
                    {"event": "system_error", "source": source, "error": str(error)}
                )
            )
            if source == "SD":
                retry_internal = True
            elif screen is not None:
                screen.show_error("RESTART REQUIRED")
        finally:
            try:
                _close(screen, card)
            except Exception as error:
                print(json.dumps({"event": "cleanup_error", "error": str(error)}))
        if not retry_internal:
            return
        internal_only = True
        gc.collect()
