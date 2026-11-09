import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from firmware import volume_control


class FakePalette(list):
    def __init__(self, color_count: int) -> None:
        super().__init__([0] * color_count)
        self.writes = []

    def __setitem__(self, index: int, value: int) -> None:
        self.writes.append((index, value))
        super().__setitem__(index, value)


class FakeGroup(list):
    def __init__(self, *, x: int = 0, y: int = 0, scale: int = 1) -> None:
        super().__init__()
        self.x = x
        self.y = y
        self.scale = scale
        self.hidden = False


class FakeBitmap:
    def __init__(self, width: int, height: int, value_count: int) -> None:
        self.width = width
        self.height = height
        self.value_count = value_count
        self.pixels = bytearray(width * height)
        self.writes = 0

    def __getitem__(self, index: int | tuple[int, int]) -> int:
        if isinstance(index, tuple):
            x, y = index
            assert 0 <= x < self.width and 0 <= y < self.height
            index = y * self.width + x
        return self.pixels[index]

    def __setitem__(self, index: int | tuple[int, int], value: int) -> None:
        if isinstance(index, tuple):
            x, y = index
            assert 0 <= x < self.width and 0 <= y < self.height
            index = y * self.width + x
        assert 0 <= value < self.value_count
        self.pixels[index] = value
        self.writes += 1

    def fill(self, value: int) -> None:
        assert 0 <= value < self.value_count
        self.pixels[:] = bytes([value]) * len(self.pixels)
        self.writes += 1


class FakeTileGrid(list):
    def __init__(
        self,
        bitmap: object,
        *,
        pixel_shader: FakePalette,
        width: int = 1,
        height: int = 1,
        tile_width: int | None = None,
        tile_height: int | None = None,
        default_tile: int = 0,
        x: int = 0,
        y: int = 0,
    ) -> None:
        super().__init__([default_tile] * width * height)
        self.bitmap = bitmap
        self.pixel_shader = pixel_shader
        self.width = width
        self.height = height
        self.tile_width = bitmap.width if tile_width is None else tile_width
        self.tile_height = bitmap.height if tile_height is None else tile_height
        self.x = x
        self.y = y
        self.hidden = False
        self.writes = []

    def __setitem__(self, index: int, value: int) -> None:
        self.writes.append((index, value))
        super().__setitem__(index, value)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x,
            self.y,
            self.x + self.width * self.tile_width,
            self.y + self.height * self.tile_height,
        )


class FakeFont:
    def __init__(self) -> None:
        self.bitmap = object()

    def get_glyph(self, codepoint: int) -> SimpleNamespace:
        return SimpleNamespace(
            bitmap=self.bitmap, tile_index=codepoint, width=6, height=12
        )


class FakeRectangle:
    def __init__(
        self, *, pixel_shader: FakePalette, width: int, height: int, x: int, y: int
    ) -> None:
        assert width > 0 and height > 0
        self.pixel_shader = pixel_shader
        self.width = width
        self.height = height
        self.x = x
        self.y = y
        self.hidden = False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.x + self.width, self.y + self.height


class FakePolygon:
    def __init__(
        self,
        *,
        pixel_shader: FakePalette,
        points: list[tuple[int, int]],
        x: int,
        y: int,
    ) -> None:
        self.pixel_shader = pixel_shader
        self.points = points
        self.x = x
        self.y = y
        self.hidden = False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x + min(point[0] for point in self.points),
            self.y + min(point[1] for point in self.points),
            self.x + max(point[0] for point in self.points) + 1,
            self.y + max(point[1] for point in self.points) + 1,
        )


class FakeLabel:
    def __init__(self, font: object, *, text: str, color: int, scale: int = 1) -> None:
        self.text = text
        self.color = color
        self.scale = scale
        self.anchor_point = (0.0, 0.0)
        self.anchored_position = (0, 0)
        self.hidden = False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        width, height = len(self.text) * 6 * self.scale, 12 * self.scale
        x = self.anchored_position[0] - self.anchor_point[0] * width
        y = self.anchored_position[1] - self.anchor_point[1] * height
        return x, y, x + width, y + height


def visible_bounds(
    item: object, x: float = 0, y: float = 0, scale: int = 1
) -> Iterator[tuple[float, float, float, float]]:
    if item.hidden:
        return
    if isinstance(item, FakeGroup):
        for child in item:
            yield from visible_bounds(
                child, x + item.x * scale, y + item.y * scale, scale * item.scale
            )
    else:
        left, top, right, bottom = item.bounds
        yield x + left * scale, y + top * scale, x + right * scale, y + bottom * scale


def test_close_releases_display_before_spi_pins(screen: object) -> None:
    events = []
    sys.modules["displayio"].release_displays.side_effect = lambda: events.append(
        "display"
    )
    screen.spi.deinit.side_effect = lambda: events.append("spi")
    screen.close()
    assert events == ["display", "spi"]


def test_borrowed_spi_survives_display_construction_and_close(screen: object) -> None:
    borrowed = Mock()
    release = sys.modules["displayio"].release_displays
    factory = sys.modules["busio"].SPI
    release.reset_mock()
    factory.reset_mock()

    shared = type(screen)(spi=borrowed)

    assert shared.spi is borrowed
    factory.assert_not_called()
    release.assert_not_called()
    shared.close()
    release.assert_called_once()
    borrowed.deinit.assert_not_called()
    shared.close()
    release.assert_called_once()


def test_owned_spi_is_released_once_when_closed_twice(screen: object) -> None:
    screen.spi.deinit.reset_mock()
    screen.close()
    screen.close()
    screen.spi.deinit.assert_called_once()


def test_background_refresh_rate_is_twelve_frames_per_second(screen: object) -> None:
    options = sys.modules["adafruit_st7789"].ST7789.call_args.kwargs
    assert options["native_frames_per_second"] == 12
    assert options["auto_refresh"] is False


def test_streaming_updates_state_without_foreground_spi_refresh(screen: object) -> None:
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(5000, 2)
    screen.display.refresh.reset_mock()
    screen.set_streaming(True)
    screen.set_state("PLAYING")
    screen.animate(1)
    screen.update(control, -20)
    assert screen.display.auto_refresh is True
    assert screen.percentage.text == "50"
    assert screen.state.text == "PLAYING"
    assert not screen.fill.hidden
    screen.display.refresh.assert_not_called()


def test_stopping_streaming_restores_manual_refresh(screen: object) -> None:
    screen.set_streaming(True)
    screen.display.refresh.reset_mock()
    screen.set_streaming(False)
    assert screen.display.auto_refresh is False
    screen.display.refresh.assert_not_called()
    screen.set_state("STOPPED")
    screen.display.refresh.assert_called_once_with(minimum_frames_per_second=0)


def test_stop_cannot_force_foreground_refresh_while_streaming(screen: object) -> None:
    screen.set_streaming(True)
    screen.display.refresh.reset_mock()
    screen.stop()
    assert screen.state.text == "STOPPED"
    screen.display.refresh.assert_not_called()


def test_close_disables_background_refresh_without_foreground_transfer(
    screen: object,
) -> None:
    screen.set_streaming(True)
    screen.display.refresh.reset_mock()
    screen.close()
    assert screen.display.auto_refresh is False
    screen.display.refresh.assert_not_called()


def border_points(screen: object, color: int | None = None) -> set[tuple[int, int]]:
    points = set()
    for (rx, ry, width, height), bitmap in zip(
        screen.border.regions, screen.border.bitmaps, strict=True
    ):
        for y in range(height):
            for x in range(width):
                value = bitmap[x, y]
                if value != 0 and (color is None or value == color):
                    points.add((rx + x, ry + y))
    return points


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> object:
    modules = {
        "adafruit_tlv320": {},
        "analogio": {"AnalogIn": Mock()},
        "audiobusio": {},
        "audiocore": {},
        "audiomixer": {},
        "board": {"GP%d" % index: index for index in range(29)},
        "busio": {"SPI": Mock()},
        "digitalio": {},
        "displayio": {
            "release_displays": Mock(),
            "Group": FakeGroup,
            "Palette": FakePalette,
            "Bitmap": FakeBitmap,
            "TileGrid": FakeTileGrid,
        },
        "fourwire": {"FourWire": Mock()},
        "terminalio": {"FONT": FakeFont()},
        "vectorio": {
            "Rectangle": FakeRectangle,
            "Polygon": FakePolygon,
        },
        "adafruit_display_text": {"label": SimpleNamespace(Label=FakeLabel)},
        "adafruit_st7789": {"ST7789": Mock()},
    }
    for name, attributes in modules.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**attributes))
    monkeypatch.setitem(sys.modules, "volume_control", volume_control)
    path = Path(__file__).parents[1] / "firmware" / "volume_test.py"
    spec = importlib.util.spec_from_file_location("volume_display_under_test", path)
    assert spec is not None and spec.loader is not None
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    return program.VolumeDisplay()


def test_fractional_zero_percent_has_no_active_meter(screen: object) -> None:
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(49, 2)
    assert control.position == pytest.approx(0.49)
    screen.update(control, -40.0)
    assert screen.percentage.text == "0"
    assert screen.level.text == "MUTED"
    assert screen.fill.hidden


@pytest.mark.parametrize(
    ("raw", "percent", "applied_db"),
    [(100, "1", -39.5), (5000, "50", -20.0), (10000, "100", 0.0)],
)
def test_active_volume_shows_dac_readback_and_meter(
    screen: object, raw: int, percent: str, applied_db: float
) -> None:
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(raw, 2)
    screen.update(control, applied_db)
    assert screen.percentage.text == percent
    assert screen.level.text == "%.1f dB" % applied_db
    assert not screen.fill.hidden
    assert screen.fill.width == round(control.position * screen.METER_WIDTH / 100)
    if raw == 10000:
        assert screen.fill.width == screen.METER_WIDTH


def test_turning_to_zero_clears_the_meter(screen: object) -> None:
    control = volume_control.VolumeControl()
    control.update(65535, 2)
    screen.update(control, -6.0)
    control.update(0, 2)
    screen.update(control, -40.0)
    assert screen.percentage.text == "0"
    assert screen.level.text == "MUTED"
    assert screen.fill.hidden


def test_bar_moves_with_fractional_position_when_displayed_percent_is_unchanged(
    screen: object,
) -> None:
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(110, 2)
    screen.update(control, -40.0)
    assert screen.percentage.text == "1"
    assert screen.fill.width == 2
    screen.display.refresh.reset_mock()

    control.update(140, 2)
    screen.update(control, -40.0)

    assert screen.percentage.text == "1"
    assert screen.fill.width == 3
    screen.display.refresh.assert_called_once()


def test_percentage_updates_only_the_changed_digit(screen: object) -> None:
    screen.percentage.text = "72"
    tiles = screen.percentage[0]
    original_x = screen.percentage.x
    tiles.writes.clear()

    screen.percentage.text = "73"

    assert tiles.writes == [(1, ord("3"))]
    assert list(tiles) == [ord("7"), ord("3"), ord(" ")]
    assert screen.percentage.x == original_x


def test_shorter_percentage_erases_trailing_digits(screen: object) -> None:
    screen.percentage.text = "100"
    tiles = screen.percentage[0]
    tiles.writes.clear()

    screen.percentage.text = "0"

    assert tiles.writes == [(0, ord("0")), (1, ord(" ")), (2, ord(" "))]
    assert list(tiles) == [ord("0"), ord(" "), ord(" ")]


@pytest.mark.parametrize("name", ["percentage", "level"])
def test_unchanged_fixed_text_does_not_write_tiles_or_palette(
    screen: object, name: str
) -> None:
    text = getattr(screen, name)
    tiles = text[0]
    tiles.writes.clear()
    tiles.pixel_shader.writes.clear()

    text.text = text.text
    text.color = text.color

    assert tiles.writes == []
    assert tiles.pixel_shader.writes == []


def test_level_color_change_updates_palette_without_replacing_glyphs(
    screen: object,
) -> None:
    tiles = screen.level[0]
    tiles.writes.clear()
    tiles.pixel_shader.writes.clear()

    screen.level.color = screen.WHITE

    assert tiles.writes == []
    assert tiles.pixel_shader.writes == [(1, screen.WHITE)]
    assert screen.level[0] is tiles


def test_unchanged_values_do_not_refresh_the_display(screen: object) -> None:
    control = volume_control.VolumeControl()
    control.update(32768, 2)
    screen.update(control, -23.0)
    screen.set_state("house / all 9 stems")
    screen.display.refresh.reset_mock()
    for _ in range(5):
        screen.update(control, -23.0)
        screen.set_state("HOUSE / ALL 9 STEMS")
    screen.display.refresh.assert_not_called()


def test_state_is_bounded_before_comparison_and_refresh(screen: object) -> None:
    screen.set_state("a" * 40)
    assert screen.state.text == "A" * 28
    left, _, right, _ = screen.state.bounds
    assert (left + right) / 2 == 120
    assert right - left == 168
    screen.display.refresh.reset_mock()
    screen.set_state("A" * 28 + "changed beyond visible text")
    screen.display.refresh.assert_not_called()


@pytest.mark.parametrize("value", ["MUTED", "-40.0 dB", "-6.0 dB", "0.0 dB"])
def test_visible_level_text_is_centered(screen: object, value: str) -> None:
    text = screen.level
    text.text = value
    tiles = text[0]
    left = text.x + tiles.x * text.scale
    width = len(value) * tiles.tile_width * text.scale
    assert left + width / 2 == 120


@pytest.mark.parametrize("raw", [0, 7200, 10000])
def test_number_and_percent_unit_form_a_centered_block(
    screen: object, raw: int
) -> None:
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    control.update(raw, 2)
    screen.update(control, round(control.db * 2) / 2)
    text = screen.percentage
    tiles = text[0]
    number_left = text.x + tiles.x * text.scale
    number_right = number_left + len(text.text) * tiles.tile_width * text.scale
    unit_left, _, unit_right, _ = screen.unit.bounds
    assert unit_left > number_right
    assert (number_left + unit_right) / 2 == 120


def test_static_headers_are_centered(screen: object) -> None:
    headers = [
        item
        for item in screen.group
        if isinstance(item, FakeLabel) and item.text in ("SHAGGY", "VOLUME")
    ]
    assert len(headers) == 2
    for text in headers:
        left, _, right, _ = text.bounds
        assert (left + right) / 2 == 120


def test_border_regions_fit_one_pixel_inset_without_overlap_or_painted_corners(
    screen: object,
) -> None:
    assert len(screen.border.regions) == len(screen.border.bitmaps) == 8
    occupied = set()
    for (x, y, width, height), bitmap in zip(
        screen.border.regions, screen.border.bitmaps, strict=True
    ):
        assert 1 <= x < x + width <= 239
        assert 1 <= y < y + height <= 279
        assert (bitmap.width, bitmap.height) == (width, height)
        region = {(px, py) for py in range(y, y + height) for px in range(x, x + width)}
        assert occupied.isdisjoint(region)
        occupied.update(region)
    points = border_points(screen)
    assert min(x for x, _ in points) == 1
    assert max(x for x, _ in points) == 238
    assert min(y for _, y in points) == 1
    assert max(y for _, y in points) == 278
    for point in ((0, 0), (239, 0), (0, 279), (239, 279), (1, 1), (238, 278)):
        assert point not in points


def test_volume_only_updates_do_not_rewrite_border_bitmaps(screen: object) -> None:
    pixels = [bytes(bitmap.pixels) for bitmap in screen.border.bitmaps]
    writes = [bitmap.writes for bitmap in screen.border.bitmaps]
    control = volume_control.VolumeControl(adc_max=10000, deadband_percent=0)
    for raw in (0, 7200, 10000, 0):
        control.update(raw, 2)
        screen.update(control, round(control.db * 2) / 2)
    assert [bytes(bitmap.pixels) for bitmap in screen.border.bitmaps] == pixels
    assert [bitmap.writes for bitmap in screen.border.bitmaps] == writes


@pytest.mark.parametrize(
    ("fraction", "right", "bottom"),
    [(0, False, False), (0.25, True, False), (0.5, True, True), (0.75, False, True)],
)
def test_accents_move_clockwise_and_stay_on_opposite_sides(
    screen: object, fraction: float, right: bool, bottom: bool
) -> None:
    border = screen.border
    assert border.length % 2 == 0
    border.animate(fraction * border.length / border.SPEED)
    cyan, magenta = border_points(screen, 2), border_points(screen, 3)
    assert cyan and magenta and cyan.isdisjoint(magenta)
    cyan_x = sum(x for x, _ in cyan) / len(cyan)
    cyan_y = sum(y for _, y in cyan) / len(cyan)
    magenta_x = sum(x for x, _ in magenta) / len(magenta)
    magenta_y = sum(y for _, y in magenta) / len(magenta)
    assert cyan_x > 200 if right else cyan_x < 40
    assert cyan_y > 220 if bottom else cyan_y < 60
    assert cyan_x + magenta_x == pytest.approx(239, abs=2)
    assert cyan_y + magenta_y == pytest.approx(279, abs=2)


def test_wrap_and_long_gaps_leave_no_stale_accent_pixels(screen: object) -> None:
    border = screen.border
    initial = [bytes(bitmap.pixels) for bitmap in border.bitmaps]
    outline = border_points(screen)
    steps = (1, 7, 50, border.length - 2, border.length + 3, 7 * border.length + 100)
    for step in steps:
        elapsed = (step + 0.25) / border.SPEED
        border.animate(elapsed)
        direct = type(screen)()
        direct.border.animate(elapsed)
        assert [bytes(bitmap.pixels) for bitmap in border.bitmaps] == [
            bytes(bitmap.pixels) for bitmap in direct.border.bitmaps
        ]
        assert border_points(screen) == outline
    border.animate((100 * border.length + 0.25) / border.SPEED)
    assert [bytes(bitmap.pixels) for bitmap in border.bitmaps] == initial


def test_unchanged_animation_position_does_not_write_or_refresh(screen: object) -> None:
    control = volume_control.VolumeControl()
    screen.update(control, -40.0)
    screen.display.refresh.reset_mock()
    writes = [bitmap.writes for bitmap in screen.border.bitmaps]

    screen.animate(0.25 / screen.border.SPEED)
    screen.update(control, -40.0)

    assert [bitmap.writes for bitmap in screen.border.bitmaps] == writes
    screen.display.refresh.assert_not_called()


def test_animation_and_volume_changes_share_one_refresh(screen: object) -> None:
    control = volume_control.VolumeControl()
    screen.update(control, -40.0)
    screen.display.refresh.reset_mock()

    control.update(32768, 2)
    screen.animate(1 / screen.border.SPEED)
    screen.display.refresh.assert_not_called()
    screen.update(control, -23.0)

    screen.display.refresh.assert_called_once()
    screen.display.refresh.reset_mock()
    screen.update(control, -23.0)
    screen.display.refresh.assert_not_called()


def test_stop_clears_volume_and_active_meter(screen: object) -> None:
    control = volume_control.VolumeControl()
    control.update(65535, 2)
    screen.update(control, -6.0)
    screen.set_state("house / all 9 stems")
    screen.stop()
    assert screen.percentage.text == "0"
    assert screen.level.text == "MUTED"
    assert screen.state.text == "STOPPED"
    assert screen.fill.hidden


@pytest.mark.parametrize("raw", [0, 655, 32768, 65535])
def test_visible_shapes_and_text_fit_the_display(screen: object, raw: int) -> None:
    control = volume_control.VolumeControl()
    control.update(raw, 2)
    screen.update(control, -40.0 if control.muted else -6.0)
    screen.set_state("A" * 40)
    for bounds in visible_bounds(screen.group):
        left, top, right, bottom = bounds
        assert 0 <= left < right <= 240, bounds
        assert 0 <= top < bottom <= 280, bounds
