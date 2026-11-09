import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NS = uuid.UUID("2f17a198-4b01-49cb-bf98-4cb90094d5b1")
components = []


def add(
    ref: str,
    value: str,
    footprint: str,
    pins: dict,
    x: float,
    y: float,
    angle: float = 0,
    **extra: object,
) -> None:
    components.append(
        dict(
            ref=ref,
            value=value,
            footprint=footprint,
            pins={str(k): v for k, v in pins.items()},
            x=x,
            y=y,
            angle=angle,
            uuid=str(uuid.uuid5(NS, ref)),
            **extra,
        )
    )


def passive(
    ref: str, value: str, a: str, b: str, x: float, y: float, angle: float = 0
) -> None:
    library = (
        "Resistor_SMD:R_0805_2012Metric"
        if ref.startswith("R")
        else "Capacitor_SMD:C_0805_2012Metric"
    )
    add(ref, value, library, {1: a, 2: b}, x, y, angle)


def connector(
    ref: str, value: str, nets: list, x: float, y: float, angle: float = 0
) -> None:
    n = len(nets)
    fp = f"Connector_JST:JST_XH_B{n}B-XH-A_1x{n:02d}_P2.50mm_Vertical"
    add(ref, value, fp, dict(enumerate(nets, 1)), x, y, angle)


pico = {
    1: "TILE1",
    2: "TILE2",
    3: "GND",
    4: "TILE3",
    5: "TILE4",
    6: "TILE5",
    7: "TILE6",
    8: "GND",
    9: "TILE7",
    10: "TILE8",
    11: "TILE9",
    12: None,
    13: "GND",
    14: None,
    15: None,
    16: "LCD_CS",
    17: "DAC_RST",
    18: "GND",
    19: "I2C_SDA",
    20: "I2C_SCL",
    21: "MCU_BCLK",
    22: "MCU_WCLK",
    23: "GND",
    24: "MCU_I2S_DATA",
    25: "MCU_MOSI",
    26: "SPI_MISO",
    27: "SD_CS",
    28: "GND",
    29: "MCU_SCK",
    30: None,
    31: "VOLUME_ADC",
    32: None,
    33: "GND",
    34: "LCD_DC",
    35: None,
    36: "PICO_3V3",
    37: None,
    38: "GND",
    39: None,
    40: "VBUS_5V",
}
add(
    "U1",
    "Raspberry Pi Pico 2 socket",
    "Module:RaspberryPi_Pico_Common_THT",
    pico,
    45,
    3,
    datasheet="https://datasheets.raspberrypi.com/pico/pico-2-datasheet.pdf",
)
for i in range(1, 10):
    connector(
        f"J{i}", f"TILE {i}", ["GND", "HALL_V", f"HALL{i}"], 8, 11 + (i - 1) * 6.8
    )
    passive(f"R{i}", "10k", "HALL_V", f"HALL{i}", 20, 11 + (i - 1) * 6.8, 90)

inputs = [2, 4, 6, 8, 11, 13, 15, 17]
outputs = [18, 16, 14, 12, 9, 7, 5, 3]
for chip, count, offset, y in [("U2", 5, 0, 23), ("U3", 4, 5, 53)]:
    pins = {1: "GND", 19: "GND", 10: "GND", 20: "PICO_3V3"}
    for index, (a, z) in enumerate(zip(inputs, outputs)):
        pins[a] = f"HALL{offset + index + 1}" if index < count else "GND"
        pins[z] = f"TILE{offset + index + 1}" if index < count else None
    if chip == "U3":
        pins[11] = "LCD_RST_DELAY"
        pins[9] = "LCD_RST"
    add(
        chip,
        "74LVC244AD",
        "Package_SO:SOIC-20W_7.5x12.8mm_P1.27mm",
        pins,
        31,
        y,
        datasheet="https://assets.nexperia.com/documents/data-sheet/74LVC_LVCH244A.pdf",
    )
passive("C1", "100nF", "PICO_3V3", "GND", 36.5, 15, 90)
passive("C2", "100nF", "PICO_3V3", "GND", 36.5, 45, 90)
add(
    "JP1",
    "HALL SUPPLY: 1-2=3V3 / 2-3=5V",
    "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    {1: "AUX_3V3", 2: "HALL_V", 3: "VBUS_5V"},
    24,
    72,
    90,
)

add(
    "U4",
    "MCP1826S-3302E/DB",
    "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    {1: "VBUS_5V", 2: "GND", 3: "AUX_3V3"},
    79,
    14,
    datasheet="https://ww1.microchip.com/downloads/en/DeviceDoc/22057B.pdf",
)
passive("C3", "10uF 16V X7R", "VBUS_5V", "GND", 71, 17, 90)
passive("C4", "10uF 16V X7R", "AUX_3V3", "GND", 85, 17, 90)
connector(
    "J10",
    "LCD",
    [
        "PICO_3V3",
        "GND",
        "SPI_MOSI",
        "SPI_SCK",
        "LCD_CS",
        "LCD_DC",
        "LCD_RST",
        "PICO_3V3",
    ],
    73,
    45,
)
connector(
    "J11", "SD", ["AUX_3V3", "SD_CS", "SPI_MOSI", "SPI_SCK", "SPI_MISO", "GND"], 78, 30
)
connector(
    "J12",
    "DAC / TLV320",
    [
        "VBUS_5V",
        "GND",
        "DAC_RST",
        "I2C_SDA",
        "I2C_SCL",
        "I2S_BCLK",
        "I2S_WCLK",
        "I2S_DATA",
        "GND",
    ],
    71,
    62,
)
connector("J13", "VOLUME", ["PICO_3V3", "POT_WIPER", "GND"], 82, 74)
passive("C5", "100nF", "PICO_3V3", "GND", 73, 39, 90)
passive("C6", "100nF", "AUX_3V3", "GND", 78, 24, 90)
passive("C7", "100nF", "VBUS_5V", "GND", 71, 56, 90)
passive("C8", "100nF", "VOLUME_ADC", "GND", 68, 30, 90)
passive("C9", "100nF", "LCD_RST_DELAY", "GND", 91, 53, 90)
passive("C10", "10uF 16V X7R", "AUX_3V3", "GND", 82, 24, 90)
passive("R10", "100k", "PICO_3V3", "LCD_CS", 68, 46, 90)
passive("R11", "10k", "AUX_3V3", "SD_CS", 69, 35, 90)
passive("R12", "10k", "PICO_3V3", "LCD_RST_DELAY", 85, 53, 90)
passive("R13", "1k", "POT_WIPER", "VOLUME_ADC", 68, 26, 90)
passive("R14", "100k", "DAC_RST", "GND", 67, 58, 90)
for ref, a, b, x, y in [
    ("R15", "MCU_SCK", "SPI_SCK", 70, 21),
    ("R16", "MCU_MOSI", "SPI_MOSI", 70, 39),
    ("R17", "MCU_BCLK", "I2S_BCLK", 63, 56.5),
    ("R18", "MCU_WCLK", "I2S_WCLK", 59, 56.5),
    ("R19", "MCU_I2S_DATA", "I2S_DATA", 55, 56.5),
]:
    passive(ref, "33R", a, b, x, y)
for i, (x, y) in enumerate([(4, 4), (96, 4), (4, 76), (96, 76)], 1):
    add(f"H{i}", "M3 clearance", "MountingHole:MountingHole_3.2mm_M3", {}, x, y)

data = dict(
    name="shaggy-carrier",
    revision="A - prototype",
    width=100,
    height=80,
    root_uuid=str(uuid.uuid5(NS, "root")),
    components=components,
    notes=[
        "100 x 80 mm outline is provisional; enclosure fit is unmeasured.",
        "Pico USB is the only power input. "
        "Use a suitable 5 V supply; measure total current.",
        "J1-J9: 1=GND, 2=HALL_V, 3=signal. Verify actual sensor pin order.",
        "JP1: fit ONE shunt, default 1-2 for 3.3 V. Check sensor rating before 5 V.",
        "Buffers require Nexperia 74LVC244AD with 5 V tolerant Schmitt inputs.",
        "Speaker stays wired to DAC SPK+ and SPK-. Neither wire connects to GND.",
        "LCD RST uses buffered 10k/100nF power-on reset; "
        "BL is always on. Validate cold starts.",
        "Pico3V3 powers LCD, pot and buffers. "
        "AUX3V3 powers SD and selectable Hall rail.",
        "U4 loss at 5 V input is 0.51 W at 300 mA; verify temperature in enclosure.",
        "Connector order is the carrier order; build labeled adapter cables.",
        "Firmware GPIO assignment preserved. Tile physical order must be traced.",
    ],
)
(ROOT / "design.json").write_text(json.dumps(data, indent=2) + "\n")
