# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""make_fixture.py — generate a known-answer synthetic DXF for smoke-testing.

Places four rectangular rooms as closed A-AREA polylines, each with a single
room-name label on A-AREA-IDEN, in millimetres, with $INSUNITS=4 (mm).

Each room's area is fixed by geometry — width × height, computed here by plain
arithmetic, NOT by any pipeline code. So a takeoff that reproduces these numbers
is an independent cross-check (the tool uses shapely's polygon area on the
extracted vertices; this script never touches shapely).

Usage:  uv run make_fixture.py [out.dxf]     (default ./fixture.dxf)
"""
from __future__ import annotations
import sys
from pathlib import Path
import ezdxf

ROOM_LAYER = "A-AREA"
LABEL_LAYER = "A-AREA-IDEN"

# (name, x0_mm, y0_mm, width_mm, height_mm) — non-overlapping rectangles.
ROOMS = [
    ("Classroom 1",     0,    0,  8000, 5000),
    ("Office 2",    10000,    0,  6000, 4000),
    ("WC 3",        10000, 6000,  3000, 2000),
    ("Laboratory 4",    0, 7000, 10000, 6000),
]


def expected_areas() -> list[tuple[str, float]]:
    # area = w * h, mm² → m². Independent of ezdxf / shapely / the pipeline.
    return [(n, (w * h) / 1_000_000) for (n, _x, _y, w, h) in ROOMS]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture.dxf").expanduser()
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4  # millimetres
    doc.layers.add(ROOM_LAYER)
    doc.layers.add(LABEL_LAYER)
    msp = doc.modelspace()

    for name, x0, y0, w, h in ROOMS:
        pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": ROOM_LAYER})
        cx, cy = x0 + w / 2, y0 + h / 2  # label at centroid → inside the polygon
        msp.add_text(name, dxfattribs={"layer": LABEL_LAYER, "height": 250}
                     ).set_placement((cx, cy))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)
    print(f"Wrote {out}  ($INSUNITS=4 mm, {len(ROOMS)} rooms on {ROOM_LAYER})")
    print("Expected room areas (independent w × h arithmetic):")
    for name, area in expected_areas():
        print(f"  {name:<16} {area:6.2f} m²")


if __name__ == "__main__":
    main()
