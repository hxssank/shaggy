"""Generate the editable carrier schematic from the shared electrical contract."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMESPACE = uuid.UUID("3d0311ca-4682-4a81-96dc-cbc2145d209e")


def uid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def q(value: object) -> str:
    return json.dumps(str(value))


def f(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def effects(size: float = 1.27, justify: str = "") -> str:
    align = f"(justify {justify})" if justify else ""
    return f"(effects (font (size {f(size)} {f(size)})) {align})"


def prop(
    name: str, value: str, x: float, y: float, *, hide: bool = False, size: float = 1.27
) -> str:
    hidden = "(hide yes)" if hide else ""
    return (
        f"(property {q(name)} {q(value)} (at {f(x)} {f(y)} 0) {hidden} {effects(size)})"
    )


def pin(
    number: str,
    name: str,
    kind: str,
    x: float,
    y: float,
    angle: int,
    length: float = 2.54,
) -> str:
    return (
        f"(pin {kind} line (at {f(x)} {f(y)} {angle}) (length {f(length)})"
        f"(name {q(name)} {effects(1.016)}) (number {q(number)} {effects(1.016)}))"
    )


def rect(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"(rectangle (start {f(x1)} {f(y1)}) (end {f(x2)} {f(y2)})"
        " (stroke (width 0.254) (type default)) (fill (type background)))"
    )


def poly(points: list[tuple[float, float]], width: float = 0.254) -> str:
    vertices = " ".join(f"(xy {f(x)} {f(y)})" for x, y in points)
    return (
        f"(polyline (pts {vertices}) (stroke (width {f(width)}) (type default))"
        " (fill (type none)))"
    )


def symbol(
    name: str, pins: list[dict], drawing: str, ref: str = "U", description: str = ""
) -> str:
    pin_text = "\n".join(pin(**p) for p in pins)
    return (
        f"(symbol {q(name)} (pin_names (offset 0.508))"
        " (exclude_from_sim no) (in_bom yes) (on_board yes)"
        f"{prop('Reference', ref, 0, 5.08)} {prop('Value', name, 0, 2.54)}"
        f"{prop('Footprint', '', 0, 0, hide=True)}"
        f" {prop('Datasheet', '', 0, 0, hide=True)}"
        f"{prop('Description', description, 0, 0, hide=True)}"
        f"(symbol {q(name + '_0_1')} {drawing}) (symbol {q(name + '_1_1')} {pin_text}))"
    )


def bipin(kind: str) -> tuple[list[dict], str]:
    pins = [
        dict(
            number="1",
            name="~",
            kind="passive",
            x=-5.08,
            y=0,
            angle=0,
            length=2.54 if kind == "R" else 4.064,
        ),
        dict(
            number="2",
            name="~",
            kind="passive",
            x=5.08,
            y=0,
            angle=180,
            length=2.54 if kind == "R" else 4.064,
        ),
    ]
    if kind == "R":
        drawing = rect(-2.54, 1.016, 2.54, -1.016)
    else:
        drawing = poly([(-1.016, 2.54), (-1.016, -2.54)]) + poly(
            [(1.016, 2.54), (1.016, -2.54)]
        )
    return pins, drawing


def pico_pins() -> list[dict]:
    names = [
        "GP0",
        "GP1",
        "GND",
        "GP2",
        "GP3",
        "GP4",
        "GP5",
        "GND",
        "GP6",
        "GP7",
        "GP8",
        "GP9",
        "GND",
        "GP10",
        "GP11",
        "GP12",
        "GP13",
        "GND",
        "GP14",
        "GP15",
        "GP16",
        "GP17",
        "GND",
        "GP18",
        "GP19",
        "GP20",
        "GP21",
        "GND",
        "GP22",
        "RUN",
        "GP26/ADC0",
        "GP27/ADC1",
        "AGND",
        "GP28/ADC2",
        "ADC_VREF",
        "3V3_OUT",
        "3V3_EN",
        "GND",
        "VSYS",
        "VBUS_USB",
    ]
    pins = []
    for i, name in enumerate(names, 1):
        kind = "bidirectional"
        if name in {"GND", "AGND", "VSYS", "ADC_VREF"}:
            kind = "power_in"
        if name in {"3V3_OUT", "VBUS_USB"}:
            kind = "power_out"
        if name in {"RUN", "3V3_EN"}:
            kind = "input"
        left = i <= 20
        y = 24.13 - (i - 1 if left else 40 - i) * 2.54
        pins.append(
            dict(
                number=str(i),
                name=name,
                kind=kind,
                x=-20.32 if left else 20.32,
                y=y,
                angle=0 if left else 180,
            )
        )
    return pins


def buffer_pins() -> list[dict]:
    left = [
        ("1", "~{1OE}", "input"),
        ("2", "1A0", "input"),
        ("4", "1A1", "input"),
        ("6", "1A2", "input"),
        ("8", "1A3", "input"),
        ("11", "2A3", "input"),
        ("13", "2A2", "input"),
        ("15", "2A1", "input"),
        ("17", "2A0", "input"),
        ("19", "~{2OE}", "input"),
    ]
    right = [
        ("20", "VCC", "power_in"),
        ("18", "1Y0", "tri_state"),
        ("16", "1Y1", "tri_state"),
        ("14", "1Y2", "tri_state"),
        ("12", "1Y3", "tri_state"),
        ("9", "2Y3", "tri_state"),
        ("7", "2Y2", "tri_state"),
        ("5", "2Y1", "tri_state"),
        ("3", "2Y0", "tri_state"),
        ("10", "GND", "power_in"),
    ]
    pins = []
    for is_left, group in [(True, left), (False, right)]:
        for i, (number, name, kind) in enumerate(group):
            pins.append(
                dict(
                    number=number,
                    name=name,
                    kind=kind,
                    x=-15.24 if is_left else 15.24,
                    y=11.43 - i * 2.54,
                    angle=0 if is_left else 180,
                )
            )
    return pins


def wire(a: tuple[float, float], b: tuple[float, float], name: str) -> str:
    return (
        f"(wire (pts (xy {f(a[0])} {f(a[1])}) (xy {f(b[0])} {f(b[1])}))"
        f" (stroke (width 0) (type default)) (uuid {q(uid(name))}))"
    )


def net_label(net: str, x: float, y: float, angle: int, key: str) -> str:
    return (
        f"(global_label {q(net)} (shape input) (at {f(x)} {f(y)} {angle})"
        f" {effects(1.016, 'left' if angle == 0 else 'right')}"
        f" (uuid {q(uid(key))})"
        f" {prop('Intersheetrefs', '${INTERSHEET_REFS}', x, y, hide=True)})"
    )


def text(value: str, x: float, y: float, size: float = 1.27) -> str:
    return (
        f"(text {q(value)} (at {f(x)} {f(y)} 0) {effects(size, 'left top')}"
        f" (uuid {q(uid('text:' + value))}))"
    )


def connector(names: list[str]) -> tuple[list[dict], str]:
    half = (len(names) - 1) * 1.27
    pins = [
        dict(
            number=str(i + 1),
            name=name,
            kind="passive",
            x=-10.16,
            y=half - i * 2.54,
            angle=0,
        )
        for i, name in enumerate(names)
    ]
    return pins, rect(-7.62, half + 2.54, 7.62, -half - 2.54)


def instance(
    component: dict,
    name: str,
    pins: list[dict],
    x: float,
    y: float,
    root_uuid: str,
    top: float,
    value_size: float = 1.27,
) -> str:
    ref = component["ref"]
    fields = "\n".join(
        [
            prop("Reference", ref, x, y - top - 5.08),
            prop("Value", component["value"], x, y - top - 2.54, size=value_size),
            prop("Footprint", component.get("footprint", ""), x, y, hide=True),
            prop("Datasheet", component.get("datasheet", ""), x, y, hide=True),
        ]
    )
    pin_instances = "\n".join(
        f"(pin {q(p['number'])} (uuid {q(uid(ref + ':pin:' + p['number']))}))"
        for p in pins
    )
    assembly = "no" if ref.startswith("#") else "yes"
    in_bom = "no" if ref.startswith(("#", "H")) else "yes"
    return (
        f"(symbol (lib_id {q('Shaggy:' + name)}) (at {f(x)} {f(y)} 0)"
        " (unit 1) (exclude_from_sim no)"
        f"(in_bom {in_bom}) (on_board {assembly}) (dnp no)"
        f" (uuid {q(component['uuid'])}) {fields} {pin_instances}"
        f'(instances (project "shaggy-carrier" (path {q("/" + root_uuid)}'
        f" (reference {q(ref)}) (unit 1)))))"
    )


def main() -> None:
    design = json.loads((ROOT / "design.json").read_text())
    root_uuid = design["root_uuid"]
    libraries: dict[str, tuple[list[dict], str]] = {}
    libraries["Pico_2_socket"] = pico_pins(), rect(-17.78, 26.67, 17.78, -26.67)
    libraries["74LVC244AD"] = buffer_pins(), rect(-12.7, 13.97, 12.7, -13.97)
    libraries["MCP1826S"] = (
        [
            dict(number="1", name="VIN", kind="power_in", x=-10.16, y=2.54, angle=0),
            dict(
                number="2", name="GND/TAB", kind="power_in", x=-10.16, y=-2.54, angle=0
            ),
            dict(number="3", name="VOUT", kind="power_out", x=10.16, y=2.54, angle=180),
        ],
        rect(-7.62, 5.08, 7.62, -5.08),
    )
    libraries["R"] = bipin("R")
    libraries["C"] = bipin("C")
    libraries["Hall_cable"] = connector(["GND", "VCC", "SIG"])
    libraries["LCD_cable"] = connector(
        ["VCC", "GND", "DIN", "CLK", "CS", "DC", "RST", "BL"]
    )
    libraries["SD_cable"] = connector(["3V3", "CS", "MOSI", "CLK", "MISO", "GND"])
    libraries["DAC_cable"] = connector(
        ["VIN", "GND", "RST", "SDA", "SCL", "BCLK", "WCLK", "DIN", "GND"]
    )
    libraries["Pot_cable"] = connector(["CW", "WIPER", "CCW"])
    libraries["Hall_supply_select"] = connector(["3V3", "HALL_V", "5V"])
    libraries["Mounting_hole"] = (
        [],
        "(circle (center 0 0) (radius 2.54)"
        " (stroke (width 0.254) (type default)) (fill (type none)))",
    )
    libraries["PWR_FLAG"] = (
        [dict(number="1", name="pwr", kind="power_out", x=0, y=0, angle=90, length=0)],
        poly([(0, 0), (0, 2.54), (-1.27, 3.81), (1.27, 3.81), (0, 2.54)]),
    )
    definitions = {
        name: symbol(
            name,
            pins,
            drawing,
            "R"
            if name == "R"
            else "C"
            if name == "C"
            else "J"
            if "cable" in name
            else "U",
        )
        for name, (pins, drawing) in libraries.items()
    }
    libfile = (
        '(kicad_symbol_lib (version 20250114) (generator "kicad_symbol_editor")\n'
        + "\n".join(definitions.values())
        + ")\n"
    )
    (ROOT / "shaggy-carrier.kicad_sym").write_text(libfile)
    (ROOT / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "Shaggy")(type "KiCad")'
        '(uri "${KIPRJMOD}/shaggy-carrier.kicad_sym")(options "")'
        '(descr "Shaggy carrier symbols")))\n'
    )

    locations = {
        "U1": ("Pico_2_socket", 90.17, 85.09, 26.67),
        "U2": ("74LVC244AD", 265.43, 64.77, 13.97),
        "U3": ("74LVC244AD", 265.43, 127.0, 13.97),
        "U4": ("MCP1826S", 69.85, 194.31, 5.08),
        "JP1": ("Hall_supply_select", 69.85, 256.54, 5.08),
        "J10": ("LCD_cable", 420.37, 200.66, 11.43),
        "J11": ("SD_cable", 539.75, 196.85, 8.89),
        "J12": ("DAC_cable", 265.43, 201.93, 12.7),
        "J13": ("Pot_cable", 500.38, 299.72, 5.08),
        "C1": ("C", 265.43, 93.98, 2.54),
        "C2": ("C", 265.43, 156.21, 2.54),
        "C3": ("C", 54.61, 224.79, 2.54),
        "C4": ("C", 135.89, 224.79, 2.54),
        "C5": ("C", 420.37, 229.87, 2.54),
        "C6": ("C", 539.75, 222.25, 2.54),
        "C7": ("C", 265.43, 232.41, 2.54),
        "C8": ("C", 500.38, 340.36, 2.54),
        "C9": ("C", 420.37, 275.59, 2.54),
        "C10": ("C", 539.75, 237.49, 2.54),
        "R10": ("R", 420.37, 245.11, 1.27),
        "R11": ("R", 539.75, 252.73, 1.27),
        "R12": ("R", 420.37, 260.35, 1.27),
        "R13": ("R", 500.38, 322.58, 1.27),
        "R14": ("R", 265.43, 247.65, 1.27),
        "R15": ("R", 95.25, 298.45, 1.27),
        "R16": ("R", 95.25, 313.69, 1.27),
        "R17": ("R", 265.43, 271.78, 1.27),
        "R18": ("R", 265.43, 287.02, 1.27),
        "R19": ("R", 265.43, 302.26, 1.27),
    }
    for i in range(1, 10):
        col, row = (i - 1) % 3, (i - 1) // 3
        x, y = 401.32 + col * 68.58, 45.72 + row * 40.64
        locations[f"J{i}"] = "Hall_cable", x, y, 5.08
        locations[f"R{i}"] = "R", x, y + 17.78, 1.27
    for i in range(1, 5):
        locations[f"H{i}"] = "Mounting_hole", 41.91 + (i - 1) * 39.37, 350.52, 2.54

    embedded = "\n".join(
        value.replace(f'(symbol "{name}"', f'(symbol "Shaggy:{name}"', 1)
        for name, value in definitions.items()
    )
    parts = [
        '(kicad_sch (version 20250114) (generator "eeschema")'
        f' (generator_version "10.0") (uuid {q(root_uuid)}) (paper "A2")',
        '(title_block (title "Shaggy breadboard replacement")'
        ' (date "2026-10-29") (rev "A - prototype") (company "Shaggy")'
        ' (comment 1 "100 x 80 mm provisional carrier.'
        ' Verify fit before fabrication."))',
        f"(lib_symbols {embedded})",
        text("SHAGGY / CARRIER REV A", 20.32, 12.7, 3.048),
        text(
            "Pico 2 and existing offboard modules. GPIO assignments preserved.",
            20.32,
            20.32,
            1.524,
        ),
        text("Pico 2 / USB power input", 20.32, 35.56, 1.778),
        text("Hall and LCD reset buffers / non-inverting", 195.58, 35.56, 1.778),
        text("Nine sensor cables / carrier connector order", 369.57, 25.4, 1.778),
        text("3.3 V auxiliary regulator", 20.32, 173.99, 1.778),
        text("Audio cable to Adafruit TLV320DAC3100", 195.58, 173.99, 1.778),
        text("Waveshare 1.69 inch LCD", 369.57, 173.99, 1.778),
        text("SPI microSD reader", 490.22, 173.99, 1.778),
        text("Volume potentiometer", 449.58, 281.94, 1.778),
        text("SPI source termination", 20.32, 283.21, 1.778),
        text("Mounting / M3 clearance", 20.32, 332.74, 1.778),
    ]
    for component in design["components"]:
        ref = component["ref"]
        name, x, y, top = locations[ref]
        pins = libraries[name][0]
        parts.append(
            instance(
                component,
                name,
                pins,
                x,
                y,
                root_uuid,
                top,
                1.016 if ref == "JP1" else 1.27,
            )
        )
        for p in pins:
            net = component["pins"][p["number"]]
            point = x + p["x"], y - p["y"]
            key = ref + ":" + p["number"]
            if net is None:
                parts.append(
                    f"(no_connect (at {f(point[0])} {f(point[1])})"
                    f" (uuid {q(uid(key + ':nc'))}))"
                )
                continue
            right = p["angle"] == 180
            endpoint = point[0] + (5.08 if right else -5.08), point[1]
            parts.append(wire(point, endpoint, key + ":wire"))
            parts.append(net_label(net, *endpoint, 0 if right else 180, key + ":label"))

    flag = dict(
        ref="#FLG01",
        value="USB ground",
        footprint="",
        pins={"1": "GND"},
        uuid=uid("ground-flag"),
    )
    parts.append(
        instance(
            flag, "PWR_FLAG", libraries["PWR_FLAG"][0], 148.59, 196.85, root_uuid, 6.35
        )
    )
    parts.append(wire((148.59, 196.85), (148.59, 201.93), "ground-flag-wire"))
    parts.append(net_label("GND", 148.59, 201.93, 0, "ground-flag-label"))
    parts.extend(
        [
            text(
                "Fit one shunt on JP1. Default 1-2 = 3.3 V.\n"
                "Check Hall sensor model before selecting 2-3 = 5 V.",
                20.32,
                267.97,
            ),
            text(
                "Use Nexperia 74LVC244AD. "
                "Inputs tolerate 5 V and have Schmitt action.\n"
                "Unused inputs are grounded; unused outputs are marked NC.",
                195.58,
                163.83,
                1.016,
            ),
            text(
                "Speaker remains on the DAC SPK+ and SPK- outputs.\n"
                "Neither speaker wire connects to carrier GND.\n"
                "DAC I2C pullups are on the Adafruit module.",
                195.58,
                317.5,
            ),
            text(
                "RST = 10k / 100nF delay through U3 Schmitt buffer.\n"
                "R10 = 100k for the LCD TXB0108. BL stays on.\n"
                "Validate buffered power-on reset on cold starts and fast cycles.",
                369.57,
                288.29,
                1.016,
            ),
            text(
                "Carrier cable order is printed here. "
                "Trace each module's pin labels before connecting.\n"
                "Hall tile positions are firmware order; "
                "trace the physical 3 x 3 panel before labeling cables.\n"
                "Pico USB is the only power input. "
                "Measure total current with all voices and the display active.\n"
                "PICO_3V3 powers the LCD, potentiometer and buffers. "
                "AUX_3V3 powers SD and the selected Hall rail.\n"
                "U4 dissipates 0.51 W at 5 V input / 300 mA output. "
                "Verify regulator temperature inside the enclosure.\n"
                "The 100 x 80 mm outline and mounting holes are provisional. "
                "Enclosure dimensions are unmeasured.",
                20.32,
                373.38,
                1.27,
            ),
            '(sheet_instances (path "/" (page "1"))) (embedded_fonts no))',
        ]
    )
    (ROOT / "shaggy-carrier.kicad_sch").write_text("\n".join(parts) + "\n")
    print(
        f"Wrote {len(design['components'])} components with shared UUIDs"
        " to shaggy-carrier.kicad_sch"
    )


if __name__ == "__main__":
    main()
