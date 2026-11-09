import gc
import json
import math
import time
from array import array

import adafruit_tlv320
import analogio
import audiobusio
import audiocore
import audiomixer
import board
import busio
import digitalio
import displayio
import fourwire
import terminalio
import vectorio
from adafruit_display_text import label
from adafruit_st7789 import ST7789
from volume_control import VolumeControl

ADC_MIN = 0
ADC_MAX = 65535
STEM_LEVEL = 0.35
SPEAKER_GAIN_DB = 12
TEST_SECONDS = 180
DISPLAY_FPS = 30
MIN_CONTROL_WINDOW = 0.008
CONTROL_INTERVAL = 0.002
STEM_NAMES = (
    "kick",
    "clap",
    "closedhat",
    "openhat",
    "bass",
    "chords",
    "perc",
    "arp",
    "pad",
)


class FixedText(displayio.Group):
    def __init__(
        self,
        text: str,
        x: int,
        y: int,
        length: int,
        scale: int,
        color: int,
        background: int,
        center_x: int | None = None,
    ) -> None:
        glyph = terminalio.FONT.get_glyph(32)
        super().__init__(x=x, y=y - glyph.height * scale // 2, scale=scale)
        self._length = length
        self._center_x = center_x
        self._cell_width = glyph.width * scale
        self._palette = displayio.Palette(2)
        self._palette[0] = background
        self._palette[1] = color
        self._glyphs = {" ": glyph.tile_index}
        self._tiles = displayio.TileGrid(
            glyph.bitmap,
            pixel_shader=self._palette,
            width=length,
            height=1,
            tile_width=glyph.width,
            tile_height=glyph.height,
            default_tile=glyph.tile_index,
        )
        self.append(self._tiles)
        self._text = " " * length
        self.text = text

    @property
    def text(self) -> str:
        return self._text.rstrip()

    @text.setter
    def text(self, value: str) -> None:
        if len(value) > self._length:
            raise ValueError("Text exceeds fixed character count")
        if self._center_x is not None:
            self.x = self._center_x - len(value) * self._cell_width // 2
        value = value + " " * (self._length - len(value))
        for index, character in enumerate(value):
            if character != self._text[index]:
                if character not in self._glyphs:
                    self._glyphs[character] = terminalio.FONT.get_glyph(
                        ord(character)
                    ).tile_index
                self._tiles[index] = self._glyphs[character]
        self._text = value

    @property
    def color(self) -> int:
        return self._palette[1]

    @color.setter
    def color(self, value: int) -> None:
        if value != self._palette[1]:
            self._palette[1] = value


class BorderAnimation:
    ACCENT_LENGTH = 48
    SPEED = 96

    def __init__(
        self,
        group: displayio.Group,
        background: int,
        border: int,
        cyan: int,
        magenta: int,
    ) -> None:
        palette = displayio.Palette(4)
        for index, color in enumerate((background, border, cyan, magenta)):
            palette[index] = color
        self.regions = (
            (23, 1, 194, 2),
            (217, 1, 22, 22),
            (237, 23, 2, 234),
            (217, 257, 22, 22),
            (23, 277, 194, 2),
            (1, 257, 22, 22),
            (1, 23, 2, 234),
            (1, 1, 22, 22),
        )
        self.bitmaps = []
        for x, y, width, height in self.regions:
            bitmap = displayio.Bitmap(width, height, 4)
            self.bitmaps.append(bitmap)
            group.append(displayio.TileGrid(bitmap, pixel_shader=palette, x=x, y=y))
        self._pixels = array("I")
        self._offsets = array("H", [0])
        seen = displayio.Bitmap(240, 280, 2)

        def point(x, y):
            before = len(self._pixels)
            for py in (y, y + 1):
                for px in (x, x + 1):
                    if seen[px, py]:
                        continue
                    seen[px, py] = 1
                    if px <= 22:
                        region = 7 if py <= 22 else (5 if py >= 257 else 6)
                    elif px >= 217:
                        region = 1 if py <= 22 else (3 if py >= 257 else 2)
                    else:
                        region = 0 if py <= 2 else 4
                    rx, ry, _, _ = self.regions[region]
                    lx, ly = px - rx, py - ry
                    self.bitmaps[region][lx, ly] = 1
                    self._pixels.append((region << 16) | (ly << 8) | lx)
            if len(self._pixels) > before:
                self._offsets.append(len(self._pixels))

        def arc(cx, cy, start):
            for step in range(32):
                angle = start + step * math.pi / 62
                point(
                    round(cx + 20 * math.cos(angle)), round(cy + 20 * math.sin(angle))
                )

        for x in range(21, 217):
            point(x, 1)
        arc(217, 21, -math.pi / 2)
        for y in range(22, 257):
            point(237, y)
        arc(217, 257, 0)
        for x in range(216, 20, -1):
            point(x, 277)
        arc(21, 257, math.pi / 2)
        for y in range(256, 21, -1):
            point(1, y)
        top_left = len(self._offsets) - 1
        arc(21, 21, math.pi)
        if (len(self._offsets) - 1) % 2:
            self._offsets.append(self._offsets[-1])
        self.length = len(self._offsets) - 1
        self._origin = (top_left + 16 - self.ACCENT_LENGTH // 2) % self.length
        self._head = None
        self.animate(0)

    def _paint(self, start, count, color):
        for step in range(count):
            index = (start + step) % self.length
            for cell in range(self._offsets[index], self._offsets[index + 1]):
                packed = self._pixels[cell]
                self.bitmaps[packed >> 16][packed & 255, (packed >> 8) & 255] = color

    def animate(self, elapsed_seconds: float) -> bool:
        head = (self._origin + int(elapsed_seconds * self.SPEED)) % self.length
        if head == self._head:
            return False
        delta = (
            self.ACCENT_LENGTH
            if self._head is None
            else (head - self._head) % self.length
        )
        if self._head is not None:
            for offset in (0, self.length // 2):
                self._paint(self._head + offset, min(delta, self.ACCENT_LENGTH), 1)
        for offset, color in ((0, 2), (self.length // 2, 3)):
            count = min(delta, self.ACCENT_LENGTH)
            self._paint(head + offset + self.ACCENT_LENGTH - count, count, color)
        self._head = head
        return True


class VolumeDisplay:
    BACKGROUND = 0x080912
    TRACK = 0x292439
    BORDER = 0x3E2B58
    CYAN = 0x61F6E8
    MAGENTA = 0xFF57B5
    WHITE = 0xE8E4F5
    DIM = 0xA19AB8
    METER_WIDTH = 192
    UNIT_Y = 131
    STREAMING_FPS = 12

    def __init__(self, spi: object | None = None) -> None:
        self._owns_spi = spi is None
        self._closed = False
        self._streaming = False
        if self._owns_spi:
            displayio.release_displays()
            spi = busio.SPI(clock=board.GP22, MOSI=board.GP19)
        self.spi = spi
        self.bus = fourwire.FourWire(
            self.spi,
            command=board.GP28,
            chip_select=board.GP12,
            baudrate=20_000_000,
        )
        self.display = ST7789(
            self.bus,
            width=240,
            height=280,
            rowstart=20,
            rotation=180,
            auto_refresh=False,
            native_frames_per_second=self.STREAMING_FPS,
        )
        self.group = displayio.Group()
        self._palettes = {}
        self._background()
        self._text("SHAGGY", 120, 31, scale=2, color=self.WHITE, anchor=0.5)
        self._rect(109, 49, 22, 2, self.MAGENTA)
        self._text("VOLUME", 120, 72, color=self.DIM, anchor=0.5)
        self.percentage = FixedText(
            "0", 0, 119, 3, 6, self.CYAN, self.BACKGROUND, center_x=110
        )
        self.group.append(self.percentage)
        self.unit = self._text("%", 136, self.UNIT_Y, scale=2, color=self.MAGENTA)
        self._rect(24, 180, self.METER_WIDTH, 6, self.TRACK)
        self.fill = self._rect(24, 180, 1, 6, self.CYAN)
        self.fill.hidden = True
        self.level = FixedText(
            "MUTED", 0, 211, 8, 1, self.MAGENTA, self.BACKGROUND, center_x=120
        )
        self.group.append(self.level)
        self.state = self._text("READY", 120, 250, color=self.DIM, anchor=0.5)
        self.display.root_group = self.group
        self._refresh()
        self._last_pixels = 0
        self._last_percent = 0
        self._last_db = None
        self._last_muted = True
        self._animation_dirty = False

    def _background(self) -> None:
        self._rect(0, 0, 240, 280, self.BACKGROUND)
        self.border = BorderAnimation(
            self.group, self.BACKGROUND, self.BORDER, self.CYAN, self.MAGENTA
        )

    def animate(self, elapsed_seconds: float) -> None:
        self._animation_dirty |= self.border.animate(elapsed_seconds)

    def set_streaming(self, enabled: bool) -> None:
        if enabled == self._streaming:
            return
        self.display.auto_refresh = enabled
        self._streaming = enabled

    def _refresh(self) -> None:
        if not self._streaming:
            self.display.refresh(minimum_frames_per_second=0)

    def _palette(self, color):
        if color not in self._palettes:
            palette = displayio.Palette(1)
            palette[0] = color
            self._palettes[color] = palette
        return self._palettes[color]

    def _rect(self, x, y, width, height, color):
        item = vectorio.Rectangle(
            pixel_shader=self._palette(color), width=width, height=height, x=x, y=y
        )
        self.group.append(item)
        return item

    def _text(self, text, x, y, scale=1, color=WHITE, anchor=0):
        item = label.Label(terminalio.FONT, text=text, color=color, scale=scale)
        item.anchor_point = (anchor, 0.5)
        item.anchored_position = (x, y)
        self.group.append(item)
        return item

    def update(self, control: VolumeControl, applied_db: float) -> None:
        percent = control.percent
        pixels = (
            0 if control.muted else round(control.position * self.METER_WIDTH / 100)
        )
        changed = self._animation_dirty
        if pixels != self._last_pixels:
            self.fill.width = max(1, pixels)
            self.fill.hidden = pixels == 0
            self._last_pixels = pixels
            changed = True
        if percent != self._last_percent:
            self.percentage.text = str(percent)
            if len(str(percent)) != len(str(self._last_percent)):
                self.unit.anchored_position = (
                    self.percentage.x + len(str(percent)) * 36 + 8,
                    self.UNIT_Y,
                )
            self._last_percent = percent
            changed = True
        if applied_db != self._last_db or control.muted != self._last_muted:
            self.level.text = "MUTED" if control.muted else "%.1f dB" % applied_db
            self.level.color = self.MAGENTA if control.muted else self.WHITE
            self._last_db = applied_db
            self._last_muted = control.muted
            changed = True
        if changed:
            self._refresh()
            self._animation_dirty = False

    def set_state(self, text: str) -> None:
        text = text.upper()[:28]
        if self.state.text == text:
            return
        self.state.text = text
        self._refresh()

    def close(self) -> None:
        if self._closed:
            return
        self.set_streaming(False)
        displayio.release_displays()
        if self._owns_spi:
            self.spi.deinit()
        self._closed = True

    def stop(self) -> None:
        self.percentage.text = "0"
        self.unit.anchored_position = (136, self.UNIT_Y)
        self.level.text = "MUTED"
        self.level.color = self.MAGENTA
        self.fill.hidden = True
        self.state.text = "STOPPED"
        self._last_pixels = 0
        self._last_percent = 0
        self._last_db = None
        self._last_muted = True
        self._refresh()


def read_knob(pot: analogio.AnalogIn) -> int:
    samples = [pot.value for _ in range(9)]
    samples.sort()
    return samples[4]


def main(
    duration_seconds: float = TEST_SECONDS, audio_root: str = "/beats/house"
) -> None:
    if not 0 < duration_seconds < float("inf"):
        raise ValueError("Test duration must be finite and positive")
    screen = VolumeDisplay()
    reset = digitalio.DigitalInOut(board.GP13)
    reset.switch_to_output(value=False)
    time.sleep(0.1)
    reset.value = True
    time.sleep(0.1)
    i2c = busio.I2C(scl=board.GP15, sda=board.GP14)
    dac = None
    audio = None
    mixer = None
    pot = None
    handles = []
    waves = []
    try:
        dac = adafruit_tlv320.TLV320DAC3100(i2c)
        dac.speaker_mute = True
        dac.speaker_output = True
        dac.headphone_output = False
        dac.speaker_gain = SPEAKER_GAIN_DB
        dac.speaker_volume = 0
        dac.dac_volume = -40
        dac.speaker_mute = True
        pot = analogio.AnalogIn(board.GP26)
        control = VolumeControl(
            initial_raw=read_knob(pot),
            adc_min=ADC_MIN,
            adc_max=ADC_MAX,
            ramp_percent_per_second=500,
        )
        sample_rate = None
        for index, name in enumerate(STEM_NAMES):
            handle = open("%s/stem%d_%s.wav" % (audio_root, index + 1, name), "rb")
            handles.append(handle)
            wave = audiocore.WaveFile(handle)
            waves.append(wave)
            if (
                wave.sample_rate not in (22050, 44100)
                or (wave.channel_count, wave.bits_per_sample) != (1, 16)
                or (sample_rate is not None and sample_rate != wave.sample_rate)
            ):
                raise ValueError(
                    "Every stem must share mono 16-bit PCM at 22050 or 44100 Hz"
                )
            sample_rate = wave.sample_rate
        dac.configure_clocks(sample_rate=sample_rate, bit_depth=16)
        mixer = audiomixer.Mixer(
            voice_count=9,
            sample_rate=sample_rate,
            channel_count=1,
            bits_per_sample=16,
            samples_signed=True,
            buffer_size=8192 if sample_rate == 22050 else 16384,
        )
        audio = audiobusio.I2SOut(board.GP16, board.GP17, board.GP18)
        screen.set_state("PLAYING")
        gc.collect()
        audio.play(mixer)
        audio.pause()
        for index, wave in enumerate(waves):
            mixer.voice[index].level = STEM_LEVEL
            mixer.voice[index].play(wave, loop=True)
        audio.resume()
        start = time.monotonic()
        previous = start
        next_frame = start
        next_status = start
        applied_db = -40.0
        muted = True
        while time.monotonic() - start < duration_seconds:
            now = time.monotonic()
            raw = read_knob(pot)
            control.update(raw, now - previous)
            previous = now
            requested_db = round(control.db * 2) / 2
            if requested_db != applied_db:
                dac.dac_volume = requested_db
                applied_db = dac.dac_volume
            if control.muted != muted:
                dac.speaker_mute = control.muted
                muted = control.muted
            if now >= next_frame:
                screen.animate(now - start)
                screen.update(control, applied_db)
                next_frame = max(
                    now + 1 / DISPLAY_FPS, time.monotonic() + MIN_CONTROL_WINDOW
                )
            if now >= next_status:
                shorted = dac.speaker_shorted
                overtemperature = dac.overtemperature
                if shorted or overtemperature:
                    raise RuntimeError("DAC reports short or overtemperature")
                print(
                    json.dumps(
                        {
                            "elapsed": now - start,
                            "adc": raw,
                            "percent": control.percent,
                            "dac_db": applied_db,
                            "muted": muted,
                            "overflow_raw": dac._page0._read_register(0x27),
                            "voices": [voice.playing for voice in mixer.voice],
                        }
                    )
                )
                next_status = now + 1
            time.sleep(CONTROL_INTERVAL)
    finally:
        reset.value = False
        if audio is not None:
            audio.stop()
            audio.deinit()
        reset.deinit()
        if mixer is not None:
            mixer.deinit()
        for wave in waves:
            wave.deinit()
        for handle in handles:
            handle.close()
        if pot is not None:
            pot.deinit()
        i2c.deinit()
        screen.stop()


if __name__ == "__main__":
    main()
