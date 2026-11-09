import sys
import time


def main() -> None:
    runtime = sys.implementation.name
    if runtime not in ("micropython", "circuitpython"):
        raise RuntimeError("Run this on the Pico 2 with mpremote, not desktop Python.")

    board_name = getattr(sys.implementation, "_machine", "")
    normalized_board = board_name.lower().replace(" ", "")
    if "pico2" not in normalized_board or "rp2350" not in normalized_board:
        raise RuntimeError("Expected a Pico 2 with RP2350; found: " + board_name)

    import gc

    print("Board:", board_name)
    print("Runtime:", sys.version)
    gc.collect()
    print("Free heap bytes:", gc.mem_free())

    if runtime == "circuitpython":
        import board
        import digitalio
        import microcontroller

        print("CPU Hz:", microcontroller.cpu.frequency)
        with digitalio.DigitalInOut(board.LED) as led:
            led.switch_to_output(value=False)
            try:
                for _ in range(5):
                    led.value = True
                    time.sleep(0.2)
                    led.value = False
                    time.sleep(0.2)
            finally:
                led.value = False
    else:
        from machine import Pin, freq

        print("CPU Hz:", freq())
        led = Pin("LED", Pin.OUT)
        try:
            for _ in range(5):
                led.on()
                time.sleep(0.2)
                led.off()
                time.sleep(0.2)
        finally:
            led.off()

    print("PASS: board identification and LED commands completed.")


if __name__ == "__main__":
    main()
