import math


class BeatControl:
    def __init__(self, hold_seconds: float = 6.0) -> None:
        if not 0 < hold_seconds < float("inf"):
            raise ValueError("Hold time must be finite and positive")
        self._hold_seconds = hold_seconds
        self._elapsed = 0.0
        self._holding = False
        self._armed = True

    def update(self, percent: int, elapsed_seconds: float) -> bool:
        if not 0 <= percent <= 100:
            raise ValueError("Volume must be between 0 and 100 percent")
        if not 0 <= elapsed_seconds < float("inf"):
            raise ValueError("Elapsed time must be finite and nonnegative")
        if percent < 100:
            self._holding = False
            self._elapsed = 0.0
            if percent < 95:
                self._armed = True
            return False
        if not self._armed:
            return False
        if not self._holding:
            self._holding = True
            self._elapsed = 0.0
            return False
        self._elapsed += elapsed_seconds
        if self._elapsed >= self._hold_seconds:
            self._armed = False
            self._holding = False
            self._elapsed = 0.0
            return True
        return False

    @property
    def remaining(self) -> int | None:
        if not self._holding:
            return None
        return math.ceil(self._hold_seconds - self._elapsed)
