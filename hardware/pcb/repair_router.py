from __future__ import annotations

import argparse
import heapq
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOARD = ROOT / "shaggy-carrier.kicad_pcb"
REPORT = ROOT / "reports"
STEP = 0.1
ORIGIN = (50.0, 50.0)


def extract() -> None:
    import pcbnew as p
    import wx

    app = wx.App(False)
    board = p.LoadBoard(str(BOARD))
    data = {"tracks": [], "pads": [], "keepouts": []}
    for item in board.GetTracks():
        via = isinstance(item, p.PCB_VIA)
        data["tracks"].append(
            {
                "uuid": item.m_Uuid.AsString(),
                "net": item.GetNetname(),
                "start": [p.ToMM(v) for v in item.GetStart()],
                "end": [p.ToMM(v) for v in item.GetEnd()],
                "width": p.ToMM(item.GetWidth(p.F_Cu) if via else item.GetWidth()),
                "layers": [0, 1] if via else [0 if item.GetLayer() == p.F_Cu else 1],
                "via": via,
            }
        )
    for fp in board.GetFootprints():
        for zone in fp.Zones():
            if zone.GetIsRuleArea() and (
                zone.GetDoNotAllowTracks() or zone.GetDoNotAllowVias()
            ):
                box = zone.GetBoundingBox()
                data["keepouts"].append(
                    [
                        p.ToMM(v)
                        for v in (
                            box.GetX(),
                            box.GetY(),
                            box.GetWidth(),
                            box.GetHeight(),
                        )
                    ]
                )
        for pad in fp.Pads():
            layers = [
                i for i, layer in enumerate([p.F_Cu, p.B_Cu]) if pad.IsOnLayer(layer)
            ]
            data["pads"].append(
                {
                    "ref": fp.GetReference(),
                    "pin": pad.GetNumber(),
                    "net": pad.GetNetname(),
                    "pos": [p.ToMM(v) for v in pad.GetPosition()],
                    "size": [p.ToMM(v) for v in pad.GetSize()],
                    "drill": [p.ToMM(v) for v in pad.GetDrillSize()],
                    "angle": pad.GetOrientationDegrees(),
                    "shape": pad.GetShape(),
                    "layers": layers,
                }
            )
    (REPORT / "repair-geometry.json").write_text(json.dumps(data))
    _ = app


def gridpoint(x: float, y: float) -> tuple[int, int]:
    return (round((x - ORIGIN[0]) / STEP), round((y - ORIGIN[1]) / STEP))


def route(net: str, width: float) -> None:
    import numpy as np

    data = json.loads((REPORT / "repair-geometry.json").read_text())
    nx, ny = 1001, 801
    grids = []
    for radius in [width / 2 + 0.23, 0.65 / 2 + 0.23]:
        blocked = np.zeros((2, ny, nx), dtype=bool)
        border = math.ceil((radius + 0.4) / STEP)
        blocked[:, :border, :] = True
        blocked[:, -border:, :] = True
        blocked[:, :, :border] = True
        blocked[:, :, -border:] = True

        def capsule(
            start: list[float], end: list[float], size: float, layers: list[int]
        ) -> None:
            a = np.array(start)
            b = np.array(end)
            r = radius + size / 2
            lo = np.floor((np.minimum(a, b) - r - ORIGIN) / STEP).astype(int)
            hi = np.ceil((np.maximum(a, b) + r - ORIGIN) / STEP).astype(int)
            x0, y0 = np.maximum(lo, [0, 0])
            x1, y1 = np.minimum(hi, [nx - 1, ny - 1])
            xx, yy = np.meshgrid(
                np.arange(x0, x1 + 1) * STEP + ORIGIN[0],
                np.arange(y0, y1 + 1) * STEP + ORIGIN[1],
            )
            dx, dy = b - a
            length = dx * dx + dy * dy
            if length:
                t = np.clip(((xx - a[0]) * dx + (yy - a[1]) * dy) / length, 0, 1)
            else:
                t = 0
            mask = (xx - a[0] - t * dx) ** 2 + (yy - a[1] - t * dy) ** 2 <= r * r
            for layer in layers:
                blocked[layer, y0 : y1 + 1, x0 : x1 + 1] |= mask

        for track in data["tracks"]:
            if track["net"] != net:
                capsule(track["start"], track["end"], track["width"], track["layers"])
        for pad in data["pads"]:
            if pad["net"] == net:
                continue
            sx, sy = pad["size"]
            if max(sx, sy) < 0.01:
                sx, sy = pad["drill"]
            if pad["shape"] == 0:
                capsule(pad["pos"], pad["pos"], max(sx, sy), pad["layers"] or [0, 1])
                continue
            angle = math.radians(pad["angle"])
            c, s = math.cos(angle), math.sin(angle)
            x, y = pad["pos"]
            rr = max(sx, sy) / 2 + radius
            x0, y0 = gridpoint(x - rr, y - rr)
            x1, y1 = gridpoint(x + rr, y + rr)
            x0, y0, x1, y1 = (
                max(0, x0 - 1),
                max(0, y0 - 1),
                min(nx - 1, x1 + 1),
                min(ny - 1, y1 + 1),
            )
            xx, yy = np.meshgrid(
                np.arange(x0, x1 + 1) * STEP + ORIGIN[0] - x,
                np.arange(y0, y1 + 1) * STEP + ORIGIN[1] - y,
            )
            xx, yy = xx * c - yy * s, xx * s + yy * c
            ex, ey = (
                np.maximum(np.abs(xx) - sx / 2, 0),
                np.maximum(np.abs(yy) - sy / 2, 0),
            )
            mask = ex * ex + ey * ey <= radius * radius
            for layer in pad["layers"] or [0, 1]:
                blocked[layer, y0 : y1 + 1, x0 : x1 + 1] |= mask
        for x, y, box_width, height in data.get("keepouts", []):
            x0, y0 = gridpoint(x - radius, y - radius)
            x1, y1 = gridpoint(x + box_width + radius, y + height + radius)
            blocked[:, max(0, y0) : min(ny, y1 + 1), max(0, x0) : min(nx, x1 + 1)] = (
                True
            )
        grids.append(blocked)
    blocked, via_blocked = grids
    starts, goals = [], []
    for pad in data["pads"]:
        if pad["net"] != net:
            continue
        x, y = pad["pos"]
        group = (
            starts if (x < 100 if net == "PICO_3V3" else pad["ref"] == "U1") else goals
        )
        for layer in pad["layers"]:
            gx, gy = gridpoint(x, y)
            if not blocked[layer, gy, gx]:
                group.append((layer, gy, gx))
    for track in data["tracks"]:
        if track["net"] != net:
            continue
        for point in [track["start"], track["end"]]:
            x, y = point
            group = starts if (x < 100 if net == "PICO_3V3" else False) else goals
            for layer in track["layers"]:
                gx, gy = gridpoint(x, y)
                if not blocked[layer, gy, gx]:
                    group.append((layer, gy, gx))
    goalset = set(goals)
    gy0, gy1 = min(g[1] for g in goals), max(g[1] for g in goals)
    gx0, gx1 = min(g[2] for g in goals), max(g[2] for g in goals)

    def heuristic(y: int, x: int) -> float:
        dx, dy = max(gx0 - x, x - gx1, 0), max(gy0 - y, y - gy1, 0)
        return max(dx, dy) + 0.414214 * min(dx, dy)

    costs = np.full((2, ny, nx), np.inf, dtype=np.float32)
    parents = np.full((2, ny, nx), -1, dtype=np.int32)
    heap = []
    for layer, y, x in starts:
        costs[layer, y, x] = 0
        heapq.heappush(heap, (heuristic(y, x), 0, layer, y, x))
    found = None
    visited = 0
    moves = [
        (-1, 0, 1),
        (1, 0, 1),
        (0, -1, 1),
        (0, 1, 1),
        (-1, -1, 1.414214),
        (-1, 1, 1.414214),
        (1, -1, 1.414214),
        (1, 1, 1.414214),
    ]
    while heap:
        _, cost, layer, y, x = heapq.heappop(heap)
        if cost > costs[layer, y, x] + 0.001:
            continue
        visited += 1
        if (layer, y, x) in goalset:
            found = (layer, y, x)
            break
        ident = (layer * ny + y) * nx + x
        for dy, dx, distance in moves:
            yy, xx = y + dy, x + dx
            if not (0 <= yy < ny and 0 <= xx < nx) or blocked[layer, yy, xx]:
                continue
            if dx and dy and (blocked[layer, yy, x] or blocked[layer, y, xx]):
                continue
            prior = int(parents[layer, y, x])
            bend = 0.0
            if prior >= 0:
                prior_layer, rem = divmod(prior, nx * ny)
                prior_y, prior_x = divmod(rem, nx)
                if prior_layer == layer and (y - prior_y, x - prior_x) != (dy, dx):
                    bend = 0.4
            nc = cost + distance + bend
            if nc + 0.001 < costs[layer, yy, xx]:
                costs[layer, yy, xx] = nc
                parents[layer, yy, xx] = ident
                heapq.heappush(heap, (nc + heuristic(yy, xx), nc, layer, yy, xx))
        other = 1 - layer
        if not via_blocked[:, y, x].any():
            nc = cost + 50
            if nc + 0.001 < costs[other, y, x]:
                costs[other, y, x] = nc
                parents[other, y, x] = ident
                heapq.heappush(heap, (nc + heuristic(y, x), nc, other, y, x))
    if found is None:
        raise RuntimeError(f"No path for {net}; visited {visited}")
    path = []
    current = found
    while True:
        layer, y, x = current
        path.append([layer, ORIGIN[0] + STEP * x, ORIGIN[1] + STEP * y])
        prior = int(parents[layer, y, x])
        if prior < 0:
            break
        layer, rem = divmod(prior, nx * ny)
        y, x = divmod(rem, nx)
        current = (layer, y, x)
    path.reverse()
    simplified = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = path[i - 1], path[i], path[i + 1]
        d1 = tuple(round(b[j] - a[j], 5) for j in range(3))
        d2 = tuple(round(c[j] - b[j], 5) for j in range(3))
        if d1 != d2:
            simplified.append(b)
    simplified.append(path[-1])
    (REPORT / f"repair-{net}.json").write_text(
        json.dumps({"net": net, "width": width, "path": simplified}, indent=2)
    )
    print(
        net,
        "visited",
        visited,
        "length",
        len(path),
        "segments",
        len(simplified),
        "path",
        simplified,
    )


def apply(net: str) -> None:
    import pcbnew as p
    import wx

    app = wx.App(False)
    board = p.LoadBoard(str(BOARD))
    route_data = json.loads((REPORT / f"repair-{net}.json").read_text())
    path = route_data["path"]
    netcode = board.FindNet(net).GetNetCode()
    for a, b in zip(path, path[1:]):
        start = p.VECTOR2I(p.FromMM(a[1]), p.FromMM(a[2]))
        end = p.VECTOR2I(p.FromMM(b[1]), p.FromMM(b[2]))
        if a[0] != b[0]:
            item = p.PCB_VIA(board)
            item.SetPosition(start)
            item.SetWidth(p.FromMM(0.65))
            item.SetDrill(p.FromMM(0.3))
            item.SetViaType(p.VIATYPE_THROUGH)
            item.SetLayerPair(p.F_Cu, p.B_Cu)
        else:
            item = p.PCB_TRACK(board)
            item.SetStart(start)
            item.SetEnd(end)
            item.SetLayer(p.F_Cu if a[0] == 0 else p.B_Cu)
            item.SetWidth(p.FromMM(route_data["width"]))
        item.SetNetCode(netcode)
        board.Add(item)
    p.ZONE_FILLER(board).Fill(board.Zones())
    p.SaveBoard(str(BOARD), board)
    _ = app


def fix_clearance() -> None:
    import pcbnew as p
    import wx

    app = wx.App(False)
    board = p.LoadBoard(str(BOARD))
    for item in board.GetTracks():
        if item.GetNetname() != "TILE4":
            continue
        for attr in ["Start", "End"]:
            point = getattr(item, "Get" + attr)()
            y = p.ToMM(point.y)
            if abs(y - 72.1354) < 0.0001:
                point.y += p.FromMM(0.05)
                getattr(item, "Set" + attr)(point)
    for pad in board.FindFootprintByReference("U1").Pads():
        if pad.GetNumber() == "8":
            pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
    p.ZONE_FILLER(board).Fill(board.Zones())
    p.SaveBoard(str(BOARD), board)
    _ = app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["extract", "route", "apply", "fix"])
    parser.add_argument("--net", default="")
    parser.add_argument("--width", type=float, default=0.25)
    args = parser.parse_args()
    if args.action == "extract":
        extract()
    elif args.action == "route":
        route(args.net, args.width)
    elif args.action == "apply":
        apply(args.net)
    else:
        fix_clearance()
