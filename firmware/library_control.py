import math


class LibraryControl:
    def __init__(
        self,
        bank_count: int,
        selected_index: int = 0,
        *,
        hold_seconds: float = 2.0,
        debounce_seconds: float = 0.15,
    ) -> None:
        if (
            not isinstance(bank_count, int)
            or isinstance(bank_count, bool)
            or bank_count < 1
        ):
            raise ValueError("Library needs at least one bank")
        if (
            not isinstance(selected_index, int)
            or isinstance(selected_index, bool)
            or not 0 <= selected_index < bank_count
        ):
            raise ValueError("Selected bank must be inside the library")
        if not 0 < hold_seconds < float("inf"):
            raise ValueError("Hold time must be finite and positive")
        if not 0 < debounce_seconds < float("inf"):
            raise ValueError("Debounce time must be finite and positive")
        self._bank_count = bank_count
        self._selected_index = selected_index
        self._hold_seconds = hold_seconds
        self._debounce_seconds = debounce_seconds
        self._hysteresis = min(2.0, 20.0 / bank_count)
        self._holding = False
        self._held_seconds = 0.0
        self._browsing = False
        self._entry_percent = 0.0
        self._knob_moved = False
        self._candidate = None
        self._candidate_seconds = 0.0

    def update(
        self, percent: float, active_mask: int, elapsed_seconds: float
    ) -> str | None:
        if not 0 <= percent <= 100:
            raise ValueError("Volume must be between 0 and 100 percent")
        if (
            not isinstance(active_mask, int)
            or isinstance(active_mask, bool)
            or not 0 <= active_mask < 512
        ):
            raise ValueError("Active stem mask must fit nine bits")
        if not 0 <= elapsed_seconds < float("inf"):
            raise ValueError("Elapsed time must be finite and nonnegative")
        if self._browsing:
            if active_mask:
                self._browsing = False
                self._candidate = None
                self._candidate_seconds = 0.0
                return "select"
            self._browse(percent, elapsed_seconds)
            return None
        if active_mask or percent > 1:
            self._holding = False
            self._held_seconds = 0.0
            return None
        if not self._holding:
            self._holding = True
            self._held_seconds = 0.0
            return None
        self._held_seconds += elapsed_seconds
        if self._held_seconds < self._hold_seconds:
            return None
        self._holding = False
        self._held_seconds = 0.0
        self._browsing = True
        self._entry_percent = percent
        self._knob_moved = False
        self._candidate = None
        self._candidate_seconds = 0.0
        return "open"

    def _browse(self, percent: float, elapsed_seconds: float) -> None:
        if not self._knob_moved:
            if abs(percent - self._entry_percent) < 3:
                return
            self._knob_moved = True
        candidate = min(self._bank_count - 1, int(percent * self._bank_count / 100))
        lower = self._selected_index * 100 / self._bank_count
        upper = (self._selected_index + 1) * 100 / self._bank_count
        if (
            candidate == self._selected_index
            or candidate < self._selected_index and percent > lower - self._hysteresis
            or candidate > self._selected_index and percent < upper + self._hysteresis
        ):
            self._candidate = None
            self._candidate_seconds = 0.0
            return
        if candidate != self._candidate:
            self._candidate = candidate
            self._candidate_seconds = 0.0
            return
        self._candidate_seconds += elapsed_seconds
        if self._candidate_seconds >= self._debounce_seconds:
            self._selected_index = candidate
            self._candidate = None
            self._candidate_seconds = 0.0

    @property
    def browsing(self) -> bool:
        return self._browsing

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def remaining(self) -> int | None:
        if not self._holding:
            return None
        return math.ceil(self._hold_seconds - self._held_seconds)
