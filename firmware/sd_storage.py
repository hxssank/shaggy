import gc

import board
import busio
import digitalio
import sdcardio
import storage


def _output_high(pin: object) -> digitalio.DigitalInOut:
    output = digitalio.DigitalInOut(pin)
    try:
        output.switch_to_output(value=True)
    except BaseException:
        output.deinit()
        raise
    return output


class SDStorage:
    MOUNT_POINT = "/sd"

    def __init__(self, baudrate: int = 16_000_000) -> None:
        if not isinstance(baudrate, int) or baudrate <= 0:
            raise ValueError("SD baudrate must be a positive integer")
        self.baudrate = baudrate
        self.spi = None
        self.mounted = False
        self.readonly = None
        self.error = None
        self._display_cs = None
        self._sd_cs = None
        self._card = None
        self._vfs = None
        self._closed = False
        try:
            self._display_cs = _output_high(board.GP12)
            self._sd_cs = _output_high(board.GP21)
            self.spi = busio.SPI(clock=board.GP22, MOSI=board.GP19, MISO=board.GP20)
        except BaseException:
            self._closed = True
            self._close_resources()
            raise

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SD storage is closed")

    def mount(self, *, readonly: bool = True) -> bool:
        self._check_open()
        if self.mounted:
            if readonly != self.readonly:
                raise RuntimeError("SD card is already mounted with different access")
            return True
        if self._display_cs is None:
            raise RuntimeError("Park the display chip select before mounting SD")
        self.error = None
        stage = "SD detection"
        try:
            self._sd_cs.deinit()
            self._sd_cs = None
            self._card = sdcardio.SDCard(self.spi, board.GP21, baudrate=self.baudrate)
            stage = "SD filesystem"
            self._vfs = storage.VfsFat(self._card)
            storage.mount(self._vfs, self.MOUNT_POINT, readonly=readonly)
            self.mounted = True
            self.readonly = readonly
            return True
        except Exception as error:
            self.error = "%s failed: %s" % (stage, error)
            self._vfs = None
            try:
                if self._card is not None:
                    self._card.deinit()
            finally:
                self._card = None
                gc.collect()
                self._sd_cs = _output_high(board.GP21)
            return False

    def release_display_cs(self) -> None:
        self._check_open()
        if self._display_cs is not None:
            self._display_cs.deinit()
            self._display_cs = None

    def park_display_cs(self) -> None:
        self._check_open()
        if self._display_cs is None:
            self._display_cs = _output_high(board.GP12)

    def _close_resources(self) -> None:
        failure = None
        if self.mounted:
            try:
                storage.umount(self.MOUNT_POINT)
            except Exception as error:
                failure = error
            self.mounted = False
            self.readonly = None
        self._vfs = None
        for name in ("_card", "spi", "_sd_cs", "_display_cs"):
            resource = getattr(self, name)
            if resource is not None:
                try:
                    resource.deinit()
                except Exception as error:
                    if failure is None:
                        failure = error
                setattr(self, name, None)
        if failure is not None:
            raise failure

    def close(self) -> None:
        if self._closed:
            return
        self.park_display_cs()
        self._closed = True
        self._close_resources()
