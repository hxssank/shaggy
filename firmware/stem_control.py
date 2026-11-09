_INFINITY = float("inf")


class StemControl:
    def __init__(
        self,
        stem_level: float = 0.35,
        debounce_seconds: float = 0.02,
        fade_seconds: float = 0.02,
    ) -> None:
        if not 0 < stem_level <= 1:
            raise ValueError("Stem level must be greater than zero and at most one")
        if not 0 < debounce_seconds < _INFINITY:
            raise ValueError("Debounce duration must be finite and positive")
        if not 0 < fade_seconds < _INFINITY:
            raise ValueError("Fade duration must be finite and positive")
        self._stem_level = stem_level
        self._debounce = debounce_seconds
        self._fade_rate = stem_level / fade_seconds
        self._indices = range(9)
        self._candidates = [False] * 9
        self._held = [0.0] * 9
        self._levels = [0.0] * 9
        self._active_mask = 0
        self._active_count = 0
        self._changed_mask = 0

    def _move(self, level: float, target: float, elapsed: float) -> float:
        step = self._fade_rate * elapsed
        if level < target:
            return min(target, level + step)
        return max(target, level - step)

    def update(
        self, present: list[bool] | tuple[bool, ...], elapsed_seconds: float
    ) -> bool:
        if len(present) != 9:
            raise ValueError("Expected nine stem presence values")
        if not 0 <= elapsed_seconds < _INFINITY:
            raise ValueError("Elapsed time must be finite and nonnegative")
        self._changed_mask = 0
        for index in self._indices:
            bit = 1 << index
            active = bool(self._active_mask & bit)
            candidate = present[index]
            level = self._levels[index]
            fade_elapsed = elapsed_seconds
            if candidate != self._candidates[index]:
                self._candidates[index] = candidate
                self._held[index] = 0.0
            elif candidate != active:
                held = self._held[index] + elapsed_seconds
                if held >= self._debounce:
                    remaining = min(elapsed_seconds, self._debounce - self._held[index])
                    level = self._move(
                        level, self._stem_level if active else 0.0, remaining
                    )
                    active = candidate
                    if active:
                        self._active_mask |= bit
                        self._active_count += 1
                    else:
                        self._active_mask &= ~bit
                        self._active_count -= 1
                    self._held[index] = self._debounce
                    self._changed_mask |= bit
                    fade_elapsed -= remaining
                else:
                    self._held[index] = held
            level = self._move(level, self._stem_level if active else 0.0, fade_elapsed)
            if level != self._levels[index]:
                self._levels[index] = level
                self._changed_mask |= bit
        return self._changed_mask != 0

    @property
    def levels(self) -> list[float]:
        return self._levels

    @property
    def active_mask(self) -> int:
        return self._active_mask

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def changed_mask(self) -> int:
        return self._changed_mask
