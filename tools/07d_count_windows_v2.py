# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""07d_count_windows_v2.py — per-room window count + glass area.

For each in-scope floor block, recursively walks INSERTs and classifies each
against the configured window-family INCLUDE/EXCLUDE patterns, then spatial-joins
every window to the nearest room label (within ROOM_RADIUS_M) from
room_inventory.csv.

Dimension source per category:
  * name-encoded (slide/hinge): parsed from the block name via DIM_RE
  * geometry (cw_panel/cw_awning): computed from the block's bbox, cached.

Outputs
-------
extracts/window_takeoff_v2.csv    one row per window detected
extracts/window_summary_v2.csv    one row per (floor, room)
extracts/window_summary_v2.md     human-readable summary by floor & by room
"""
from __future__ import annotations
import os, sys
import csv
import re
from pathlib import Path
from collections import defaultdict, Counter
from math import hypot
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
ROOM_RADIUS_M = cfg.get("windows.room_radius_m", 8.0)
MAX_DEPTH = cfg.get("windows.max_depth", 8)
MM_TO_M = 1e-3

# Window-family patterns — [[windows.include]] / [windows].exclude.
# Categories drive the rollup columns; "slide"/"hinge" read dims from the block
# NAME (DIM_RE), "cw_panel"/"cw_awning" from block geometry bbox.
INCLUDE_PATTERNS = cfg.window_includes()
EXCLUDE_PATTERNS = cfg.window_excludes()

# Window dimensions often live inside the block name, e.g. "5000x1500mm".
DIM_RE = cfg.regex("windows.dim_pattern", r"(\d{2,5})\s*[xX]\s*(\d{2,5})")

# Room-name pattern for the spatial join (generic room-type keywords,
# multilingual) — [windows].room_name_pattern overrides (include (?i) if wanted).
ROOM_NAME_RE = cfg.regex(
    "windows.room_name_pattern",
    r"(?i)^(Classroom|Office|Bureau|Bath\.?|WC|Lobby|Laboratory|Library)\s*\d*",
)
# ==============================================================================


def classify(name: str) -> str | None:
    for pat in EXCLUDE_PATTERNS:
        if pat.search(name):
            return None
    for pat, label in INCLUDE_PATTERNS:
        if pat.search(name):
            return label
    return None


def parse_dims_from_name(name: str) -> tuple[int, int] | None:
    m = DIM_RE.search(name)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if not (300 <= w <= 8000 and 300 <= h <= 5000):
        return None
    return w, h


_block_bbox_cache: dict[str, tuple[float, float] | None] = {}

def block_geom_size_mm(doc, block_name: str) -> tuple[float, float] | None:
    """Compute width, height of a block's geometry in its own local coords."""
    if block_name in _block_bbox_cache:
        return _block_bbox_cache[block_name]
    blk = doc.blocks.get(block_name)
    if blk is None:
        _block_bbox_cache[block_name] = None
        return None
    try:
        ents = list(blk)
        bb = extents(ents)
        if not bb.has_data:
            _block_bbox_cache[block_name] = None
            return None
        w = bb.extmax.x - bb.extmin.x
        h = bb.extmax.y - bb.extmin.y
        result = (w, h)
    except Exception:
        result = None
    _block_bbox_cache[block_name] = result
    return result


def walk_window_inserts(parent_block, doc, parent_xform: Matrix44 | None = None,
                        depth: int = 0):
    """Yield window dicts: family, classification, world insert pt, scale, rotation."""
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
            try:
                ipt = world_m.transform((0.0, 0.0, 0.0))
                ix, iy = float(ipt.x), float(ipt.y)
            except Exception:
                continue
            yield {
                "block_name": name,
                "class": cls,
                "x_m": ix * MM_TO_M,
                "y_m": iy * MM_TO_M,
                "xscale": float(e.dxf.xscale),
                "yscale": float(e.dxf.yscale),
                "rotation_deg": float(e.dxf.rotation),
            }
            continue  # don't recurse into windows
        # not a window — recurse
        if depth >= MAX_DEPTH:
            continue
        sub = doc.blocks.get(name)
        if sub is None:
            continue
        yield from walk_window_inserts(sub, doc, world_m, depth + 1)


def load_school_rooms() -> dict[str, list[dict]]:
    rooms_by_floor: dict[str, list[dict]] = {f: [] for f in FLOORS}
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
                rooms_by_floor[r["floor"]].append({
                    "label": label,
                    "x_m": float(r["centroid_x_m"]),
                    "y_m": float(r["centroid_y_m"]),
                })
            except (ValueError, KeyError):
                continue
    return rooms_by_floor


def nearest_room(win: dict, rooms: list[dict]):
    best, best_d = None, 1e18
    for r in rooms:
        d = hypot(win["x_m"] - r["x_m"], win["y_m"] - r["y_m"])
        if d < best_d:
            best_d, best = d, r
    return best, best_d


def compute_area_m2(win: dict, doc) -> float | None:
    name = win["block_name"]
    cls = win["class"]
    if cls in ("slide", "hinge"):
        dims = parse_dims_from_name(name)
        if not dims:
            return None
        w_mm, h_mm = dims
    else:
        # cw_panel or cw_awning — read from block geometry bbox
        size = block_geom_size_mm(doc, name)
        if not size:
            return None
        w_mm, h_mm = size
    # apply INSERT scale to both axes
    area_m2 = (w_mm * win["xscale"]) * (h_mm * win["yscale"]) / 1e6
    return area_m2


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening {DXF.name} ...")
    doc = ezdxf.readfile(str(DXF))
    print(f"  {len(list(doc.blocks))} blocks loaded\n")

    rooms_by_floor = load_school_rooms()
    for f in FLOORS:
        print(f"  {f}: {len(rooms_by_floor[f])} school-room labels")

    all_rows: list[dict] = []
    floor_class_count: dict[str, Counter] = {f: Counter() for f in FLOORS}
    floor_class_area: dict[str, Counter] = {f: Counter() for f in FLOORS}
    floor_unassigned: dict[str, int] = {f: 0 for f in FLOORS}

    for floor in FLOORS:
        bname = FLOOR_BLOCKS[floor]
        blk = doc.blocks.get(bname)
        if blk is None:
            print(f"  [skip] {bname} missing")
            continue
        print(f"\n[{floor}] walking {bname} ...")
        windows = list(walk_window_inserts(blk, doc))
        print(f"  detected {len(windows)} window INSERTs")

        rooms = rooms_by_floor[floor]
        for w in windows:
            room, dist = nearest_room(w, rooms)
            assigned = room["label"] if room and dist <= ROOM_RADIUS_M else None
            area_m2 = compute_area_m2(w, doc)
            all_rows.append({
                "floor": floor,
                "class": w["class"],
                "block_name": w["block_name"],
                "x_m": round(w["x_m"], 2),
                "y_m": round(w["y_m"], 2),
                "xscale": round(w["xscale"], 3),
                "yscale": round(w["yscale"], 3),
                "rotation_deg": round(w["rotation_deg"], 1),
                "area_m2": round(area_m2, 3) if area_m2 else "",
                "assigned_room": assigned or "",
                "room_distance_m": round(dist, 2) if room else "",
            })
            floor_class_count[floor][w["class"]] += 1
            if area_m2:
                floor_class_area[floor][w["class"]] += area_m2
            if assigned is None:
                floor_unassigned[floor] += 1

    # Write per-window CSV
    fields = ["floor", "class", "block_name", "x_m", "y_m", "xscale", "yscale",
              "rotation_deg", "area_m2", "assigned_room", "room_distance_m"]
    with (OUT_DIR / "window_takeoff_v2.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    # Per-room rollup
    per_room: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for r in all_rows:
        if not r["assigned_room"]:
            continue
        a = float(r["area_m2"]) if r["area_m2"] != "" else 0.0
        per_room[(r["floor"], r["assigned_room"])].append((r["class"], a))

    with (OUT_DIR / "window_summary_v2.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["floor", "room", "n_slide", "n_hinge",
                    "n_cw_panel", "n_cw_awning", "n_total", "area_m2_total"])
        for (floor, room), wins in sorted(per_room.items()):
            counts = Counter(c for c, _ in wins)
            area = sum(a for _, a in wins)
            w.writerow([
                floor, room,
                counts["slide"], counts["hinge"],
                counts["cw_panel"], counts["cw_awning"],
                sum(counts.values()), f"{area:.2f}",
            ])

    # Markdown summary
    md = ["# Window takeoff — v2 (corrected patterns)\n"]
    md.append("## Per-floor summary\n")
    md.append("| floor | slide | hinge | cw_panel | cw_awning | TOTAL | total area (m²) | unassigned |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for f in FLOORS:
        c = floor_class_count[f]
        a = floor_class_area[f]
        total = sum(c.values())
        total_area = sum(a.values())
        md.append(
            f"| {f} | {c['slide']} | {c['hinge']} | "
            f"{c['cw_panel']} | {c['cw_awning']} | {total} | "
            f"{total_area:.1f} | {floor_unassigned[f]} |"
        )

    md.append("\n## Per-room rollup (school rooms only)\n")
    md.append("| floor | room | slide | hinge | cw_panel | cw_awning | total | area (m²) |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for (floor, room), wins in sorted(per_room.items()):
        counts = Counter(c for c, _ in wins)
        area = sum(a for _, a in wins)
        md.append(
            f"| {floor} | {room} | {counts['slide']} | "
            f"{counts['hinge']} | {counts['cw_panel']} | "
            f"{counts['cw_awning']} | {sum(counts.values())} | {area:.2f} |"
        )

    (OUT_DIR / "window_summary_v2.md").write_text("\n".join(md), encoding="utf-8")

    print("\n=== headline ===")
    for f in FLOORS:
        c = floor_class_count[f]
        a = floor_class_area[f]
        print(f"  {f}: slide={c['slide']}, hinge={c['hinge']}, "
              f"cw_panel={c['cw_panel']}, cw_awning={c['cw_awning']}, "
              f"total={sum(c.values())} windows, "
              f"area={sum(a.values()):.1f} m² ({floor_unassigned[f]} unassigned)")
    print(f"\nWrote window_takeoff_v2.csv, window_summary_v2.csv, window_summary_v2.md")


if __name__ == "__main__":
    main()
