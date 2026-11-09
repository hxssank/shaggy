class VolumeControl:
    def __init__(
        self,
        initial_raw: int = 0,
        adc_min: int = 0,
        adc_max: int = 65535,
        deadband_percent: float = 0.75,
        ramp_percent_per_second: float = 100.0,
    ) -> None:
        if not 0 <= adc_min < adc_max <= 65535:
            raise ValueError("ADC endpoints must satisfy 0 <= min < max <= 65535")
        if not 0 <= deadband_percent < 100:
            raise ValueError("Deadband must be at least 0 and less than 100 percent")
        if not 0 < ramp_percent_per_second < float("inf"):
            raise ValueError("Ramp rate must be finite and greater than 0")
        self._adc_min = adc_min
        self._adc_max = adc_max
        self._deadband = deadband_percent
        self._ramp_rate = ramp_percent_per_second
        self._target = self._percent_from_adc(initial_raw)
        self._position = 0.0

    def _percent_from_adc(self, raw: int) -> float:
        raw = max(self._adc_min, min(self._adc_max, raw))
        return (raw - self._adc_min) * 100.0 / (self._adc_max - self._adc_min)

    def update(self, raw: int, elapsed_seconds: float) -> None:
        if not 0 <= elapsed_seconds < float("inf"):
            raise ValueError("Elapsed time must be finite and nonnegative")
        candidate = self._percent_from_adc(raw)
        if (
            candidate < 0.5
            or candidate >= 99.5
            or abs(candidate - self._target) >= self._deadband
        ):
            self._target = candidate
        step = self._ramp_rate * elapsed_seconds
        if self._position < self._target:
            self._position = min(self._target, self._position + step)
        else:
            self._position = max(self._target, self._position - step)

    @property
    def position(self) -> float:
        return self._position

    @property
    def percent(self) -> int:
        return int(self._position + 0.5)

    @property
    def db(self) -> float:
        return -40.0 + max(0, self.percent - 1) * 40.0 / 99.0

    @property
    def muted(self) -> bool:
        return self.percent == 0
