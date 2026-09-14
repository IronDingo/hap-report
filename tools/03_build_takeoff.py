# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely"]
# ///
"""
03_build_takeoff.py — pair room polygons with their labels via spatial join.

Reads polylines.json + inserts.json + texts.json, finds closed polygons
on A-AREA, then for each label (either an MTEXT/TEXT on A-AREA-IDEN, or
a block insert with attributes on the same layer) checks point-in-polygon
to determine which room the label belongs to.

Output: takeoff_rooms.csv with one row per room:
  room_id, layout, name, number, area_m2, perimeter_m, centroid_x, centroid_y
"""

from pathlib import Path
import os
import csv
import json
from shapely.geometry import Polygon, Point

# ===== CONFIG — resolved via hap_config: env > hap-project.toml (NCS defaults) =====
from hap_config import cfg

EXTRACTS = cfg.extracts_dir()
ROOM_LAYER = cfg.get("layers.room", "A-AREA")         # closed room/area polygons
LABEL_LAYER = cfg.get("layers.label", "A-AREA-IDEN")  # room-name labels
OUT_CSV = EXTRACTS / "takeoff_rooms.csv"
# ==================================================================================


def label_text(item, kind):
    """Best-guess room name/number from a text entity or block insert."""
    if kind == "text":
        return item["text"].strip(), ""
    # block insert: try common attribute tag conventions
    attrs = item.get("attribs", {})
    name = (attrs.get("ROOM_NAME") or attrs.get("NAME") or attrs.get("RM_NAME")
            or attrs.get("DESCRIPTION") or "").strip()
    number = (attrs.get("ROOM_NUMBER") or attrs.get("NUMBER") or attrs.get("RM_NUM")
              or attrs.get("ROOM_NO") or "").strip()
    if not name and not number and attrs:
        # fall back to dumping whatever tag we got
        name = " / ".join(f"{k}={v}" for k, v in attrs.items())
    return name, number


def main():
    polylines = json.loads((EXTRACTS / "polylines.json").read_text())
    inserts = json.loads((EXTRACTS / "inserts.json").read_text())
    texts = json.loads((EXTRACTS / "texts.json").read_text())

    rooms = []
    for pl in polylines:
        if pl["layer"] != ROOM_LAYER or not pl["closed"]:
            continue
        if len(pl["vertices"]) < 3:
            continue
        poly = Polygon(pl["vertices"])
        if not poly.is_valid or poly.area < 0.5:  # ignore slivers (< 0.5 m²)
            continue
        rooms.append({"layout": pl["layout"], "polygon": poly,
                      "area_m2": poly.area, "perimeter_m": poly.length,
                      "centroid": poly.centroid})

    labels = []
    for t in texts:
        if t["layer"] == LABEL_LAYER:
            labels.append({"layout": t["layout"], "x": t["x"], "y": t["y"],
                           "name": t["text"].strip(), "number": "", "_kind": "text"})
    for ins in inserts:
        if ins["layer"] == LABEL_LAYER:
            name, number = label_text(ins, "insert")
            labels.append({"layout": ins["layout"], "x": ins["x"], "y": ins["y"],
                           "name": name, "number": number, "_kind": "insert"})

    room_layouts = {r["layout"] for r in rooms}
    label_layouts = {l["layout"] for l in labels}
    if rooms and labels and room_layouts.isdisjoint(label_layouts):
        print(f"WARNING: no layout overlap between rooms {room_layouts} and labels {label_layouts}")
        print("  → all matches will be zero. Geometry is probably split between modelspace and paper-space.")

    matched = 0
    for room in rooms:
        room["name"] = ""
        room["number"] = ""
        for lab in labels:
            if lab["layout"] != room["layout"]:
                continue
            # covers() includes boundary points; contains() misses them silently
            if room["polygon"].covers(Point(lab["x"], lab["y"])):
                room["name"] = lab["name"]
                room["number"] = lab["number"]
                matched += 1
                break

    rooms.sort(key=lambda r: (r["layout"], r["number"] or "", r["name"] or ""))

    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["room_id", "layout", "name", "number", "area_m2",
                    "perimeter_m", "centroid_x", "centroid_y"])
        for i, r in enumerate(rooms, 1):
            w.writerow([f"R{i:04d}", r["layout"], r["name"], r["number"],
                        f"{r['area_m2']:.2f}", f"{r['perimeter_m']:.2f}",
                        f"{r['centroid'].x:.2f}", f"{r['centroid'].y:.2f}"])

    print(f"Rooms found: {len(rooms)} (on layer {ROOM_LAYER})")
    print(f"Labels found: {len(labels)} (on layer {LABEL_LAYER})")
    print(f"Matched labels → rooms: {matched}")
    print(f"Unmatched rooms: {len(rooms) - matched}")
    print(f"Written: {OUT_CSV}")


if __name__ == "__main__":
    main()
