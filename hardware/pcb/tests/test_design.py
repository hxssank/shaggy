from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PCB = Path(__file__).resolve().parents[1]
REPO = PCB.parents[1]
DESIGN = json.loads((PCB / "design.json").read_text())
XML = ET.parse(PCB / "reports/schematic-netlist.xml")
COMPONENTS = {node.attrib["ref"]: node for node in XML.findall("./components/comp")}
NETS = {
    (node.attrib["ref"], node.attrib["pin"]): net.attrib["name"]
    for net in XML.findall("./nets/net")
    for node in net.findall("node")
}

# Pico 2 physical header numbering from the Raspberry Pi Pico 2 datasheet.
GPIO_TO_HEADER = dict(
    zip(
        list(range(23)) + [26, 27, 28],
        [
            1,
            2,
            4,
            5,
            6,
            7,
            9,
            10,
            11,
            12,
            14,
            15,
            16,
            17,
            19,
            20,
            21,
            22,
            24,
            25,
            26,
            27,
            29,
            31,
            32,
            34,
        ],
        strict=True,
    )
)
# Nexperia 74LVC244A pin pairs, independent of symbol placement.
BUFFER_PAIRS = {2: 18, 4: 16, 6: 14, 8: 12, 11: 9, 13: 7, 15: 5, 17: 3}


def firmware(file: str) -> ast.Module:
    return ast.parse((REPO / "firmware" / file).read_text())


def pin_net(ref: str, number: int) -> str:
    return NETS[ref, str(number)]


def board_gpio(node: ast.AST) -> int:
    assert isinstance(node, ast.Attribute), ast.dump(node)
    assert isinstance(node.value, ast.Name) and node.value.id == "board"
    assert node.attr.startswith("GP"), ast.dump(node)
    return int(node.attr[2:])


def call(file: str, module: str, name: str) -> ast.Call:
    matches = [
        node
        for node in ast.walk(firmware(file))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == module
        and node.func.attr == name
    ]
    assert len(matches) == 1, (file, module, name, len(matches))
    return matches[0]


def keyword_gpio(node: ast.Call, keyword: str) -> int:
    return board_gpio(next(item.value for item in node.keywords if item.arg == keyword))


def gpio_net(number: int) -> str:
    return pin_net("U1", GPIO_TO_HEADER[number])


def test_exported_schematic_matches_every_contract_pin_and_component() -> None:
    expected_refs = {component["ref"] for component in DESIGN["components"]}
    assert set(COMPONENTS) == expected_refs
    expected_pins = set()
    for component in DESIGN["components"]:
        ref = component["ref"]
        assert COMPONENTS[ref].findtext("value") == component["value"]
        assert COMPONENTS[ref].findtext("footprint") == component["footprint"]
        assert COMPONENTS[ref].findtext("tstamps") == component["uuid"]
        for number, expected in component["pins"].items():
            key = ref, number
            expected_pins.add(key)
            actual = NETS[key]
            if expected is None:
                assert actual.startswith("unconnected-"), (key, actual)
            else:
                assert actual == expected, (key, expected, actual)
    assert set(NETS) == expected_pins


def test_hall_inputs_match_firmware_through_noninverting_buffers() -> None:
    declaration = next(
        node
        for node in firmware("shaggy.py").body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SENSOR_PINS"
            for target in node.targets
        )
    )
    assert isinstance(declaration.value, ast.Tuple)
    gpios = [board_gpio(node) for node in declaration.value.elts]
    assert gpios == list(range(9))
    for index, gpio in enumerate(gpios, 1):
        sensor_net = pin_net(f"J{index}", 3)
        assert sensor_net != gpio_net(gpio)
        matches = [
            (ref, output)
            for ref in ("U2", "U3")
            for input_pin, output in BUFFER_PAIRS.items()
            if pin_net(ref, input_pin) == sensor_net
        ]
        assert len(matches) == 1
        ref, output = matches[0]
        assert pin_net(ref, output) == gpio_net(gpio)
        assert pin_net(f"J{index}", 1) == "GND"
        assert pin_net(f"J{index}", 2) == pin_net("JP1", 2) == "HALL_V"
        assert pin_net(f"R{index}", 1) == "HALL_V"
        assert pin_net(f"R{index}", 2) == sensor_net


@pytest.mark.parametrize("ref", ["U2", "U3"])
def test_buffer_power_and_output_enable(ref: str) -> None:
    assert COMPONENTS[ref].findtext("value") == "74LVC244AD"
    assert COMPONENTS[ref].findtext("footprint") == (
        "Package_SO:SOIC-20W_7.5x12.8mm_P1.27mm"
    )
    assert pin_net(ref, 20) == pin_net("U1", 36) == "PICO_3V3"
    assert {pin_net(ref, number) for number in (1, 10, 19)} == {"GND"}
    for input_pin, output_pin in BUFFER_PAIRS.items():
        if pin_net(ref, output_pin).startswith("unconnected-"):
            assert pin_net(ref, input_pin) == "GND"


def test_spi_card_and_display_match_firmware() -> None:
    spi = call("sd_storage.py", "busio", "SPI")
    for argument, resistor, sd_pin, lcd_pin in [
        ("clock", "R15", 4, 4),
        ("MOSI", "R16", 3, 3),
    ]:
        assert gpio_net(keyword_gpio(spi, argument)) == pin_net(resistor, 1)
        assert pin_net(resistor, 2) == pin_net("J11", sd_pin)
        assert pin_net(resistor, 2) == pin_net("J10", lcd_pin)
        assert COMPONENTS[resistor].findtext("value") == "33R"
    assert gpio_net(keyword_gpio(spi, "MISO")) == pin_net("J11", 5)
    card = call("sd_storage.py", "sdcardio", "SDCard")
    assert gpio_net(board_gpio(card.args[1])) == pin_net("J11", 2)
    lcd = call("volume_test.py", "fourwire", "FourWire")
    assert gpio_net(keyword_gpio(lcd, "command")) == pin_net("J10", 6)
    assert gpio_net(keyword_gpio(lcd, "chip_select")) == pin_net("J10", 5)


def test_audio_i2s_i2c_and_reset_match_firmware() -> None:
    audio = call("shaggy.py", "audiobusio", "I2SOut")
    assert len(audio.args) == 3
    for argument, resistor, dac_pin in zip(
        audio.args, ("R17", "R18", "R19"), (6, 7, 8), strict=True
    ):
        assert gpio_net(board_gpio(argument)) == pin_net(resistor, 1)
        assert pin_net(resistor, 2) == pin_net("J12", dac_pin)
        assert COMPONENTS[resistor].findtext("value") == "33R"
    i2c = call("shaggy.py", "busio", "I2C")
    assert gpio_net(keyword_gpio(i2c, "sda")) == pin_net("J12", 4)
    assert gpio_net(keyword_gpio(i2c, "scl")) == pin_net("J12", 5)
    reset = next(
        node.value
        for node in ast.walk(firmware("shaggy.py"))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "reset"
            for target in node.targets
        )
    )
    assert isinstance(reset, ast.Call)
    assert gpio_net(board_gpio(reset.args[0])) == pin_net("J12", 3)


def test_potentiometer_reaches_firmware_adc_through_filter() -> None:
    adc = call("shaggy.py", "analogio", "AnalogIn")
    assert pin_net("J13", 1) == "PICO_3V3"
    assert pin_net("J13", 3) == "GND"
    assert pin_net("J13", 2) == pin_net("R13", 1)
    assert pin_net("R13", 2) == gpio_net(board_gpio(adc.args[0]))
    assert pin_net("C8", 1) == pin_net("R13", 2)
    assert pin_net("C8", 2) == "GND"
    assert COMPONENTS["R13"].findtext("value") == "1k"
    assert COMPONENTS["C8"].findtext("value") == "100nF"


def test_regulator_pins_and_power_sources_remain_separate() -> None:
    assert COMPONENTS["U4"].findtext("value") == "MCP1826S-3302E/DB"
    assert COMPONENTS["U4"].findtext("footprint") == (
        "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    )
    assert pin_net("U4", 1) == pin_net("U1", 40) == "VBUS_5V"
    assert pin_net("U4", 2) == "GND"
    assert pin_net("U4", 3) == "AUX_3V3"
    rails = {pin_net("U1", 36), pin_net("U1", 40), pin_net("U4", 3), "GND"}
    assert len(rails) == 4
    for net in XML.findall("./nets/net"):
        if net.attrib["name"] not in rails - {"GND"}:
            continue
        sources = [
            node
            for node in net.findall("node")
            if node.attrib["pintype"] == "power_out"
        ]
        assert len(sources) == 1, net.attrib["name"]
    assert pin_net("JP1", 1) == "AUX_3V3"
    assert pin_net("JP1", 3) == "VBUS_5V"
    assert pin_net("J10", 1) == pin_net("J10", 8) == "PICO_3V3"
    assert pin_net("J11", 1) == "AUX_3V3"
    assert pin_net("J12", 1) == "VBUS_5V"
    assert pin_net("C3", 1) == "VBUS_5V"
    assert pin_net("C4", 1) == "AUX_3V3"
    assert pin_net("C3", 2) == pin_net("C4", 2) == "GND"


def test_reset_rc_drives_schmitt_input_not_lcd_level_translator() -> None:
    delay = pin_net("R12", 2)
    assert delay == pin_net("C9", 1) == pin_net("U3", 11)
    assert pin_net("U3", BUFFER_PAIRS[11]) == pin_net("J10", 7)
    assert delay != pin_net("J10", 7)
    assert pin_net("R12", 1) == "PICO_3V3"
    assert pin_net("C9", 2) == "GND"
    assert COMPONENTS["R12"].findtext("value") == "10k"
    assert COMPONENTS["C9"].findtext("value") == "100nF"
    assert COMPONENTS["R10"].findtext("value") == "100k"
    assert pin_net("R10", 1) == "PICO_3V3"
    assert pin_net("R10", 2) == pin_net("J10", 5)


def test_sd_cs_pullup_uses_card_supply() -> None:
    assert pin_net("R11", 1) == pin_net("J11", 1) == "AUX_3V3"
    assert pin_net("R11", 2) == pin_net("J11", 2)
    assert COMPONENTS["R11"].findtext("value") == "10k"


def test_erc_report_has_no_electrical_violations() -> None:
    report = json.loads((PCB / "reports/schematic-erc.json").read_text())
    assert not [item for sheet in report["sheets"] for item in sheet["violations"]]
