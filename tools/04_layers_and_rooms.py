# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely"]
# ///
"""04_layers_and_rooms.py — derive layer catalogue + room inventory CSV.

Inputs: extracts/floor_*.json from 01_floor_inventory.py
Outputs:
  extracts/layers.csv               - layer x floor entity counts (rolled-up)
  extracts/room_inventory.csv       - one row per identifiable room

A-AREA polygons aren't separated by floor in the inventory JSONs (the
inventory only stores text and counts). So for room inventory we rely on
the text dump: each MTEXT/TEXT that looks like a room label gets one row,
with empty area_m2 unless we later add an A-AREA polygon pass.

The script also flags whether A-AREA layers actually carry polygons on
each floor (from the inventory's per-layer entity counts).
"""
from __future__ import annotations
from pathlib import Path
import os, sys
import json
import csv
import re

# ===== CONFIG — resolved via hap_config: env > hap-project.toml =====
from hap_config import cfg

EXT_DIR = cfg.extracts_dir()
# Floor labels to roll up — the [floors.blocks] keys, matching the
# floor_*.json files written by 01_floor_inventory.
FLOORS: list[str] = list(cfg.require(
    "floors.blocks", hint="`openhap suggest floors --write` drafts candidates"))
# ====================================================================

# heuristic: a "room label" text is short-ish, not pure-numeric coordinate,
# not full of weird symbols, and not obviously a dimension. We capture
# room number tokens (e.g. "G.01", "RDC-12", "F1.07") separately.
ROOM_NUM_RE = re.compile(r"^([A-Z]{1,3}[\.\-]?\d{1,3}[A-Z]?)$")
SKIP_TEXTS = {"", "-", "--", "...", "N", "S", "E", "W"}


def looks_like_label(s: str) -> bool:
    s = s.strip()
    if not s or s in SKIP_TEXTS:
        return False
    if len(s) > 80:
        return False
    # ignore pure dimensions / coords
    if re.fullmatch(r"[-+]?\d+(?:[\.,]\d+)?", s):
        return False
    return True


def main():
    # --- layer catalogue ---
    layer_totals: dict[str, dict[str, int]] = {}  # layer -> {floor -> total entities}
    for floor in FLOORS:
        p = EXT_DIR / f"floor_{floor}.json"
        data = json.loads(p.read_text())
        for layer, type_counts in data.get("layer_counts", {}).items():
            tot = sum(type_counts.values())
            layer_totals.setdefault(layer, {})[floor] = tot

    layers_csv = EXT_DIR / "layers.csv"
    with layers_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["layer", "total"] + FLOORS)
        # sort by descending total entity count across all floors
        rows = []
        for layer, by_floor in layer_totals.items():
            total = sum(by_floor.values())
            rows.append((layer, total, by_floor))
        rows.sort(key=lambda r: -r[1])
        for layer, total, by_floor in rows:
            w.writerow([layer, total] + [by_floor.get(fl, 0) for fl in FLOORS])
    print(f"Wrote {layers_csv} ({len(rows)} layers)")

    # --- room inventory (text-driven) ---
    rooms_csv = EXT_DIR / "room_inventory.csv"
    n_rows = 0
    with rooms_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["floor", "room_label", "room_number", "area_m2",
                    "centroid_x_m", "centroid_y_m", "source_block", "layer", "text_type"])
        for floor in FLOORS:
            p = EXT_DIR / f"floor_{floor}.json"
            data = json.loads(p.read_text())
            src = data["block"]

            # group texts by layer; A-AREA-IDEN is the canonical room-label layer,
            # but Revit often dumps to plain `A-AREA` or `0`. We include everything
            # on A-AREA*, but flag layer for review.
            for t in data.get("texts", []):
                txt = (t.get("text") or "").strip()
                if not looks_like_label(txt):
                    continue
                layer = t.get("layer", "")
                # we filter aggressively to A-AREA*-style layers AND any layer
                # named like a room/identifier; fall back to capturing ALL labels
                # so the user can post-filter in LibreOffice.
                # Detect a room number embedded in the label.
                m = ROOM_NUM_RE.match(txt)
                if m:
                    room_number = m.group(1)
                    room_label = ""
                else:
                    room_number = ""
                    room_label = txt
                w.writerow([
                    floor, room_label, room_number, "",  # no area yet
                    t.get("x_m"), t.get("y_m"),
                    src, layer, t.get("type"),
                ])
                n_rows += 1
    print(f"Wrote {rooms_csv} ({n_rows} text-derived rows)")

    # --- A-AREA presence flag ---
    area_present = {}
    for floor in FLOORS:
        p = EXT_DIR / f"floor_{floor}.json"
        data = json.loads(p.read_text())
        has_area = False
        for layer, type_counts in data.get("layer_counts", {}).items():
            if "A-AREA" in layer.upper():
                if "LWPOLYLINE" in type_counts or "POLYLINE" in type_counts or "HATCH" in type_counts:
                    has_area = True
                    break
        area_present[floor] = has_area

    flag_path = EXT_DIR / "a_area_presence.json"
    flag_path.write_text(json.dumps(area_present, indent=2))
    print(f"Wrote {flag_path} -- A-AREA polygon presence per floor")
    print("  per floor:", area_present)


if __name__ == "__main__":
    main()
