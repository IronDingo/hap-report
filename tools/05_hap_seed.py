# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""05_hap_seed.py — generate a HAP space-list seed CSV for the in-scope floors.

Reads existing extracts/room_inventory.csv (no DXF re-open required).
For each real room on the in-scope floors, finds the nearest "X m²" area tag
within a tolerance distance and uses that as the area estimate.

Output → extracts/hap_seed.csv with columns:
  floor, space_name, area_m2, space_type, density_ppl_per_100m2,
  oa_cfm_per_person, oa_cfm_per_m2, notes

ASHRAE 62.1-2019 Table 6-2.1.1 ventilation rates (converted to SI).
"""
from __future__ import annotations
import os, sys
import csv
import re
from pathlib import Path
from math import hypot

# ===== CONFIG — resolved via hap_config: env > hap-project.toml =====
from hap_config import cfg

EXTRACTS = cfg.extracts_dir()
INV = EXTRACTS / "room_inventory.csv"
OUT = EXTRACTS / "hap_seed.csv"
# Floors to include in the seed (the in-scope set) — [floors].in_scope.
IN_SCOPE_FLOORS: set[str] = set(cfg.require("floors.in_scope"))
LABEL_LAYER = cfg.get("layers.label", "A-AREA-IDEN")
AREA_RE = re.compile(r"^\s*(\d{1,4})\s*m²\s*$")
# How far a room label can be from its area tag — [seed].area_match_radius_m.
AREA_MATCH_RADIUS_M = cfg.get("seed.area_match_radius_m", 8.0)
# ====================================================================


SPACE_TYPES = {
    # ASHRAE 62.1 categories, SI units
    # density_ppl_per_100m2, oa_cfm_per_person, oa_cfm_per_m2
    "classroom":   (35, 10, 0.65, "Classroom (ages 9+)"),
    "office":      (5,  5,  0.30, "Office space"),
    "lobby":       (30, 7.5,0.30, "Lobby / corridor"),
    "wc":          (0,  0,  0.0,  "Toilet — exhaust only, 50 cfm/WC fixture"),
    "bath":        (0,  0,  0.0,  "Toilet — exhaust only, 50 cfm/WC fixture"),
    "stairs":      (0,  0,  0.0,  "Stairwell — no OA needed"),
    "electrical":  (0,  0,  0.0,  "Electrical room — thermal mgmt only"),
    "mechanical":  (0,  0,  0.0,  "Mechanical room — thermal mgmt only"),
    "shaft":       (0,  0,  0.0,  "Shaft — no OA"),
    "storage":     (0,  0,  0.61, "Storage — minimum vent"),
    "lab":         (10, 10, 0.90, "Science lab"),
    "library":     (10, 5,  0.60, "Library"),
    "corridor":    (0,  0,  0.30, "Corridor — minimum vent"),
    "terrace":     (0,  0,  0.0,  "Outdoor — no HVAC"),
    "balcony":     (0,  0,  0.0,  "Outdoor — no HVAC"),
}

# Skip patterns — not real room names (room-ID tags, dim labels, grid bubbles)
SKIP_PATTERNS = [
    re.compile(r"^[A-Z]'?$"),                      # grid letters A B C D'
    re.compile(r"^\d+'?'?$"),                      # grid numbers 1, 2', 14''
    re.compile(r"^\d+\s*m²$"),                     # area tags (consumed elsewhere)
    re.compile(r"^\d+\s*[xX]\s*\d+\s*mm$"),        # door sizes 300x1700mm
    re.compile(r"^[A-Za-z0-9]{1,4}'?\s*-[A-Z]-\s*\d+$"),  # room-ID tags like "<floor>-A-023"
    re.compile(r"^[A-Z][a-z]?-\d+$"),              # Aa-201, Ad-313 number tags
    re.compile(r"^Access\s+Panel"),
    re.compile(r"^\?$"),
]


def classify(label: str) -> tuple[str, str] | None:
    """Return (space_type_key, normalized_name) or None if not a real room."""
    L = label.lower()
    if "classroom" in L or "salle de classe" in L:
        return "classroom", label
    if re.match(r"^office\s*\d", L) or L.startswith("bureau"):
        return "office", label
    if L.startswith("bath") or "toilet" in L or L == "wc" or L.startswith("wc "):
        return "wc", label
    if "stair" in L:
        return "stairs", label
    if "electric" in L:
        return "electrical", label
    if "mechanic" in L:
        return "mechanical", label
    if "storage" in L or "closet" in L:
        return "storage", label
    if "lobby" in L or "corridor" in L:
        return "lobby", label
    if "shaft" in L:
        return "shaft", label
    if "laboratory" in L or L.startswith("lab "):
        return "lab", label
    if "library" in L or "biblio" in L:
        return "library", label
    if "terrace" in L:
        return "terrace", label
    if "balcony" in L:
        return "balcony", label
    return None  # not a known room type — drop


def is_skip(label: str) -> bool:
    if not label or len(label) < 2:
        return True
    return any(p.search(label) for p in SKIP_PATTERNS)


def main() -> None:
    # Load all rows from inventory
    rows = []
    with INV.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    # Per floor, separate "rooms" from "area tags"
    rooms_by_floor: dict[str, list[dict]] = {f: [] for f in IN_SCOPE_FLOORS}
    areas_by_floor: dict[str, list[tuple[float, float, float]]] = {f: [] for f in IN_SCOPE_FLOORS}

    for r in rows:
        floor = r["floor"]
        if floor not in IN_SCOPE_FLOORS:
            continue
        if r["layer"] != LABEL_LAYER:
            continue
        label = r["room_label"]
        try:
            x = float(r["centroid_x_m"])
            y = float(r["centroid_y_m"])
        except (ValueError, KeyError):
            continue
        m = AREA_RE.match(label)
        if m:
            areas_by_floor[floor].append((x, y, float(m.group(1))))
        elif not is_skip(label):
            rooms_by_floor[floor].append({"label": label, "x": x, "y": y})

    # Match each room to nearest area tag
    out_rows = []
    for floor in sorted(IN_SCOPE_FLOORS):
        for room in rooms_by_floor[floor]:
            best = None
            best_d = 1e18
            for ax, ay, a_m2 in areas_by_floor[floor]:
                d = hypot(room["x"] - ax, room["y"] - ay)
                if d < best_d:
                    best_d, best = d, (ax, ay, a_m2)
            area_m2 = best[2] if best and best_d <= AREA_MATCH_RADIUS_M else None
            cls = classify(room["label"])
            if cls is None:
                continue
            stype_key, name = cls
            dens, oap, oam2, note = SPACE_TYPES[stype_key]
            out_rows.append({
                "floor": floor,
                "space_name": f"{floor}_{name}",
                "area_m2": f"{area_m2:.1f}" if area_m2 else "",
                "space_type": stype_key,
                "ashrae_category": note,
                "density_ppl_per_100m2": dens,
                "oa_cfm_per_person": oap,
                "oa_cfm_per_m2": oam2,
                "x_m": f"{room['x']:.2f}",
                "y_m": f"{room['y']:.2f}",
                "area_match_dist_m": f"{best_d:.2f}" if best else "",
            })

    # Write
    fields = ["floor", "space_name", "area_m2", "space_type",
              "ashrae_category", "density_ppl_per_100m2",
              "oa_cfm_per_person", "oa_cfm_per_m2",
              "x_m", "y_m", "area_match_dist_m"]
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    # Summary
    from collections import Counter
    print(f"Wrote {OUT}")
    print(f"  {len(out_rows)} school spaces total")
    print(f"  by floor: {dict(Counter(r['floor'] for r in out_rows))}")
    print(f"  by type:  {dict(Counter(r['space_type'] for r in out_rows))}")
    with_area = sum(1 for r in out_rows if r['area_m2'])
    print(f"  with matched area: {with_area} / {len(out_rows)}")


if __name__ == "__main__":
    main()
