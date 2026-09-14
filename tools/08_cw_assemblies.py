# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""08_cw_assemblies.py — Group curtain-wall panes into wall-assemblies.

The DXF is a 2D plan export; cw_panel / cw_awning blocks have no Z info.
To recover real facade areas we need pane HEIGHTS, which only exist in
the source Revit model (or in AutoCAD's 3D view in the VM).

This script produces the *measurement worklist*: it groups every curtain-wall
pane INSERT (matching the configured INCLUDE patterns) under each in-scope floor
block into ASSEMBLIES (panes sharing a wall line within LATERAL_TOL_MM laterally
and GAP_TOL_MM end-to-end). You measure each assembly's height once and feed the
heights back in via a follow-up step.

Outputs
-------
extracts/cw_assemblies.csv   machine-readable, one row per assembly
extracts/cw_assemblies.md    human worklist (sorted by n_panes desc)
"""
from __future__ import annotations
import os, sys
import csv
import math
import re
from collections import Counter, defaultdict
from math import hypot
from pathlib import Path

import ezdxf
from ezdxf.bbox import extents
from ezdxf.math import Matrix44

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

DXF = cfg.dxf_path()
EXTRACTS = cfg.extracts_dir()
ROOM_CSV = EXTRACTS / "room_inventory.csv"
OUT_DIR = EXTRACTS

# {floor label -> floor block name} — [floors.blocks].
FLOOR_BLOCKS: dict[str, str] = cfg.require(
    "floors.blocks", hint="`openhap suggest floors --write` drafts candidates")
FLOORS = list(FLOOR_BLOCKS)

LABEL_LAYER = cfg.get("layers.label", "A-AREA-IDEN")  # room-label layer (AIA NCS default)
MAX_DEPTH = cfg.get("windows.max_depth", 8)
MM_TO_M = 1e-3

# Spatial clustering tolerances (mm) — wall is "same" if lateral offset and
# end-to-end gap are within these bounds ([cw] section).
LATERAL_TOL_MM = cfg.get("cw.lateral_tol_mm", 400)
GAP_TOL_MM = cfg.get("cw.gap_tol_mm", 7500)
ANGLE_BIN_DEG = cfg.get("cw.angle_bin_deg", 15)  # 15° bins → 130-140° clusters land together as 135° wall

# Spatial join: assembly centroid → nearest room label
ROOM_RADIUS_M = cfg.get("cw.room_radius_m", 15.0)  # generous; centroids sit on wall, not inside room

# Curtain-wall patterns: the cw_* categories from [[windows.include]].
INCLUDE_PATTERNS = [(p, c) for p, c in cfg.window_includes() if c.startswith("cw_")]
if not INCLUDE_PATTERNS:
    sys.exit("No cw_* categories in [[windows.include]] — add curtain-wall "
             "patterns (`openhap suggest windows` helps) or skip the cw stage")
EXCLUDE_PATTERNS = cfg.window_excludes()

# Room-name pattern for the spatial join (generic room-type keywords,
# multilingual) — [windows].room_name_pattern overrides (include (?i) if wanted).
ROOM_NAME_RE = cfg.regex(
    "windows.room_name_pattern",
    r"(?i)^(Classroom|Office|Bureau|Bath\.?|WC|Lobby|Laboratory|Library|Stair|Reception|Corridor|Hall|Lounge)\s*\d*",
)
# ==============================================================================

# Awning dimension in name, e.g. "1000x300mm"
AWN_DIM_RE = re.compile(r"(\d{3,5})\s*[xX]\s*(\d{2,4})")


def classify(name: str) -> str | None:
    for p in EXCLUDE_PATTERNS:
        if p.search(name):
            return None
    for p, k in INCLUDE_PATTERNS:
        if p.search(name):
            return k
    return None


_block_bbox_cache: dict[str, tuple[float, float, float, float] | None] = {}


def block_local_bbox(doc, name: str):
    if name in _block_bbox_cache:
        return _block_bbox_cache[name]
    blk = doc.blocks.get(name)
    if blk is None:
        _block_bbox_cache[name] = None
        return None
    try:
        bb = extents(list(blk))
        if not bb.has_data:
            _block_bbox_cache[name] = None
            return None
        result = (
            float(bb.extmin.x), float(bb.extmin.y),
            float(bb.extmax.x), float(bb.extmax.y),
        )
    except Exception:
        result = None
    _block_bbox_cache[name] = result
    return result


def walk_cw(parent_block, doc, parent_xform: Matrix44 | None = None, depth: int = 0):
    """Yield pane dicts with world-coord bbox + orientation."""
    if parent_xform is None:
        parent_xform = Matrix44()
    for e in parent_block:
        if e.dxftype() != "INSERT":
            continue
        name = e.dxf.name
        try:
            ins_m = e.matrix44()
        except Exception:
            continue
        world_m = parent_xform @ ins_m

        cls = classify(name)
        if cls is not None:
            local_bb = block_local_bbox(doc, name)
            if local_bb is None:
                continue
            x0, y0, x1, y1 = local_bb
            corners = [(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)]
            try:
                wpts = [world_m.transform(c) for c in corners]
            except Exception:
                continue
            wxs = [float(p.x) for p in wpts]
            wys = [float(p.y) for p in wpts]
            xmin, xmax = min(wxs), max(wxs)
            ymin, ymax = min(wys), max(wys)

            # Direction of pane's local-X axis in world space
            try:
                uvec = world_m.transform_direction((1.0, 0.0, 0.0))
            except Exception:
                continue
            angle = math.degrees(math.atan2(float(uvec.y), float(uvec.x))) % 180

            yield {
                "kind": cls,
                "block_name": name,
                "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                "cx": 0.5 * (xmin + xmax),
                "cy": 0.5 * (ymin + ymax),
                "angle_deg": angle,
            }
            continue  # don't recurse into pane blocks

        if depth >= MAX_DEPTH:
            continue
        sub = doc.blocks.get(name)
        if sub is None:
            continue
        yield from walk_cw(sub, doc, world_m, depth + 1)


def load_school_rooms() -> dict[str, list[dict]]:
    rooms: dict[str, list[dict]] = {f: [] for f in FLOORS}
    with ROOM_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["floor"] not in FLOORS:
                continue
            if r["layer"] != LABEL_LAYER:
                continue
            label = r["room_label"]
            if not ROOM_NAME_RE.match(label):
                continue
            try:
                rooms[r["floor"]].append({
                    "label": label,
                    "x_m": float(r["centroid_x_m"]),
                    "y_m": float(r["centroid_y_m"]),
                })
            except (ValueError, KeyError):
                continue
    return rooms


def nearest_room(cx_m: float, cy_m: float, rooms: list[dict]):
    best, best_d = None, 1e18
    for r in rooms:
        d = hypot(cx_m - r["x_m"], cy_m - r["y_m"])
        if d < best_d:
            best_d, best = d, r
    return best, best_d


def cluster_assemblies(panes: list[dict]) -> list[dict]:
    """Cluster panes into wall assemblies, angle-aware.

    Each pane has angle_deg ∈ [0, 180). Bucket by ANGLE_BIN_DEG bins; within
    each angle bin project pane bboxes onto the wall axis (u) and the
    perpendicular (v), then cluster by lateral coord + along-axis gap.
    """
    assemblies: list[dict] = []

    # Bucket panes by angle bin
    by_bin: dict[int, list[dict]] = defaultdict(list)
    for p in panes:
        bin_key = int(round(p["angle_deg"] / ANGLE_BIN_DEG)) * ANGLE_BIN_DEG
        by_bin[bin_key % 180].append(p)

    for bin_key, bucket in by_bin.items():
        # Use the median angle within this bin to define the wall axis
        median_angle = sorted(p["angle_deg"] for p in bucket)[len(bucket) // 2]
        theta = math.radians(median_angle)
        ux, uy = math.cos(theta), math.sin(theta)   # wall direction
        vx, vy = -math.sin(theta), math.cos(theta)  # perpendicular

        # Project each pane's 4 corners onto (u, v); along = u·corner, lateral = v·corner
        for p in bucket:
            corners = [
                (p["xmin"], p["ymin"]), (p["xmax"], p["ymin"]),
                (p["xmax"], p["ymax"]), (p["xmin"], p["ymax"]),
            ]
            alongs = [cx * ux + cy * uy for (cx, cy) in corners]
            laterals = [cx * vx + cy * vy for (cx, cy) in corners]
            p["along_lo"] = min(alongs)
            p["along_hi"] = max(alongs)
            p["lateral"] = 0.25 * sum(laterals)  # average ≈ centroid·v
            p["_bin"] = bin_key
            p["_median_angle"] = median_angle

        # Sort by lateral to form lateral bins (parallel walls = same angle, different lateral)
        bucket.sort(key=lambda p: p["lateral"])
        i = 0
        while i < len(bucket):
            lat_seed = bucket[i]["lateral"]
            j = i
            while j < len(bucket) and abs(bucket[j]["lateral"] - lat_seed) < LATERAL_TOL_MM:
                j += 1
            lateral_bucket = bucket[i:j]
            lateral_bucket.sort(key=lambda p: p["along_lo"])

            # Walk along axis, grouping panes with edge-to-edge gap < GAP_TOL_MM
            current = [lateral_bucket[0]]
            for p in lateral_bucket[1:]:
                gap = p["along_lo"] - current[-1]["along_hi"]
                if gap < GAP_TOL_MM:
                    current.append(p)
                else:
                    assemblies.append(_make_assembly(current))
                    current = [p]
            assemblies.append(_make_assembly(current))
            i = j

    return assemblies


def _make_assembly(panes: list[dict]) -> dict:
    xs = [p["xmin"] for p in panes] + [p["xmax"] for p in panes]
    ys = [p["ymin"] for p in panes] + [p["ymax"] for p in panes]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    # Plan length = span along the wall axis (uses pre-computed along_lo/along_hi)
    alongs_lo = [p["along_lo"] for p in panes]
    alongs_hi = [p["along_hi"] for p in panes]
    plan_length_mm = max(alongs_hi) - min(alongs_lo)
    angle_deg = panes[0].get("_median_angle", 0.0)
    counts = Counter(p["kind"] for p in panes)

    # Awning dimensions encoded in name
    awn_dims = []
    for p in panes:
        if p["kind"] == "cw_awning":
            m = AWN_DIM_RE.search(p["block_name"])
            if m:
                awn_dims.append((int(m.group(1)), int(m.group(2))))
    awn_dim_summary = ""
    if awn_dims:
        dim_counts = Counter(awn_dims)
        parts = [f"{w}x{h}mm×{n}" for (w, h), n in dim_counts.most_common()]
        awn_dim_summary = "; ".join(parts)

    # A small sample of panel names (for cross-check in AutoCAD)
    sample_panel_names = sorted({
        p["block_name"] for p in panes if p["kind"] == "cw_panel"
    })[:3]

    return {
        "angle_deg": round(angle_deg, 1),
        "n_panes": len(panes),
        "n_panel": counts["cw_panel"],
        "n_awning": counts["cw_awning"],
        "x0_mm": x0, "y0_mm": y0, "x1_mm": x1, "y1_mm": y1,
        "cx_mm": 0.5 * (x0 + x1),
        "cy_mm": 0.5 * (y0 + y1),
        "plan_length_mm": plan_length_mm,
        "awning_dims_in_name": awn_dim_summary,
        "sample_panel_names": " | ".join(sample_panel_names),
        "_panes": panes,  # carried for pane-level CSV output
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening {DXF.name} ...")
    doc = ezdxf.readfile(str(DXF))
    print(f"  {len(list(doc.blocks))} blocks loaded\n")

    rooms_by_floor = load_school_rooms()
    for f in FLOORS:
        print(f"  {f}: {len(rooms_by_floor[f])} school-room labels")
    print()

    all_assemblies: list[dict] = []
    floor_pane_counts: dict[str, Counter] = {f: Counter() for f in FLOORS}

    for floor in FLOORS:
        bname = FLOOR_BLOCKS[floor]
        blk = doc.blocks.get(bname)
        if blk is None:
            print(f"  [skip] {bname} missing")
            continue
        print(f"[{floor}] walking {bname} ...")
        panes = list(walk_cw(blk, doc))
        angle_hist: Counter = Counter()
        for p in panes:
            floor_pane_counts[floor][p["kind"]] += 1
            angle_hist[int(round(p["angle_deg"] / ANGLE_BIN_DEG)) * ANGLE_BIN_DEG % 180] += 1
        floor_pane_counts[floor]["_angle_hist"] = angle_hist
        bin_summary = "  ".join(f"{b}°×{n}" for b, n in sorted(angle_hist.items()))
        print(
            f"  {len(panes)} panes  panel={floor_pane_counts[floor]['cw_panel']} "
            f"awning={floor_pane_counts[floor]['cw_awning']}\n"
            f"  angle bins: {bin_summary}"
        )

        assemblies = cluster_assemblies(panes)
        rooms = rooms_by_floor[floor]
        for asm_idx, a in enumerate(assemblies, start=1):
            cx_m = a["cx_mm"] * MM_TO_M
            cy_m = a["cy_mm"] * MM_TO_M
            room, dist = nearest_room(cx_m, cy_m, rooms)
            a["floor"] = floor
            a["assembly_id"] = f"{floor}-A{asm_idx:02d}"
            a["room"] = room["label"] if room and dist <= ROOM_RADIUS_M else ""
            a["room_distance_m"] = round(dist, 2) if room else ""
            a["cx_m"] = round(cx_m, 2)
            a["cy_m"] = round(cy_m, 2)
            a["plan_length_m"] = round(a["plan_length_mm"] / 1000.0, 2)
            all_assemblies.append(a)
        print(f"  → {len(assemblies)} assemblies")

    # Sort by n_panes desc so top-impact walls come first
    all_assemblies.sort(key=lambda a: -a["n_panes"])

    # CSV
    fields = [
        "assembly_id", "floor", "room", "angle_deg", "plan_length_m",
        "n_panes", "n_panel", "n_awning",
        "cx_m", "cy_m", "room_distance_m",
        "awning_dims_in_name", "sample_panel_names",
        "measure_height_mm",  # blank — user fills in
    ]
    csv_path = OUT_DIR / "cw_assemblies.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in all_assemblies:
            row = {k: a.get(k, "") for k in fields}
            w.writerow(row)

    # Pane-level CSV — one row per pane, with the assembly_id it belongs to.
    # Lets Step 09 join measured heights back to individual panes for
    # per-room facade-area computation.
    pane_fields = [
        "assembly_id", "floor", "kind", "block_name",
        "long_dim_mm", "short_dim_mm",
        "cx_m", "cy_m", "angle_deg",
        "assigned_room",  # we'll also do per-pane room assignment for accuracy
    ]
    pane_csv = OUT_DIR / "cw_panes.csv"
    rooms_by_floor_local = load_school_rooms()
    with pane_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pane_fields)
        w.writeheader()
        for a in all_assemblies:
            for p in a["_panes"]:
                long_dim = max(p["xmax"] - p["xmin"], p["ymax"] - p["ymin"])
                short_dim = min(p["xmax"] - p["xmin"], p["ymax"] - p["ymin"])
                cx_m = p["cx"] * MM_TO_M
                cy_m = p["cy"] * MM_TO_M
                room, dist = nearest_room(cx_m, cy_m, rooms_by_floor_local[a["floor"]])
                assigned = room["label"] if room and dist <= ROOM_RADIUS_M else ""
                w.writerow({
                    "assembly_id": a["assembly_id"],
                    "floor": a["floor"],
                    "kind": p["kind"],
                    "block_name": p["block_name"],
                    "long_dim_mm": round(long_dim, 1),
                    "short_dim_mm": round(short_dim, 1),
                    "cx_m": round(cx_m, 2),
                    "cy_m": round(cy_m, 2),
                    "angle_deg": round(p["angle_deg"], 1),
                    "assigned_room": assigned,
                })

    # Markdown worklist
    md = ["# Curtain-wall assemblies — measurement worklist\n"]
    md.append(
        "Each row is one curtain-wall ASSEMBLY (panes grouped by colinearity).\n"
        "**Measure the FACADE HEIGHT** of each assembly once in AutoCAD's 3D / "
        "elevation view, in millimetres. Fill the `measure_height_mm` column "
        "(or note 'ribbon' / 'full') and we'll compute corrected areas.\n\n"
        "Awning dims encoded in family name show as e.g. `1000x300mm×4`. "
        "For System Panels there's no name dimension — that's why we need a measurement.\n"
    )
    md.append("Sorted by `n_panes` descending — top rows are the highest-area walls.\n")

    md.append(
        "| asm_id | floor | room | angle° | plan_len (m) | panes | panel | awn | awning_dims | cx_m | cy_m | **height_mm** |"
    )
    md.append(
        "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|"
    )
    for a in all_assemblies:
        md.append(
            f"| {a['assembly_id']} | {a['floor']} | {a['room'] or '(none)'} | "
            f"{a['angle_deg']} | {a['plan_length_m']} | {a['n_panes']} | "
            f"{a['n_panel']} | {a['n_awning']} | {a['awning_dims_in_name'] or '—'} | "
            f"{a['cx_m']} | {a['cy_m']} | _____ |"
        )

    md.append("\n## Summary per floor\n")
    md.append("| floor | assemblies | panes | panel | awning | angle histogram |")
    md.append("|---|---:|---:|---:|---:|---|")
    for f in FLOORS:
        asms = [a for a in all_assemblies if a["floor"] == f]
        total_panes = sum(a["n_panes"] for a in asms)
        total_panel = sum(a["n_panel"] for a in asms)
        total_awning = sum(a["n_awning"] for a in asms)
        c = floor_pane_counts[f]
        hist = c.get("_angle_hist", Counter())
        hist_s = ", ".join(f"{b}°:{n}" for b, n in sorted(hist.items()))
        md.append(
            f"| {f} | {len(asms)} | {total_panes} | {total_panel} | {total_awning} | {hist_s} |"
        )

    md_path = OUT_DIR / "cw_assemblies.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print("\n=== summary ===")
    for f in FLOORS:
        asms = [a for a in all_assemblies if a["floor"] == f]
        hist = floor_pane_counts[f].get("_angle_hist", Counter())
        hist_s = " ".join(f"{b}°:{n}" for b, n in sorted(hist.items()))
        print(
            f"  {f}: {len(asms)} assemblies covering {sum(a['n_panes'] for a in asms)} panes  "
            f"angles=[{hist_s}]"
        )
    print(f"\nWrote {csv_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
