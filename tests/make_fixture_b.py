# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""make_fixture_b.py — known-answer DXF for chain B, windows, north, orient.

These tools read differently from chain A: they walk a *floor block definition*
(not modelspace), assume millimetres, and get window positions from INSERT
transforms + window sizes from the block NAME. So this fixture builds:

  FLOOR_L1 block  — room-name + area-tag TEXT on A-AREA-IDEN, plus window INSERTs
  FOOTPRINT block — a 40×20 m rectangle (long axis = world X), centre at origin
  SITE block      — one "court" INSERT (matches the landmark regex) at (-10,+8) m
  court block     — a small marker

All answers below are fixed by construction (arithmetic / geometry), independent
of the pipeline code. Usage: `uv run make_fixture_b.py [out.dxf]`.
"""
from __future__ import annotations
import sys
from pathlib import Path
import ezdxf

LABEL_LAYER = "A-AREA-IDEN"

# (label, area_m2, x_mm, y_mm) — 16 m apart so each matches its own area tag.
ROOMS = [
    ("Classroom 1", 40, 4000, 2500),
    ("Office 2",    24, 20000, 2500),
    ("WC 3",         6, 36000, 2500),
    ("Laboratory 4", 60, 52000, 2500),
]
# ASHRAE 62.1 rates the seed tool should assign (density/100m2, cfm/person, cfm/m2).
EXPECT_SEED = {
    "Classroom 1":  ("classroom", 40.0, 35, 10, 0.65),
    "Office 2":     ("office",    24.0,  5,  5, 0.30),
    "WC 3":         ("wc",         6.0,  0,  0, 0.0),
    "Laboratory 4": ("lab",       60.0, 10, 10, 0.90),
}

# (block_name, x_mm, y_mm, rotation_deg, near_room) — size is encoded in the name.
WINDOWS = [
    ("WIN_SLIDE_1500x1200", 4200, 2500,  0, "Classroom 1"),   # 1.8 m², faces N
    ("WIN_SLIDE_1500x1200", 4000, 2800,  0, "Classroom 1"),   # 1.8 m², faces N
    ("WIN_HINGE_1000x1200", 20200, 2500, 90, "Office 2"),     # 1.2 m², faces W
]
# north = +Y (math 90°) because the landmark sits NW of the footprint centre and
# we declare its quadrant [N, W]; a rotation-0 window (outward face +Y) then reads
# compass "N", a rotation-90 window reads "W".
EXPECT_NORTH_MATH_DEG = 90.0
EXPECT_NORTH_LABEL = "+Y"
EXPECT_WINDOW = {  # per window index: (class, area_m2, room, compass)
    0: ("slide", 1.8, "Classroom 1", "N"),
    1: ("slide", 1.8, "Classroom 1", "N"),
    2: ("hinge", 1.2, "Office 2",    "W"),
}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture_b.dxf").expanduser()
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4  # mm
    doc.layers.add(LABEL_LAYER)

    # window family blocks (geometry irrelevant for slide/hinge — size is in name)
    for wname in {w[0] for w in WINDOWS}:
        wb = doc.blocks.new(wname)
        wb.add_line((0, 0), (100, 0))

    # floor block: room texts + window inserts
    fl = doc.blocks.new("FLOOR_L1")
    for label, area, x, y in ROOMS:
        fl.add_text(label, dxfattribs={"layer": LABEL_LAYER, "height": 250}
                    ).set_placement((x, y))
        fl.add_text(f"{area} m²", dxfattribs={"layer": LABEL_LAYER, "height": 250}
                    ).set_placement((x, y - 200))
    for wname, x, y, rot, _room in WINDOWS:
        fl.add_blockref(wname, (x, y), dxfattribs={"rotation": rot})

    # footprint: 40 m × 20 m rectangle centred at origin (long axis world X)
    fp = doc.blocks.new("FOOTPRINT")
    fp.add_lwpolyline([(-20000, -10000), (20000, -10000),
                       (20000, 10000), (-20000, 10000)], close=True)

    # landmark: a "court" marker NW of centre, inside a SITE block
    court = doc.blocks.new("court")
    court.add_circle((0, 0), 200)
    site = doc.blocks.new("SITE")
    site.add_blockref("court", (-10000, 8000))

    msp = doc.modelspace()
    for b in ("FLOOR_L1", "FOOTPRINT", "SITE"):
        msp.add_blockref(b, (0, 0))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)

    print(f"Wrote {out}  ($INSUNITS=4 mm)")
    print("Expected seed (space_type, area, dens/100m2, cfm/person, cfm/m2):")
    for name, e in EXPECT_SEED.items():
        print(f"  {name:<14} {e}")
    print(f"Expected north: math {EXPECT_NORTH_MATH_DEG}° label {EXPECT_NORTH_LABEL}")
    print("Expected windows (class, area, room, compass):")
    for i, e in EXPECT_WINDOW.items():
        print(f"  win {i}: {e}")


if __name__ == "__main__":
    main()
