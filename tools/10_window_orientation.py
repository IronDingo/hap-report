# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""10_window_orientation.py — apply true-north to per-window rotations.

Reads extracts/window_takeoff_v2.csv (rotation, area, assigned_room per window)
and converts each window's modelspace rotation to a real-world compass bearing
using the true-north math angle from 09_north_mapping (north_mapping.json).

Block convention: a window block at rotation 0 has its outward face pointing
modelspace +Y (math 90°); rotation rotates this outward face CCW. Verify on a
window of known orientation before trusting the whole set.

Outputs:
  extracts/window_orientation_per_window.csv - per-window compass info
  extracts/window_orientation_per_room.csv   - per-room glass area by compass
  extracts/window_orientation_summary.md     - human-readable summary
"""
from __future__ import annotations
import os, sys, json
import csv
from pathlib import Path
from collections import defaultdict

# ===== CONFIG — resolved via hap_config: env > hap-project.toml =====
from hap_config import cfg

EXT = cfg.extracts_dir()
WIN_FILE = EXT / "window_takeoff_v2.csv"
NORTH_JSON = EXT / "north_mapping.json"
# True-north math angle (CCW from +X). Taken from 09's output by default.
if NORTH_JSON.exists():
    N_MATH_DEG = float(json.loads(NORTH_JSON.read_text())["north_world_math_deg"])
else:
    N_MATH_DEG = None
if N_MATH_DEG is None:
    sys.exit("Run 09_north_mapping first (writes north_mapping.json), or set N_MATH_DEG")
# Floors to aggregate — [floors].in_scope (empty = all floors in the input).
IN_SCOPE_FLOORS: set[str] = set(cfg.get("floors.in_scope", []))
# ====================================================================

COMPASS_LABELS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_bin(deg: float) -> str:
    """Map compass bearing (0..360°) to one of 16 labels (22.5° bins, centered on cardinals)."""
    idx = int(((deg + 11.25) % 360) / 22.5)
    return COMPASS_LABELS[idx]


def rot_to_compass(rotation_deg: float) -> tuple[float, str]:
    """Block rotation → (compass_bearing_deg, label)."""
    outward_math = (90.0 + rotation_deg) % 360.0
    compass = (N_MATH_DEG - outward_math) % 360.0
    return compass, compass_bin(compass)


def main():
    rows = []
    n_orphan = 0
    with WIN_FILE.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rot = float(r["rotation_deg"])
                area = float(r["area_m2"])
            except (ValueError, KeyError):
                continue
            compass_deg, compass_label = rot_to_compass(rot)
            room = r["assigned_room"].strip()
            if not room:
                n_orphan += 1
            rows.append({
                "floor": r["floor"],
                "class": r["class"],
                "room": room,
                "rotation_deg": rot,
                "compass_deg": round(compass_deg, 1),
                "compass": compass_label,
                "area_m2": area,
                "x_m": float(r["x_m"]),
                "y_m": float(r["y_m"]),
            })

    out_per_window = EXT / "window_orientation_per_window.csv"
    with out_per_window.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["floor", "class", "room", "rotation_deg",
                                          "compass_deg", "compass", "area_m2", "x_m", "y_m"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {out_per_window} ({len(rows)} windows; {n_orphan} orphan / no room)")

    # Per-room aggregation
    per_room = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if IN_SCOPE_FLOORS and r["floor"] not in IN_SCOPE_FLOORS:
            continue
        if not r["room"]:
            continue
        per_room[(r["floor"], r["room"])][r["compass"]] += r["area_m2"]

    out_per_room = EXT / "window_orientation_per_room.csv"
    with out_per_room.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["floor", "room", "total_glass_m2"] + COMPASS_LABELS)
        for (floor, room), compass_areas in sorted(per_room.items()):
            total = sum(compass_areas.values())
            row = [floor, room, round(total, 2)]
            for d in COMPASS_LABELS:
                row.append(round(compass_areas.get(d, 0.0), 2))
            w.writerow(row)
    print(f"Wrote {out_per_room} ({len(per_room)} rooms)")

    # Per-floor summary
    floor_totals = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if IN_SCOPE_FLOORS and r["floor"] not in IN_SCOPE_FLOORS:
            continue
        floor_totals[r["floor"]][r["compass"]] += r["area_m2"]

    # Non-zero columns for cleaner table
    used_dirs = set()
    for floor in floor_totals:
        for d, v in floor_totals[floor].items():
            if v > 0:
                used_dirs.add(d)
    used_dirs_ordered = [d for d in COMPASS_LABELS if d in used_dirs]

    lines = [
        "# Window Orientation Summary",
        "",
        f"**True N at modelspace math {N_MATH_DEG:.2f}°** (from 09_north_mapping)",
        "",
        "Compass direction = direction the window's outward face points in real-world",
        "compass bearing. Block convention: outward face is +Y at rotation 0.",
        "",
        "## Total glass area per direction per floor (m²)",
        "",
        "| Floor | " + " | ".join(used_dirs_ordered) + " | Total |",
        "|---|" + "|".join(["---"] * (len(used_dirs_ordered) + 1)) + "|",
    ]
    for floor in sorted(floor_totals.keys()):
        compass_areas = floor_totals[floor]
        total = sum(compass_areas.values())
        cells = []
        for d in used_dirs_ordered:
            v = compass_areas.get(d, 0.0)
            cells.append(f"{v:.0f}" if v > 0 else "—")
        lines.append(f"| **{floor}** | " + " | ".join(cells) + f" | **{total:.0f}** |")

    lines += [
        "",
        "## Per-floor takeaways",
        "",
    ]
    for floor in sorted(floor_totals.keys()):
        compass_areas = floor_totals[floor]
        total = sum(compass_areas.values())
        sorted_dirs = sorted(compass_areas.items(), key=lambda x: -x[1])
        top3 = ", ".join(f"{d}={v:.0f} m²" for d, v in sorted_dirs[:3] if v > 0)
        lines.append(f"- **{floor}**: {total:.0f} m² total glazing — top 3: {top3}")

    lines += [
        "",
        "## Solar-load implications",
        "",
        "- **W / WSW** facades = afternoon sun → typically the worst-case cooling-load facade.",
        "- **E / ENE** facades = morning sun → spaces warm earliest in the day.",
        "- **N / S** facades (in the tropics) take high-angle midday sun; lower direct gain.",
        "",
        "The east (morning) and west (afternoon) facades catch direct sun at low solar",
        "altitude angles, which is when window solar gain peaks — the design facades for",
        "SHGC selection and shading strategy. (Adjust specifics to the site latitude.)",
    ]

    out_summary = EXT / "window_orientation_summary.md"
    out_summary.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out_summary}")

    print()
    print("=== Per-floor totals ===")
    for floor in sorted(floor_totals.keys()):
        ca = floor_totals[floor]
        total = sum(ca.values())
        max_dir = max(ca, key=ca.get) if ca else "none"
        print(f"{floor}: {total:.0f} m² glazing; peak = {max_dir} ({ca[max_dir]:.0f} m²)")


if __name__ == "__main__":
    main()
