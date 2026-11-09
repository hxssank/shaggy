import displayio
from volume_test import FixedText, VolumeDisplay


class PlayerDisplay(VolumeDisplay):
    UNIT_Y = 118
    INSTRUMENTS = (
        "KICK",
        "CLAP",
        "C.HAT",
        "O.HAT",
        "BASS",
        "CHORDS",
        "PERC",
        "ARP",
        "PAD",
    )
    INSTRUMENT_NAMES = (
        "KICK",
        "CLAP",
        "CLOSED HAT",
        "OPEN HAT",
        "BASS",
        "CHORDS",
        "PERCUSSION",
        "ARP",
        "PAD",
    )

    def __init__(
        self,
        beat_title: str = "HOUSE / 124 BPM",
        instruments: tuple[tuple[str, str], ...] | None = None,
        spi: object | None = None,
    ) -> None:
        self.INSTRUMENTS, self.INSTRUMENT_NAMES = self._instrument_names(instruments)
        beat_title = self._bounded_text(beat_title, 32)
        super().__init__(spi=spi)
        for item in self.group:
            if getattr(item, "text", None) == "VOLUME":
                self.beat_label = item
                item.text = beat_title
                item.anchored_position = (120, 60)
            if (
                getattr(item, "width", None) == self.METER_WIDTH
                and getattr(item, "y", None) == 180
                and item.pixel_shader[0] == self.TRACK
            ):
                item.y = 155
        self.source_badge = FixedText(
            "INTERNAL 1/1", 0, 11, 20, 1, self.DIM, self.BACKGROUND, center_x=120
        )
        self.group.append(self.source_badge)
        self.percentage.y = 106 - 36
        self.fill.y = 155
        self.level.y = 174 - 6
        self.group.remove(self.state)
        self.state = FixedText(
            "", 0, 263, 16, 1, self.DIM, self.BACKGROUND, center_x=120
        )
        self.group.append(self.state)
        self.instrument_labels = []
        for index, name in enumerate(self.INSTRUMENTS):
            item = FixedText(
                name,
                0,
                197 + index // 3 * 21,
                6,
                1,
                self.DIM,
                self.BACKGROUND,
                center_x=44 + index % 3 * 76,
            )
            self.group.append(item)
            self.instrument_labels.append(item)
        self._active_mask = None
        self._switch_remaining = None
        self._library_enabled = False
        self._library_remaining = None
        self._takeover_waiting = False
        self._library_group = None
        self._library_banks = None
        self._library_selected = None
        self._library_source = None
        self._library_start = None
        self._library_visible = False
        self.set_state("STARTING")

    @classmethod
    def _instrument_names(cls, instruments: tuple | None) -> tuple[tuple, tuple]:
        if instruments is None:
            return cls.INSTRUMENTS, cls.INSTRUMENT_NAMES
        if len(instruments) != 9:
            raise ValueError("Expected nine instrument labels")
        for pair in instruments:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError("Each instrument needs a short and full label")
            for text, limit in zip(pair, (6, 16)):
                if (
                    not isinstance(text, str)
                    or not 1 <= len(text) <= limit
                    or any(ord(char) < 32 or ord(char) > 126 for char in text)
                ):
                    raise ValueError(
                        "Instrument labels need 1-%d printable ASCII characters" % limit
                    )
        return tuple(pair[0] for pair in instruments), tuple(
            pair[1] for pair in instruments
        )

    @staticmethod
    def _bounded_text(text: str, length: int) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Display text must be a nonempty string")
        return "".join(
            char if 32 <= ord(char) <= 126 else "?" for char in text.upper()[:length]
        )

    def set_bank(
        self,
        beat_title: str,
        instruments: tuple[tuple[str, str], ...] | None = None,
        index: int = 0,
        total: int = 1,
        source: str = "INTERNAL",
    ) -> None:
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 1
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < total
        ):
            raise ValueError("Bank position must be inside the library")
        short, full = self._instrument_names(instruments)
        title = self._bounded_text(beat_title, 32)
        source = self._bounded_text(source, 8)
        self.INSTRUMENTS, self.INSTRUMENT_NAMES = short, full
        self.beat_label.text = title
        self.source_badge.text = ("%s %d/%d" % (source, index + 1, total))[:20]
        self.source_badge.color = self.CYAN if source == "SD" else self.DIM
        for item, name in zip(self.instrument_labels, short):
            item.text = name
            item.color = self.DIM
        self._active_mask = None
        self._switch_remaining = None
        self._library_remaining = None
        self._takeover_waiting = False
        self._library_enabled = total > 1
        self._library_visible = False
        self.percentage.text = "0"
        self.unit.anchored_position = (136, self.UNIT_Y)
        self.level.text = "MUTED"
        self.level.color = self.MAGENTA
        self.fill.hidden = True
        self._last_pixels = 0
        self._last_percent = 0
        self._last_db = None
        self._last_muted = True
        self.state.text = "STARTING"
        self.state.color = self.DIM
        self.display.root_group = self.group
        self._refresh()
        self._animation_dirty = False

    def set_state(self, text: str) -> None:
        text = text.upper()[:16]
        if self.state.text == text:
            return
        self.state.text = text
        self._refresh()
        self._animation_dirty = False

    def _footer(self) -> None:
        mask = 0 if self._active_mask is None else self._active_mask
        color = self.DIM
        if self._takeover_waiting:
            text, color = "TURN TO ZERO", self.MAGENTA
        elif self._library_remaining is not None:
            text = "LIBRARY IN %d" % self._library_remaining
            color = self.MAGENTA
        elif self._switch_remaining is not None:
            text = "NEXT BEAT IN %d" % self._switch_remaining
            color = self.MAGENTA
        elif mask == 0:
            text = "MUTE FOR LIBRARY" if self._library_enabled else "ADD INSTRUMENT"
        else:
            count = 0
            active_index = 0
            for index in range(9):
                if mask & (1 << index):
                    count += 1
                    active_index = index
            text = (
                self.INSTRUMENT_NAMES[active_index]
                if count == 1
                else "%d INSTRUMENTS" % count
            )
        text = text[:16]
        if self.state.text != text or self.state.color != color:
            self.state.text = text
            self.state.color = color
            self._animation_dirty = True

    def show_stems(self, active_mask: int) -> None:
        if not 0 <= active_mask < 512:
            raise ValueError("Active stem mask must fit nine bits")
        if active_mask == self._active_mask:
            return
        previous = 0 if self._active_mask is None else self._active_mask
        changed = previous ^ active_mask
        for index, instrument in enumerate(self.instrument_labels):
            if changed & (1 << index):
                instrument.color = self.CYAN if active_mask & (1 << index) else self.DIM
        self._active_mask = active_mask
        self._footer()
        self._animation_dirty = True

    def stop(self) -> None:
        self._library_visible = False
        self.display.root_group = self.group
        for instrument in self.instrument_labels:
            instrument.color = self.DIM
        self._active_mask = None
        super().stop()
        self._animation_dirty = False

    def show_switch(self, remaining: int | None) -> None:
        if remaining == self._switch_remaining:
            return
        self._switch_remaining = remaining
        self._footer()

    def show_library_hold(self, remaining: int | None) -> None:
        if self._library_enabled and remaining == self._library_remaining:
            return
        self._library_enabled = True
        self._library_remaining = remaining
        self._footer()

    def show_takeover(self, waiting: bool) -> None:
        if waiting == self._takeover_waiting:
            return
        self._takeover_waiting = waiting
        self._footer()

    def _make_library(self) -> None:
        player_group = self.group
        self._library_group = displayio.Group()
        self.group = self._library_group
        try:
            self._rect(0, 0, 240, 280, self.BACKGROUND)
            self._rect(1, 1, 238, 2, self.BORDER)
            self._rect(1, 277, 238, 2, self.BORDER)
            self._rect(1, 3, 2, 274, self.BORDER)
            self._rect(237, 3, 2, 274, self.BORDER)
            self._rect(20, 1, 50, 2, self.CYAN)
            self._rect(170, 277, 50, 2, self.MAGENTA)
            self._text("LIBRARY", 120, 29, scale=2, color=self.WHITE, anchor=0.5)
            self._library_count = FixedText(
                "SD / 1 BEATS", 0, 51, 24, 1, self.DIM, self.BACKGROUND, center_x=120
            )
            self.group.append(self._library_count)
            self._rect(16, 65, 208, 2, self.BORDER)
            self._library_highlight = self._rect(14, 70, 212, 28, self.TRACK)
            self._library_accent = self._rect(14, 70, 3, 28, self.CYAN)
            self._library_rows = []
            for row in range(5):
                item = FixedText(
                    "", 25, 84 + row * 29, 31, 1, self.DIM, self.BACKGROUND
                )
                self._library_rows.append(item)
                self.group.append(item)
            self._text("TURN: CHOOSE", 120, 236, color=self.CYAN, anchor=0.5)
            self._text("TILE: LOAD", 120, 256, color=self.MAGENTA, anchor=0.5)
        finally:
            self.group = player_group

    def show_library(
        self, banks: list | tuple, selected_index: int, source: str = "SD"
    ) -> None:
        if (
            not isinstance(banks, (list, tuple))
            or not banks
            or not isinstance(selected_index, int)
            or isinstance(selected_index, bool)
            or not 0 <= selected_index < len(banks)
        ):
            raise ValueError("Selected bank must be inside a nonempty library")
        if (
            self._library_visible
            and banks is self._library_banks
            and selected_index == self._library_selected
            and source == self._library_source
        ):
            return
        source = self._bounded_text(source, 8)
        if banks is not self._library_banks:
            for bank in banks:
                if not isinstance(bank, dict):
                    raise ValueError("Library bank must have a title")
                self._bounded_text(bank.get("title"), 31)
        if self._library_group is None:
            self._make_library()
        start = max(0, min(selected_index - 2, len(banks) - 5))
        for row, item in enumerate(self._library_rows):
            bank_index = start + row
            item.hidden = bank_index >= len(banks)
            if not item.hidden:
                title = self._bounded_text(banks[bank_index]["title"], 31)
                item.text = ("%02d %s" % (bank_index + 1, title))[:31]
                item.color = self.CYAN if bank_index == selected_index else self.DIM
                background = (
                    self.TRACK if bank_index == selected_index else self.BACKGROUND
                )
                if item[0].pixel_shader[0] != background:
                    item[0].pixel_shader[0] = background
        self._library_count.text = ("%s / %d BEATS" % (source, len(banks)))[:24]
        self._library_highlight.y = 70 + (selected_index - start) * 29
        self._library_accent.y = self._library_highlight.y
        self._library_banks = banks
        self._library_selected = selected_index
        self._library_source = source
        self._library_start = start
        self._library_visible = True
        self.display.root_group = self._library_group
        self._refresh()
        self._animation_dirty = False

    def show_error(self, message: str) -> None:
        self.state.color = self.MAGENTA
        self.stop()
        self.set_state(message or "AUDIO ERROR")
