import argparse
import csv
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pcbnew as p
import wx

ROOT = Path(__file__).resolve().parent
LIB = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")
BOARD = ROOT / "shaggy-carrier.kicad_pcb"
DATA = json.loads((ROOT / "design.json").read_text())
app = wx.App(False)


def point(x: float, y: float) -> p.VECTOR2I:
    return p.VECTOR2I(p.FromMM(50 + x), p.FromMM(50 + y))


def text(
    board: p.BOARD,
    value: str,
    x: float,
    y: float,
    size: float = 1,
    layer: int = p.F_SilkS,
) -> None:
    item = p.PCB_TEXT(board)
    item.SetText(value)
    item.SetPosition(point(x, y))
    item.SetTextSize(p.VECTOR2I(p.FromMM(size), p.FromMM(size)))
    item.SetTextThickness(p.FromMM(max(0.12, size * 0.14)))
    item.SetLayer(layer)
    if layer == p.B_SilkS:
        item.SetMirrored(True)
    board.Add(item)


def outline(board: p.BOARD) -> None:
    r, w, h = 3, DATA["width"], DATA["height"]
    for start, end in [
        ((r, 0), (w - r, 0)),
        ((w, r), (w, h - r)),
        ((w - r, h), (r, h)),
        ((0, h - r), (0, r)),
    ]:
        item = p.PCB_SHAPE(board)
        item.SetShape(p.SHAPE_T_SEGMENT)
        item.SetStart(point(*start))
        item.SetEnd(point(*end))
        item.SetWidth(p.FromMM(0.05))
        item.SetLayer(p.Edge_Cuts)
        board.Add(item)
    for start, mid, end in [
        ((0, r), (0.87868, 0.87868), (r, 0)),
        ((w - r, 0), (w - 0.87868, 0.87868), (w, r)),
        ((w, h - r), (w - 0.87868, h - 0.87868), (w - r, h)),
        ((r, h), (0.87868, h - 0.87868), (0, h - r)),
    ]:
        item = p.PCB_SHAPE(board)
        item.SetShape(p.SHAPE_T_ARC)
        item.SetArcGeometry(point(*start), point(*mid), point(*end))
        item.SetWidth(p.FromMM(0.05))
        item.SetLayer(p.Edge_Cuts)
        board.Add(item)


def project() -> None:
    template = LIB / "template/RaspberryPi-uHAT/RaspberryPi-uHAT.kicad_pro"
    src = json.loads(template.read_text())
    rules = src["board"]["design_settings"]["rules"]
    rules.update(
        min_clearance=0.2,
        min_copper_edge_clearance=0.4,
        min_hole_clearance=0.2,
        min_hole_to_hole=0.25,
        min_silk_clearance=0.1,
        min_track_width=0.25,
        min_via_annular_width=0.15,
        min_via_diameter=0.65,
        min_through_hole_diameter=0.3,
    )
    src["board"]["design_settings"]["drc_exclusions"] = []
    src["board"]["design_settings"]["track_widths"] = [0, 0.25, 0.4, 0.6, 0.8, 1]
    src["board"]["design_settings"]["via_dimensions"] = [dict(diameter=0.65, drill=0.3)]
    default = src["net_settings"]["classes"][0]
    default.update(track_width=0.25, clearance=0.2, via_diameter=0.65, via_drill=0.3)
    power = dict(
        default,
        name="Power",
        track_width=0.6,
        via_diameter=0.8,
        via_drill=0.4,
        priority=1,
    )
    usb = dict(power, name="USB power", track_width=1, priority=0)
    src["net_settings"]["classes"] = [default, power, usb]
    src["net_settings"]["netclass_patterns"] = [
        {"netclass": "Power", "pattern": n} for n in ["PICO_3V3", "AUX_3V3", "HALL_V"]
    ] + [{"netclass": "USB power", "pattern": "VBUS_5V"}]
    src["net_settings"]["netclass_assignments"] = {}
    src["meta"] = {"filename": "shaggy-carrier.kicad_pro", "version": 1}
    src["text_variables"] = {}
    (ROOT / "shaggy-carrier.kicad_pro").write_text(json.dumps(src, indent=2) + "\n")


def routing_rules() -> None:
    path = ROOT / "reports/unrouted.dsn"
    dsn = path.read_text()
    dsn = dsn.replace("Via[0-1]_600:300_um", "Via[0-1]_650:300_um")
    dsn = dsn.replace("(circle F.Cu 600)", "(circle F.Cu 650)")
    dsn = dsn.replace("(circle B.Cu 600)", "(circle B.Cu 650)")
    dsn = dsn.replace("(width 200)", "(width 250)")
    dsn = dsn.replace("(clearance 50 (type smd_smd))", "(clearance 200 (type smd_smd))")
    names = sorted({n for c in DATA["components"] for n in c["pins"].values() if n})
    groups = {
        "signals": [
            n for n in names if n not in ["VBUS_5V", "AUX_3V3", "PICO_3V3", "HALL_V"]
        ],
        "peripherals": ["AUX_3V3", "PICO_3V3", "HALL_V"],
        "usb_power": ["VBUS_5V"],
    }
    classes = ""
    for group, nets in groups.items():
        width = {"signals": 250, "peripherals": 600, "usb_power": 1000}[group]
        classes += (
            f"(class {group} "
            + " ".join(nets)
            + ' (circuit (use_via "Via[0-1]_650:300_um"))'
            f" (rule (width {width}) (clearance 200)))\n"
        )
    dsn, count = re.subn(r"\(class kicad_default[\s\S]+?\n    \)\n", classes, dsn)
    if count != 1:
        raise RuntimeError("Expected one DSN default netclass")
    path.write_text(dsn)


def make() -> None:
    project()
    board = p.BOARD()
    board.SetFileName(str(BOARD))
    board.SetCopperLayerCount(2)
    board.GetDesignSettings().SetBoardThickness(p.FromMM(1.6))
    board.GetDesignSettings().m_CopperEdgeClearance = p.FromMM(0.4)
    title = p.TITLE_BLOCK()
    title.SetTitle("Shaggy - Pico 2 carrier")
    title.SetRevision("A / prototype")
    title.SetDate("2026-10-29")
    board.SetTitleBlock(title)
    names = sorted({n for c in DATA["components"] for n in c["pins"].values() if n})
    nets = {}
    for name in names:
        net = p.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net
    libraries = set()
    for c in DATA["components"]:
        lib, name = c["footprint"].split(":")
        local_lib = ROOT / "footprints" / (lib + ".pretty")
        local_lib.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            LIB / "footprints" / (lib + ".pretty") / (name + ".kicad_mod"),
            local_lib / (name + ".kicad_mod"),
        )
        libraries.add(lib)
        fp = p.FootprintLoad(str(local_lib), name)
        fp.SetReference(c["ref"])
        fp.SetValue(c["value"])
        fp.SetFPIDAsString(c["footprint"])
        fp.SetPath(p.KIID_PATH("/" + DATA["root_uuid"] + "/" + c["uuid"]))
        fp.SetOrientationDegrees(c["angle"])
        fp.SetPosition(point(c["x"], c["y"]))
        fp.Value().SetVisible(False)
        fp.Reference().SetVisible(False)
        for pad in fp.Pads():
            net_name = c["pins"].get(pad.GetNumber())
            if net_name:
                pad.SetNet(nets[net_name])
            if c["ref"] == "U4" and pad.GetNumber() == "2":
                pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
        board.Add(fp)
        if c["ref"].startswith("J") and c["ref"] != "JP1":
            n = len(c["pins"])
            label = c["ref"] + " " + c["value"]

            if int(c["ref"][1:]) <= 9:
                text(board, "T" + c["ref"][1:], 3, c["y"], 0.85)
            else:
                text(board, label, c["x"] + (n - 1) * 1.25, c["y"] - 3.5, 0.85)
        elif c["ref"].startswith(("R", "C")):
            text(
                board,
                c["ref"],
                c["x"] - 2.3
                if c["ref"] == "R10"
                else c["x"] + 2.3
                if c["angle"] == 90
                else c["x"],
                c["y"] if c["angle"] == 90 else c["y"] + 1.7,
                0.8,
            )
        elif c["ref"] in ["U2", "U3"]:
            text(board, c["ref"], c["x"], c["y"], 1)
        elif c["ref"] == "U4":
            text(board, "U4 3V3 AUX", 79, 7.5, 1)
    table = "(fp_lib_table\n (version 7)\n"
    for lib in sorted(libraries):
        table += (
            f' (lib (name "{lib}")(type "KiCad")'
            f'(uri "${{KIPRJMOD}}/footprints/{lib}.pretty")'
            '(options "")(descr "Project footprint snapshot"))\n'
        )
    (ROOT / "fp-lib-table").write_text(table + ")\n")
    outline(board)
    text(board, "SHAGGY", 50, 66.5, 2.4)
    text(board, "PICO 2 / CARRIER A", 50, 70, 1)
    text(board, "USB POWER", 31, 3, 1)
    text(board, "U1  PICO 2", 53.5, 53.8, 1)
    text(board, "JP1  HALL VOLTAGE", 30, 67.5, 0.8)
    text(board, "3V3  V  5V", 26.5, 75, 0.8)
    text(board, "1:GND  2:V  3:SIG", 14, 78, 0.8)
    for ref, labels in [
        ("J10", ["3V", "G", "DI", "CK", "CS", "DC", "RS", "BL"]),
        ("J11", ["3V", "CS", "DI", "CK", "DO", "G"]),
        ("J12", ["5V", "G", "RS", "DA", "CL", "BC", "WC", "DI", "G"]),
        ("J13", ["3V", "W", "G"]),
    ]:
        c = next(c for c in DATA["components"] if c["ref"] == ref)
        for i, label in enumerate(labels):
            text(board, label, c["x"] + i * 2.5, c["y"] + 4.7, 0.8)
    text(board, "SHAGGY / REV A", 28, 18, 1.5, p.B_SilkS)
    text(board, "PROTOTYPE / VERIFY FIT", 28, 23, 1, p.B_SilkS)
    text(
        board,
        "HALL CONNECTORS\n1 GND / 2 V / 3 SIGNAL\n\n"
        "JP1: ONE SHUNT\n1-2 = 3V3\n2-3 = 5V",
        28,
        38,
        1,
        p.B_SilkS,
    )
    text(board, "SPEAKER TO DAC\nSPK+ / SPK- ONLY\nNEVER TO GND", 28, 55, 1, p.B_SilkS)
    text(board, "100 x 80 mm\n2 layers / 1.6 mm", 53, 68, 1, p.B_SilkS)
    p.SaveBoard(str(BOARD), board)
    shutil.copy2(BOARD, ROOT / "reports/placement.kicad_pcb")
    board = p.LoadBoard(str(BOARD))
    ns = board.GetDesignSettings().m_NetSettings
    for net in board.GetNetInfo().NetsByNetcode().values():
        if net.GetNetname():
            net.SetNetClass(ns.GetEffectiveNetClass(net.GetNetname()))
    if not p.ExportSpecctraDSN(board, str(ROOT / "reports/unrouted.dsn")):
        raise RuntimeError("DSN export failed")
    routing_rules()
    with (ROOT / "bom.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Reference", "Value", "Footprint", "Quantity"])
        for c in DATA["components"]:
            writer.writerow([c["ref"], c["value"], c["footprint"], 1])
    print("Created", len(DATA["components"]), "footprints and", len(nets), "nets")


def finish(session: str) -> None:
    board = p.LoadBoard(str(ROOT / "reports/placement.kicad_pcb"))
    board.SetFileName(str(BOARD))
    if session and not p.ImportSpecctraSES(board, session):
        raise RuntimeError("Routing import failed")
    for track in board.GetTracks():
        if isinstance(track, p.PCB_VIA):
            track.SetDrill(p.FromMM(0.3))
    symbols = {c["ref"]: c for c in DATA["components"]}
    for fp in board.GetFootprints():
        c = symbols[fp.GetReference()]
        fp.GetField(p.FIELD_T_DATASHEET).SetText(c.get("datasheet", ""))
    netlist = ET.parse(ROOT / "reports/schematic-netlist.xml").getroot()
    for net in netlist.findall("./nets/net"):
        name = net.attrib["name"].replace("/", "{slash}")
        if not name.startswith("unconnected-"):
            continue
        netinfo = board.FindNet(name)
        if not netinfo:
            netinfo = p.NETINFO_ITEM(board, name)
            board.Add(netinfo)
        for node in net.findall("node"):
            fp = board.FindFootprintByReference(node.attrib["ref"])
            for pad in fp.Pads():
                if pad.GetNumber() == node.attrib["pin"]:
                    pad.SetNet(netinfo)
    for layer in [p.F_Cu, p.B_Cu]:
        zone = p.ZONE(board)
        zone.SetLayer(layer)
        zone.SetNetCode(board.FindNet("GND").GetNetCode())
        zone.SetLocalClearance(p.FromMM(0.25))
        zone.SetThermalReliefGap(p.FromMM(0.25))
        zone.SetThermalReliefSpokeWidth(p.FromMM(0.35))
        zone.SetPadConnection(p.ZONE_CONNECTION_THERMAL)
        zone.SetMinThickness(p.FromMM(0.2))
        zone.SetIslandRemovalMode(p.ISLAND_REMOVAL_MODE_ALWAYS)
        poly = zone.Outline()
        poly.NewOutline()
        for x, y in [(1, 1), (99, 1), (99, 79), (1, 79)]:
            v = point(x, y)
            poly.Append(v.x, v.y)
        board.Add(zone)
    p.ZONE_FILLER(board).Fill(board.Zones())
    p.SaveBoard(str(BOARD), board)
    source = BOARD.read_text()
    stackup = """(stackup
      (layer "F.SilkS" (type "Top Silk Screen") (color "White"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (color "Blue") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51)
        (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (color "Blue") (thickness 0.01))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen") (color "White"))
      (copper_finish "ENIG") (dielectric_constraints no))"""
    if "(stackup" not in source:
        source = source.replace("(setup", "(setup\n" + stackup, 1)
    BOARD.write_text(source)
    project()
    print("Saved routed board with ground fills")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["make", "finish"])
    parser.add_argument("--session", default="")
    args = parser.parse_args()
    make() if args.action == "make" else finish(args.session)
