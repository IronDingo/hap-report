# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""09_north_mapping.py — determine world ↔ compass rotation.

Combines two user-supplied facts (set in CONFIG):
  (a) a principal (long) axis of the FOOTPRINT_BLOCK aligns with a cardinal axis
  (b) a fixed LANDMARK sits in a known compass quadrant (LANDMARK_QUADRANT)

The landmark is located by matching LANDMARK_RE against INSERT block names inside
LANDMARK_BLOCK, which is assumed to share local origin with FOOTPRINT_BLOCK
(standard Revit-export convention — modelspace placement is just for layout).

Output:
  extracts/north_mapping.json — N/E/S/W unit vectors in world coords + bboxes.
"""
from __future__ import annotations
import os, sys
import json
import math
import re
from pathlib import Path
from collections import Counter

import ezdxf
from ezdxf.bbox import extents
from ezdxf.math import Matrix44

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

DXF = cfg.dxf_path()
OUT_DIR = cfg.extracts_dir(create=False)

# Block whose bbox defines the building footprint (long axis = a principal axis).
FOOTPRINT_BLOCK = cfg.require("north.footprint_block")
# Block to search for the orientation LANDMARK; may equal FOOTPRINT_BLOCK.
LANDMARK_BLOCK = cfg.require("north.landmark_block")
# Regex matching the landmark's block name(s) — a fixed site feature of known
# compass position (e.g. a fenced court, car-park, entrance canopy).
LANDMARK_RE = re.compile(cfg.get(
    "north.landmark_pattern", r"chainlink|fence|court|playground|canopy|parking"), re.I)
# Known compass quadrant of the landmark vs footprint center: [N|S, E|W].
LANDMARK_QUADRANT = tuple(cfg.get("north.landmark_quadrant", ["N", "W"]))
# ==============================================================================


def walk_inserts(blk, doc, parent_xform: Matrix44 | None = None,
                 depth: int = 0, max_depth: int = 8):
    if parent_xform is None:
        parent_xform = Matrix44()
    if depth > max_depth:
        return
    for e in blk:
        if e.dxftype() != "INSERT":
            continue
        try:
            ins_m = e.matrix44()
        except Exception:
            continue
        world_m = parent_xform @ ins_m
        yield (e.dxf.name, world_m)
        sub = doc.blocks.get(e.dxf.name)
        if sub is not None:
            yield from walk_inserts(sub, doc, world_m, depth + 1, max_depth)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening {DXF.name} ...")
    doc = ezdxf.readfile(str(DXF))

    # === Footprint ===
    f1_blk = doc.blocks.get(FOOTPRINT_BLOCK)
    if f1_blk is None:
        raise SystemExit(f"{FOOTPRINT_BLOCK} missing")
    f1_bb = extents(list(f1_blk))
    if not f1_bb.has_data:
        raise SystemExit(f"{FOOTPRINT_BLOCK} has no bbox data")
    fx0, fx1 = float(f1_bb.extmin.x), float(f1_bb.extmax.x)
    fy0, fy1 = float(f1_bb.extmin.y), float(f1_bb.extmax.y)
    f1_w = fx1 - fx0
    f1_h = fy1 - fy0
    f1_cx = 0.5 * (fx0 + fx1)
    f1_cy = 0.5 * (fy0 + fy1)
    print(f"\nFootprint bbox (mm):")
    print(f"  X: [{fx0:.0f}, {fx1:.0f}]  width  = {f1_w/1000:.1f} m")
    print(f"  Y: [{fy0:.0f}, {fy1:.0f}]  height = {f1_h/1000:.1f} m")
    print(f"  center: ({f1_cx:.0f}, {f1_cy:.0f}) mm")
    print(f"  aspect ratio: {max(f1_w, f1_h)/min(f1_w, f1_h):.2f}:1")

    if f1_w > f1_h:
        long_axis = "X"
        print(f"  → long axis = world X (ridgeline runs E-W in world coords)")
    else:
        long_axis = "Y"
        print(f"  → long axis = world Y (ridgeline runs N-S in world coords)")

    # === Landmark search ===
    f4_blk = doc.blocks.get(LANDMARK_BLOCK)
    if f4_blk is None:
        raise SystemExit(f"{LANDMARK_BLOCK} missing — can't find landmark")
    f4_bb = extents(list(f4_blk))
    if f4_bb.has_data:
        print(f"\nLandmark-block bbox (mm):")
        print(f"  X: [{f4_bb.extmin.x:.0f}, {f4_bb.extmax.x:.0f}]")
        print(f"  Y: [{f4_bb.extmin.y:.0f}, {f4_bb.extmax.y:.0f}]")
        # Sanity check: landmark block should sit within footprint bbox if shared origin
        f4_in_f1 = (fx0 - 5000 <= f4_bb.extmin.x and f4_bb.extmax.x <= fx1 + 5000
                    and fy0 - 5000 <= f4_bb.extmin.y and f4_bb.extmax.y <= fy1 + 5000)
        print(f"  Landmark block inside footprint? {f4_in_f1} (origin-share sanity check)")

    # Find any block name matching the configured landmark pattern
    fence_positions: list[tuple[float, float, str]] = []
    name_counter: Counter = Counter()
    for name, world_m in walk_inserts(f4_blk, doc):
        name_counter[name] += 1
        if LANDMARK_RE.search(name):
            try:
                ipt = world_m.transform((0.0, 0.0, 0.0))
                fence_positions.append((float(ipt.x), float(ipt.y), name))
            except Exception:
                continue

    print(f"\nF4 unique block names: {len(name_counter)}")
    print(f"Landmark INSERTs found: {len(fence_positions)}")
    if fence_positions:
        # Show the family names found
        fam_counter = Counter(n for _, _, n in fence_positions)
        for fam, cnt in fam_counter.most_common(5):
            print(f"  {fam} × {cnt}")

    if not fence_positions:
        print("\nNo landmark markers found. Showing top landmark-block names for diagnosis:")
        for n, c in name_counter.most_common(20):
            print(f"  {c:>4}  {n}")
        raise SystemExit(f"Cannot determine landmark position from {LANDMARK_BLOCK}")

    # Court centroid
    cxs = [p[0] for p in fence_positions]
    cys = [p[1] for p in fence_positions]
    court_cx = sum(cxs) / len(cxs)
    court_cy = sum(cys) / len(cys)
    print(f"\nLandmark centroid (world mm): ({court_cx:.0f}, {court_cy:.0f})")

    # === Direction from footprint center to landmark ===
    dx = court_cx - f1_cx
    dy = court_cy - f1_cy
    print(f"Vector footprint-center → landmark: ({dx/1000:.1f}, {dy/1000:.1f}) m")

    # === Disambiguate N direction ===
    # Test all 4 axis-aligned N candidates (don't constrain to the long axis).
    # Pick the candidate where the landmark falls in LANDMARK_QUADRANT — i.e. its
    # projections onto that candidate's N and W axes carry the expected signs
    # (W is 90° CCW from N; math angle = N_angle + 90°).
    candidates = []
    for n_vec, n_label in [
        ((+1.0, 0.0), "+X"), ((-1.0, 0.0), "-X"),
        ((0.0, +1.0), "+Y"), ((0.0, -1.0), "-Y"),
    ]:
        w_vec = (-n_vec[1], n_vec[0])
        n_comp = dx * n_vec[0] + dy * n_vec[1]
        w_comp = dx * w_vec[0] + dy * w_vec[1]
        along_long_axis = (
            (long_axis == "X" and n_vec[0] != 0)
            or (long_axis == "Y" and n_vec[1] != 0)
        )
        ns_ok = (n_comp > 0) if LANDMARK_QUADRANT[0] == "N" else (n_comp < 0)
        ew_ok = (w_comp > 0) if LANDMARK_QUADRANT[1] == "W" else (w_comp < 0)
        candidates.append({
            "n_label": n_label,
            "n_vec": n_vec,
            "w_vec": w_vec,
            "n_comp_m": n_comp / 1000,
            "w_comp_m": w_comp / 1000,
            "landmark_in_quadrant": (ns_ok and ew_ok),
            "along_long_axis": along_long_axis,
        })

    print("\nCandidate N directions (all 4 axes considered):")
    _q = "".join(LANDMARK_QUADRANT)
    for c in candidates:
        match = f"✓ LANDMARK IN {_q}" if c["landmark_in_quadrant"] else f"  (not in {_q})"
        long_note = " [along ridgeline]" if c["along_long_axis"] else " [perpendicular]"
        print(f"  N = {c['n_label']}: N_comp = {c['n_comp_m']:+6.1f} m, "
              f"W_comp = {c['w_comp_m']:+6.1f} m {match}{long_note}")

    matching = [c for c in candidates if c["landmark_in_quadrant"]]
    if len(matching) != 1:
        print("\nWARNING: zero or multiple matches — see candidate table above")
        # Best-effort: pick the one with largest combined NW projection
        matching = sorted(candidates, key=lambda c: -(c["n_comp_m"] + c["w_comp_m"]))[:1]

    chosen = matching[0]
    n_vec = chosen["n_vec"]
    w_vec = chosen["w_vec"]
    e_vec = (n_vec[1], -n_vec[0])  # 90° CW from N
    s_vec = (-n_vec[0], -n_vec[1])

    print(f"\n=== DECISION ===")
    print(f"  N = {chosen['n_label']} world axis = ({n_vec[0]:+.0f}, {n_vec[1]:+.0f})")
    print(f"  E = ({e_vec[0]:+.0f}, {e_vec[1]:+.0f})")
    print(f"  S = ({s_vec[0]:+.0f}, {s_vec[1]:+.0f})")
    print(f"  W = ({w_vec[0]:+.0f}, {w_vec[1]:+.0f})")
    if not chosen["along_long_axis"]:
        print(f"\n  NOTE: N is PERPENDICULAR to the long axis (ridgeline).")
        print(f"        Long facade ({f1_w/1000:.0f} m) faces N and S — good design for tropics.")
        print(f"        Short facade ({f1_h/1000:.0f} m) faces E and W.")

    # World math angle of N (CCW from +X in standard math convention)
    north_world_math_deg = math.degrees(math.atan2(n_vec[1], n_vec[0]))
    print(f"\n  North in world coords: {north_world_math_deg:.0f}° "
          f"(math convention, CCW from +X)")

    out = {
        "north_world": list(n_vec),
        "east_world": list(e_vec),
        "south_world": list(s_vec),
        "west_world": list(w_vec),
        "north_label_in_world": chosen["n_label"],  # "+X" / "-X" / "+Y" / "-Y"
        "north_world_math_deg": north_world_math_deg,
        "long_axis": long_axis,
        "f1_bbox_mm": [fx0, fy0, fx1, fy1],
        "f1_center_mm": [f1_cx, f1_cy],
        "f1_aspect_ratio": round(max(f1_w, f1_h) / min(f1_w, f1_h), 2),
        "court_centroid_mm": [court_cx, court_cy],
        "court_relative_to_f1_center_m": [dx / 1000, dy / 1000],
        "n_fence_inserts": len(fence_positions),
        "method": "footprint bbox principal axis + landmark centroid (landmark quadrant per CONFIG)",
    }
    out_path = OUT_DIR / "north_mapping.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")

    # Helper formulas for downstream scripts
    print(f"\n=== usage in downstream scripts ===")
    print(f"  N_world = {tuple(n_vec)}")
    print(f"  for any vector v=(vx, vy) in world mm:")
    print(f"    n_component = vx*({n_vec[0]:+.0f}) + vy*({n_vec[1]:+.0f})")
    print(f"    e_component = vx*({e_vec[0]:+.0f}) + vy*({e_vec[1]:+.0f})")
    print(f"    compass_bearing_deg = (atan2(e_comp, n_comp) * 180/pi) % 360")


if __name__ == "__main__":
    main()
