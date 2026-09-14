# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf", "shapely"]
# ///
"""
02_extract_geometry.py — pull all useful geometry from the DXF.

Extracts:
  - Polylines (LWPOLYLINE + 2D POLYLINE) → polylines.json
  - Block inserts with attributes (INSERT)  → inserts.json
  - Text labels (TEXT + MTEXT)              → texts.json

Filters to architectural / structural / electrical layers (prefix-based)
to keep the output sane. Coordinates are converted to meters using the
DXF's $INSUNITS header.

Sweeps modelspace AND every paper-space layout, tagging each entity with
the layout it came from so the takeoff script can group by floor/pavilion.
"""

from pathlib import Path
import os, sys
import json
import ezdxf
from shapely.geometry import Polygon as SPoly, LineString

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

DXF = cfg.dxf_path()
OUT_DIR = cfg.extracts_dir()

# Keep only layers that start with one of these — drops annotation noise.
# (AIA NCS discipline-prefix default; [layers].prefixes overrides.)
LAYER_PREFIXES = tuple(cfg.get("layers.prefixes", ["A-", "S-", "E-", "P-", "C-"]))
# ==============================================================================

# INSUNITS → metres conversion factor
UNIT_TO_M = {0: 0.001, 1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}


def keep(layer: str) -> bool:
    return layer.startswith(LAYER_PREFIXES)


def measure(pts, closed):
    """Return (area_m2, length_m) computed via shapely. ezdxf LWPolyline has
    no .area/.length attribute in 1.4.x, so we recompute geometry-side."""
    if len(pts) < 2:
        return None, None
    if closed and len(pts) >= 3:
        poly = SPoly(pts)
        if poly.is_valid:
            return poly.area, poly.length
        return None, None
    return None, LineString(pts).length


def extract(space, layout_name, scale):
    polylines, inserts, texts = [], [], []

    for pl in space.query("LWPOLYLINE"):
        if not keep(pl.dxf.layer):
            continue
        pts = [(p[0] * scale, p[1] * scale) for p in pl.get_points("xy")]
        closed = bool(pl.closed)
        area_m2, length_m = measure(pts, closed)
        polylines.append({
            "layout": layout_name,
            "layer": pl.dxf.layer,
            "closed": closed,
            "vertices": pts,
            "area_m2": area_m2,
            "length_m": length_m,
        })

    for pl in space.query("POLYLINE"):
        if not getattr(pl, "is_2d_polyline", False):
            continue
        if not keep(pl.dxf.layer):
            continue
        pts = [(v.dxf.location.x * scale, v.dxf.location.y * scale) for v in pl.vertices]
        closed = bool(pl.is_closed)
        area_m2, length_m = measure(pts, closed)
        polylines.append({
            "layout": layout_name,
            "layer": pl.dxf.layer,
            "closed": closed,
            "vertices": pts,
            "area_m2": area_m2,
            "length_m": length_m,
        })

    for ins in space.query("INSERT"):
        if not keep(ins.dxf.layer):
            continue
        inserts.append({
            "layout": layout_name,
            "layer": ins.dxf.layer,
            "block": ins.dxf.name,
            "x": ins.dxf.insert.x * scale,
            "y": ins.dxf.insert.y * scale,
            "rotation": ins.dxf.rotation,
            "xscale": ins.dxf.xscale,
            "yscale": ins.dxf.yscale,
            "attribs": {att.dxf.tag: att.dxf.text for att in ins.attribs},
        })

    for t in space.query("TEXT MTEXT"):
        if not keep(t.dxf.layer):
            continue
        txt = t.dxf.text if t.dxftype() == "TEXT" else t.plain_text()
        texts.append({
            "layout": layout_name,
            "layer": t.dxf.layer,
            "type": t.dxftype(),
            "x": t.dxf.insert.x * scale,
            "y": t.dxf.insert.y * scale,
            "text": txt,
        })

    return polylines, inserts, texts


def main():
    print(f"Loading {DXF.name} ...")
    doc = ezdxf.readfile(str(DXF))
    units = doc.header.get("$INSUNITS", 4)
    scale = UNIT_TO_M.get(units, 0.001)
    print(f"  units={units} → scale_to_m={scale}")

    all_polylines, all_inserts, all_texts = [], [], []

    msp = doc.modelspace()
    p, i, t = extract(msp, "Model", scale)
    all_polylines += p; all_inserts += i; all_texts += t
    print(f"  Modelspace: {len(p)} polylines, {len(i)} inserts, {len(t)} texts")

    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        p, i, t = extract(layout, layout.name, scale)
        all_polylines += p; all_inserts += i; all_texts += t
        print(f"  Layout '{layout.name}': {len(p)} polylines, {len(i)} inserts, {len(t)} texts")

    (OUT_DIR / "polylines.json").write_text(json.dumps(all_polylines))
    (OUT_DIR / "inserts.json").write_text(json.dumps(all_inserts))
    (OUT_DIR / "texts.json").write_text(json.dumps(all_texts))

    print()
    print(f"Total: {len(all_polylines)} polylines, {len(all_inserts)} inserts, {len(all_texts)} texts")
    print(f"Written to {OUT_DIR}/{{polylines,inserts,texts}}.json")


if __name__ == "__main__":
    main()
