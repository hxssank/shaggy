import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_volume_display import FakeLabel, visible_bounds
from test_volume_display import screen as volume_screen

from firmware.volume_control import VolumeControl

SANDSTORM_INSTRUMENTS = (
    ("DR LOW", "DRUM LOW"),
    ("DRBODY", "DRUM BODY"),
    ("DRSNAP", "DRUM SNAP"),
    ("DR AIR", "DRUM AIR"),
    ("B LOW", "BASS LOW"),
    ("B EDGE", "BASS EDGE"),
    ("SYNTH", "SYNTH"),
    ("FX", "VOICE OR FX"),
    ("DETAIL", "MIX DETAIL"),
)


@pytest.fixture
def display_class(monkeypatch: pytest.MonkeyPatch) -> type:
    base = volume_screen.__wrapped__(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "volume_test",
        SimpleNamespace(VolumeDisplay=type(base), FixedText=type(base.percentage)),
    )
    path = Path(__file__).parents[1] / "firmware" / "player_display.py"
    spec = importlib.util.spec_from_file_location("player_display_under_test", path)
    assert spec is not None and spec.loader is not None
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    return program.PlayerDisplay


@pytest.fixture
def screen(display_class: type, request: pytest.FixtureRequest) -> object:
    return display_class(getattr(request, "param", "HOUSE / 124 BPM"))


def test_custom_labels_are_centered_and_name_each_active_layer(
    display_class: type,
) -> None:
    custom = display_class("SANDSTORM / 136 BPM", instruments=SANDSTORM_INSTRUMENTS)
    assert custom.INSTRUMENTS == tuple(pair[0] for pair in SANDSTORM_INSTRUMENTS)
    assert custom.INSTRUMENT_NAMES == tuple(pair[1] for pair in SANDSTORM_INSTRUMENTS)
    for index, (instrument, names) in enumerate(
        zip(custom.instrument_labels, SANDSTORM_INSTRUMENTS)
    ):
        assert instrument.text == names[0]
        assert instrument.x + len(instrument.text) * 3 == 44 + index % 3 * 76
        custom.show_stems(1 << index)
        assert custom.state.text == names[1]
        assert custom.state.x + len(names[1]) * 3 == 120
        assert instrument.color == custom.CYAN
    for left, top, right, bottom in visible_bounds(custom.group):
        assert 0 <= left < right <= 240
        assert 0 <= top < bottom <= 280
    default = display_class(instruments=None)
    assert default.INSTRUMENTS[0] == "KICK"
    assert default.INSTRUMENT_NAMES[8] == "PAD"


@pytest.mark.parametrize(
    "instruments",
    [
        (),
        SANDSTORM_INSTRUMENTS[:8],
        SANDSTORM_INSTRUMENTS + (("EXTRA", "EXTRA"),),
        (("ONLY ONE",),) * 9,
        (("TOO LONG", "VALID"),) * 9,
        (("VALID", "MORE THAN SIXTEEN!"),) * 9,
        (("", "VALID"),) * 9,
        (("VALID", ""),) * 9,
        (("SYNTH", "SYNTH\n"),) * 9,
        (("BASS", "B\u00c4SS"),) * 9,
        (("SYNTH", 12),) * 9,
    ],
)
def test_invalid_custom_labels_are_rejected(
    display_class: type, instruments: tuple
) -> None:
    with pytest.raises(ValueError):
        display_class(instruments=instruments)


def test_startup_has_beat_title_and_inactive_indicators(screen: object) -> None:
    assert screen.state.text == "STARTING"
    assert [item.text for item in screen.instrument_labels] == list(screen.INSTRUMENTS)
    assert all(item.color == screen.DIM for item in screen.instrument_labels)
    titles = [
        item
        for item in screen.group
        if isinstance(item, FakeLabel) and item.text == "HOUSE / 124 BPM"
    ]
    assert len(titles) == 1
    left, _, right, _ = titles[0].bounds
    assert (left + right) / 2 == 120


@pytest.mark.parametrize(
    "screen", ["NEON RUSH / 138 BPM", "BLOCK PARTY / 140 BPM"], indirect=True
)
def test_new_beat_titles_fit_the_screen(
    screen: object, request: pytest.FixtureRequest
) -> None:
    expected = request.node.callspec.params["screen"]
    titles = [
        item
        for item in screen.group
        if isinstance(item, FakeLabel) and item.text == expected
    ]
    assert len(titles) == 1
    left, top, right, bottom = titles[0].bounds
    assert 0 <= left < right <= 240
    assert 0 <= top < bottom <= 280
    assert (left + right) / 2 == 120


@pytest.mark.parametrize(
    ("mask", "footer"),
    [
        (0, "ADD INSTRUMENT"),
        (1, "KICK"),
        (4, "CLOSED HAT"),
        (64, "PERCUSSION"),
        (257, "2 INSTRUMENTS"),
        (511, "9 INSTRUMENTS"),
    ],
)
def test_stem_changes_wait_for_the_main_display_update(
    screen: object, mask: int, footer: str
) -> None:
    screen.display.refresh.reset_mock()
    screen.show_stems(mask)
    assert screen.state.text == footer
    assert screen._animation_dirty
    screen.display.refresh.assert_not_called()
    for index, instrument in enumerate(screen.instrument_labels):
        expected = screen.CYAN if mask & (1 << index) else screen.DIM
        assert instrument.color == expected
    screen.update(VolumeControl(), -40.0)
    screen.display.refresh.assert_called_once()
    assert not screen._animation_dirty


def test_unchanged_sensors_do_not_write_text_or_request_a_refresh(
    screen: object,
) -> None:
    screen.show_stems(257)
    screen.update(VolumeControl(), -40.0)
    tiles = screen.state[0]
    tiles.writes.clear()
    tiles.pixel_shader.writes.clear()
    screen.display.refresh.reset_mock()
    for instrument in screen.instrument_labels:
        instrument[0].pixel_shader.writes.clear()
        instrument[0].writes.clear()

    screen.show_stems(257)

    assert not screen._animation_dirty
    assert tiles.writes == []
    assert tiles.pixel_shader.writes == []
    for instrument in screen.instrument_labels:
        assert instrument[0].pixel_shader.writes == []
        assert instrument[0].writes == []
    screen.display.refresh.assert_not_called()


def test_lifting_all_tiles_clears_the_active_indicators(screen: object) -> None:
    screen.show_stems(511)
    screen.show_stems(0)
    assert screen.state.text == "ADD INSTRUMENT"
    assert all(item.color == screen.DIM for item in screen.instrument_labels)


def test_countdown_survives_tile_changes_and_restores_instruments(
    screen: object,
) -> None:
    screen.show_stems(1)
    screen.show_switch(6)
    screen.show_stems(3)
    screen.show_switch(6)
    assert screen.state.text == "NEXT BEAT IN 6"
    assert screen.state.color == screen.MAGENTA
    screen.show_switch(None)
    assert screen.state.text == "2 INSTRUMENTS"
    assert screen.state.color == screen.DIM


def test_error_clears_playback_indicators_and_bounds_the_footer(screen: object) -> None:
    control = VolumeControl()
    control.update(65535, 2)
    screen.show_stems(511)
    screen.update(control, -6.0)

    screen.show_error("Missing audio file for stem 1")

    assert screen.percentage.text == "0"
    assert screen.level.text == "MUTED"
    assert screen.fill.hidden
    assert screen.state.text == "MISSING AUDIO FI"
    assert screen.state.color == screen.MAGENTA
    assert all(item.color == screen.DIM for item in screen.instrument_labels)
    for left, top, right, bottom in visible_bounds(screen.group):
        assert 0 <= left < right <= 240
        assert 0 <= top < bottom <= 280


@pytest.mark.parametrize("mask", [-1, 512])
def test_invalid_sensor_mask_leaves_the_view_unchanged(
    screen: object, mask: int
) -> None:
    with pytest.raises(ValueError, match="nine bits"):
        screen.show_stems(mask)
    assert screen.state.text == "STARTING"


@pytest.mark.parametrize("percent", [0, 72, 100])
def test_compact_layout_fits_in_every_volume_state(
    screen: object, percent: int
) -> None:
    control = VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(percent * 100, 2)
    screen.show_stems(511)
    screen.update(control, round(control.db * 2) / 2)
    assert screen.unit.anchored_position[1] == screen.UNIT_Y
    for left, top, right, bottom in visible_bounds(screen.group):
        assert 0 <= left < right <= 240
        assert 0 <= top < bottom <= 280
    for index, instrument in enumerate(screen.instrument_labels):
        width = len(instrument.text) * 6
        assert instrument.x + width / 2 == 44 + index % 3 * 76
        assert instrument.y + 6 == 197 + index // 3 * 21


def test_player_borrows_spi_without_releasing_it(display_class: type) -> None:
    shared_spi = Mock()
    release = sys.modules["displayio"].release_displays
    release.reset_mock()
    player = display_class(spi=shared_spi)
    release.assert_not_called()
    assert player.spi is shared_spi
    player.close()
    release.assert_called_once()
    shared_spi.deinit.assert_not_called()


def test_set_bank_restores_player_and_clears_old_bank_state(screen: object) -> None:
    banks = [{"title": "HOUSE"}, {"title": "SANDSTORM"}]
    screen.show_library(banks, 1)
    screen.show_stems(511)
    screen.show_switch(3)
    screen.show_takeover(True)

    screen.set_bank(
        "SANDSTORM / 136 BPM",
        SANDSTORM_INSTRUMENTS,
        index=1,
        total=2,
        source="SD",
    )

    assert screen.display.root_group is screen.group
    assert screen.beat_label.text == "SANDSTORM / 136 BPM"
    assert screen.source_badge.text == "SD 2/2"
    assert screen.state.text == "STARTING"
    assert screen.percentage.text == "0"
    assert screen.level.text == "MUTED"
    assert screen.fill.hidden
    assert all(item.color == screen.DIM for item in screen.instrument_labels)
    assert [item.text for item in screen.instrument_labels] == [
        pair[0] for pair in SANDSTORM_INSTRUMENTS
    ]
    screen.show_stems(1)
    assert screen.state.text == "DRUM LOW"

    screen.set_bank("HOUSE / 124 BPM")
    assert screen.source_badge.text == "INTERNAL 1/1"
    assert screen.INSTRUMENTS[0] == "KICK"
    screen.show_stems(1)
    assert screen.state.text == "KICK"


@pytest.mark.parametrize(
    "count,selected,start", [(4, 3, 0), (5, 4, 0), (8, 3, 1), (8, 7, 3)]
)
def test_library_shows_at_most_five_rows_and_scrolls_to_selection(
    screen: object, count: int, selected: int, start: int
) -> None:
    banks = [{"title": "BEAT %d" % index} for index in range(count)]
    screen.show_library(banks, selected)
    assert screen.display.root_group is screen._library_group
    assert screen._library_start == start
    visible = [row for row in screen._library_rows if not row.hidden]
    assert len(screen._library_rows) == 5
    assert len(visible) == min(5, count)
    assert visible[0].text == "%02d BEAT %d" % (start + 1, start)
    highlighted = [row for row in visible if row.color == screen.CYAN]
    assert len(highlighted) == 1
    assert highlighted[0].text == "%02d BEAT %d" % (selected + 1, selected)
    assert screen._library_accent.y == screen._library_highlight.y
    assert screen._library_highlight.y == 70 + (selected - start) * 29
    for bounds in visible_bounds(screen._library_group):
        left, top, right, bottom = bounds
        assert 0 <= left < right <= 240, bounds
        assert 0 <= top < bottom <= 280, bounds


def test_library_keeps_the_same_rows_and_skips_unchanged_refreshes(
    screen: object,
) -> None:
    banks = [{"title": "BEAT %d" % index} for index in range(9)]
    screen.show_library(banks, 0)
    group = screen._library_group
    rows = tuple(screen._library_rows)
    screen.display.refresh.reset_mock()
    for _ in range(5):
        screen.show_library(banks, 0)
    screen.display.refresh.assert_not_called()
    screen.show_library(banks, 8)
    screen.display.refresh.assert_called_once()
    assert screen._library_group is group
    assert all(old is new for old, new in zip(rows, screen._library_rows))


def test_library_reopens_after_player_view_with_the_same_selection(
    screen: object,
) -> None:
    banks = [{"title": "HOUSE"}, {"title": "NEON RUSH"}]
    screen.show_library(banks, 1)
    group = screen._library_group
    screen.set_bank("NEON RUSH", index=1, total=2, source="SD")
    screen.display.refresh.reset_mock()
    screen.show_library(banks, 1)
    assert screen.display.root_group is group
    screen.display.refresh.assert_called_once()


def test_long_titles_and_large_bank_numbers_fit_both_views(screen: object) -> None:
    banks = [{"title": "VERY LONG TITLE " * 8} for _ in range(100)]
    screen.show_library(banks, 99, source="INTERNAL")
    assert screen._library_rows[-1].text.startswith("100 ")
    assert all(len(row.text) <= 31 for row in screen._library_rows)
    for bounds in visible_bounds(screen._library_group):
        left, top, right, bottom = bounds
        assert 0 <= left < right <= 240, bounds
        assert 0 <= top < bottom <= 280, bounds
    screen.set_bank(banks[99]["title"], index=99, total=100, source="SD")
    assert len(screen.beat_label.text) == 32
    for bounds in visible_bounds(screen.group):
        left, top, right, bottom = bounds
        assert 0 <= left < right <= 240, bounds
        assert 0 <= top < bottom <= 280, bounds


def test_library_countdown_and_takeover_preserve_footer_priority(
    screen: object,
) -> None:
    screen.show_stems(0)
    screen.show_library_hold(None)
    assert screen.state.text == "MUTE FOR LIBRARY"
    screen.show_library_hold(2)
    assert screen.state.text == "LIBRARY IN 2"
    screen.show_takeover(True)
    screen.show_library_hold(1)
    screen.show_stems(3)
    assert screen.state.text == "TURN TO ZERO"
    screen.show_takeover(False)
    assert screen.state.text == "LIBRARY IN 1"
    screen.show_library_hold(None)
    assert screen.state.text == "2 INSTRUMENTS"
    screen.show_stems(0)
    assert screen.state.text == "MUTE FOR LIBRARY"


def test_single_bank_sd_does_not_offer_unavailable_library(screen: object) -> None:
    screen.set_bank("HOUSE / 124 BPM", total=1, source="SD")
    screen.show_stems(0)
    assert screen.source_badge.text == "SD 1/1"
    assert screen.state.text == "ADD INSTRUMENT"
    screen.set_bank("HOUSE / 124 BPM", total=2, source="SD")
    screen.show_stems(0)
    assert screen.state.text == "MUTE FOR LIBRARY"


def test_unchanged_library_footer_does_not_schedule_another_refresh(
    screen: object,
) -> None:
    screen.show_library_hold(2)
    screen.update(VolumeControl(), -40)
    screen.display.refresh.reset_mock()
    screen.show_library_hold(2)
    screen.show_takeover(False)
    screen.update(VolumeControl(), -40)
    screen.display.refresh.assert_not_called()


def test_error_restores_player_group_when_library_was_visible(screen: object) -> None:
    screen.show_library([{"title": "HOUSE"}], 0)
    screen.show_error("SD READ ERROR")
    assert screen.display.root_group is screen.group
    assert screen.state.text == "SD READ ERROR"
    assert screen.state.color == screen.MAGENTA


@pytest.mark.parametrize(
    "banks,index",
    [([], 0), ([{"title": "A"}], -1), ([{"title": "A"}], 1), ([{}], 0)],
)
def test_invalid_library_keeps_the_current_view(
    screen: object, banks: list, index: int
) -> None:
    current = screen.display.root_group
    with pytest.raises(ValueError):
        screen.show_library(banks, index)
    assert screen.display.root_group is current


@pytest.mark.parametrize(
    "method,args",
    [
        ("set_bank", ("NEON RUSH / 138 BPM",)),
        ("set_state", ("PLAYING",)),
        ("show_library", ([{"title": "HOUSE"}, {"title": "NEON RUSH"}], 1)),
        ("show_error", ("SD READ ERROR",)),
        ("stop", ()),
        ("update", (VolumeControl(), -20)),
    ],
)
def test_player_methods_do_not_refresh_foreground_during_streaming(
    screen: object, method: str, args: tuple
) -> None:
    screen.set_streaming(True)
    screen.display.refresh.reset_mock()
    getattr(screen, method)(*args)
    assert screen.display.auto_refresh is True
    screen.display.refresh.assert_not_called()


def test_library_refresh_is_manual_after_streaming_stops(screen: object) -> None:
    screen.set_streaming(True)
    screen.set_streaming(False)
    screen.display.refresh.reset_mock()
    screen.show_library([{"title": "HOUSE"}, {"title": "NEON RUSH"}], 1)
    screen.display.refresh.assert_called_once_with(minimum_frames_per_second=0)
    assert screen.display.auto_refresh is False
