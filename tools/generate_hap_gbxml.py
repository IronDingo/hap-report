#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
gbXML 6.01 exporter for HAP — emits one gbXML from a spaces register CSV.

Reads the spaces CSV (one row per room) and emits a gbXML file with full
Surface + Construction + Material definitions so HAP imports cleanly. Each room
is placed as a square box (sqrt(area) per side, FLOOR_TO_FLOOR_M tall) via
cursor-advance (no overlap, no fragment carving).

Surface classification is CSV-driven via Floor_Boundary / Ceiling_Boundary /
Wall_Construction columns. Vertical adjacency uses an ID-mirror convention
(e.g. F1-CL-01 stacks under F2-CL-01) — a second AdjacentSpaceId is emitted
when the mirror room exists.

All envelope thermal properties, site/location, geometry, and occupant gains
come from the CONFIG block below — nothing is baked in. ExtIR (0.90) and
Roughness (MediumRough) are ASHRAE Ch. 26 opaque-masonry defaults.

Input : spaces CSV via $SPACES_CSV or arg 1 (read-only; never written).
Output: $HAP_GBXML_OUT (default gbxml/building.xml).
"""

import csv, json, math, os, sys
from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring, register_namespace

NS = "http://www.gbxml.org/schema"
register_namespace("", NS)
def ns(t): return f"{{{NS}}}{t}"

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

# Spaces register CSV (the room "truth" table): $SPACES_CSV > arg 1 > [gbxml].spaces_csv.
_raw_csv = (os.environ.get("SPACES_CSV")
            or (sys.argv[1] if len(sys.argv) > 1 else "")
            or cfg.get("gbxml.spaces_csv", ""))
SPACES_CSV = Path(_raw_csv).expanduser()
if not _raw_csv or not SPACES_CSV.exists():
    sys.exit("Set [gbxml].spaces_csv in hap-project.toml (or SPACES_CSV, or pass as arg 1)")
CSV_IN = SPACES_CSV
OUT    = Path(os.environ.get("HAP_GBXML_OUT")
              or cfg.get("gbxml.out", "gbxml/building.xml")).expanduser()

# --- Building geometry ---
FLOOR_TO_FLOOR_M = cfg.get("gbxml.floor_to_floor_m")        # e.g. 3.5
STOREY_Z: dict[str, float] = cfg.get("gbxml.storey_z", {})  # {floor_label: base_elevation_m}
GAP = cfg.get("gbxml.gap_m", 2.0)  # layout gap between space rectangles (m) — cosmetic only

# --- Site / location (from the project's design conditions) ---
PROJECT_NAME  = cfg.get("project.name") or "<project>"
SITE_NAME     = cfg.get("project.site") or "<site>"
LATITUDE      = cfg.get("site.latitude")                    # decimal degrees
LONGITUDE     = cfg.get("site.longitude")
ELEVATION_M   = cfg.get("site.elevation_m")

# CAD-model azimuth (deg). [gbxml].north_azimuth, or "auto" to derive it from
# 09_north_mapping's output: gbXML CADModelAzimuth is the angle from the CAD +Y
# axis to true north, clockwise; with true north at math angle θ (CCW from +X)
# that is (90° − θ) mod 360.
NORTH_AZIMUTH = cfg.get("gbxml.north_azimuth", "auto")
if NORTH_AZIMUTH == "auto":
    _nm = cfg.extracts_dir(create=False) / "north_mapping.json"
    if _nm.is_file():
        _deg = float(json.loads(_nm.read_text())["north_world_math_deg"])
        NORTH_AZIMUTH = (90.0 - _deg) % 360.0
    else:
        sys.exit('[gbxml].north_azimuth is "auto" but extracts/north_mapping.json '
                 "is missing — run the north stage first, or set the value")

# --- Envelope (the locked design values) — [[gbxml.materials/layers/constructions]] ---
# Materials: id -> (name, thickness_m, conductivity_WmK, density_kgm3, cp_JkgK)
MATERIALS: dict[str, tuple] = {
    m["id"]: (m["name"], m["thickness_m"], m["conductivity_w_mk"],
              m["density_kg_m3"], m["specific_heat_j_kgk"])
    for m in cfg.get("gbxml.materials", [])
}
# Layers: id -> [material_id, ...]
LAYERS: dict[str, list] = {L["id"]: L["materials"] for L in cfg.get("gbxml.layers", [])}
# Constructions: id -> (name, U_value_Wm2K, ext_solar_absorptance, layer_id).
# classify_* references these canonical ids — keep the keys:
#   cons-extwall, cons-intwall, cons-roof, cons-intfloor, cons-slab
CONSTRUCTIONS: dict[str, tuple] = {
    c["id"]: (c["name"], c["u_w_m2k"], c["ext_solar_abs"], c["layer"])
    for c in cfg.get("gbxml.constructions", [])
}
# Occupant heat gains (W/person) — pick per ASHRAE for the dominant activity.
PERSON_SENSIBLE_W = cfg.get("gbxml.person_sensible_w")      # e.g. 65
PERSON_LATENT_W   = cfg.get("gbxml.person_latent_w")        # e.g. 55

if (FLOOR_TO_FLOOR_M is None or not STOREY_Z or LATITUDE is None or NORTH_AZIMUTH is None
        or not MATERIALS or not LAYERS or not CONSTRUCTIONS
        or PERSON_SENSIBLE_W is None or PERSON_LATENT_W is None):
    sys.exit("Fill [gbxml] + [site] in hap-project.toml (geometry, site, "
             "materials/layers/constructions, occupant gains)")

H = FLOOR_TO_FLOOR_M
# Stacking order derived from storey elevations (for vertical adjacency).
_ordered = [f for f, _ in sorted(STOREY_Z.items(), key=lambda kv: kv[1])]
FLOORS_ABOVE = {a: b for a, b in zip(_ordered, _ordered[1:])}
FLOORS_BELOW = {b: a for a, b in zip(_ordered, _ordered[1:])}
# ==============================================================================

# ---- root element ----
root = Element(ns("gbXML"), {
    "temperatureUnit": "C", "lengthUnit": "Meters", "areaUnit": "SquareMeters",
    "volumeUnit": "CubicMeters", "useSIUnitsForResults": "true", "version": "6.01",
})

# ---- Materials (sources cited per row) ----
def material(mid, name, thickness, k, density, cp):
    m = SubElement(root, ns("Material"), id=mid)
    SubElement(m, ns("Name")).text = name
    SubElement(m, ns("Thickness"),   unit="Meters").text       = f"{thickness:.3f}"
    SubElement(m, ns("Conductivity"),unit="WPerMeterK").text   = f"{k:.3f}"
    SubElement(m, ns("Density"),     unit="KgPerCubicM").text  = f"{density:.0f}"
    SubElement(m, ns("SpecificHeat"),unit="JPerKgK").text      = f"{cp:.0f}"

for _mid, _args in MATERIALS.items():
    material(_mid, *_args)

# ---- Layers (1 material per layer; ground-floor slab single-layer, adiabatic — HAP applies ground BC) ----
def layer(lid, *material_refs):
    L = SubElement(root, ns("Layer"), id=lid)
    for mr in material_refs:
        SubElement(L, ns("MaterialId"), materialIdRef=mr)

for _lid, _mats in LAYERS.items():
    layer(_lid, *_mats)

# ---- Constructions (U air-to-air; ExtIR/Roughness = ASHRAE Ch. 26 opaque-masonry defaults) ----
def construction(cid, name, u_value, ext_solar_abs, layer_ref):
    c = SubElement(root, ns("Construction"), id=cid)
    SubElement(c, ns("Name")).text = name
    SubElement(c, ns("U-value"), unit="WPerSquareMeterK").text = f"{u_value:.2f}"
    SubElement(c, ns("Absorptance"), unit="Fraction", type="ExtIR").text    = "0.90"
    SubElement(c, ns("Absorptance"), unit="Fraction", type="ExtSolar").text = f"{ext_solar_abs:.3f}"
    SubElement(c, ns("Roughness"), value="MediumRough")
    SubElement(c, ns("LayerId"), layerIdRef=layer_ref)

for _cid, _args in CONSTRUCTIONS.items():
    construction(_cid, *_args)

# ---- Campus / Location / Building ----
campus = SubElement(root, ns("Campus"), id="campus")
SubElement(campus, ns("Name")).text = f"{PROJECT_NAME} Campus"
loc = SubElement(campus, ns("Location"))
SubElement(loc, ns("Name")).text             = SITE_NAME
SubElement(loc, ns("Latitude")).text         = f"{LATITUDE}"
SubElement(loc, ns("Longitude")).text        = f"{LONGITUDE}"
SubElement(loc, ns("Elevation")).text        = f"{ELEVATION_M}"
SubElement(loc, ns("CADModelAzimuth")).text  = f"{NORTH_AZIMUTH}"

building = SubElement(campus, ns("Building"), id="bldg", buildingType="School")
SubElement(building, ns("Name")).text          = f"{PROJECT_NAME} Building"
SubElement(building, ns("StreetAddress")).text = SITE_NAME

for fl, z in STOREY_Z.items():
    storey = SubElement(building, ns("BuildingStorey"), id=f"storey-{fl}")
    SubElement(storey, ns("Name")).text  = fl
    SubElement(storey, ns("Level")).text = f"{z:.2f}"

# ---- helpers ----
def cart_point(parent, x, y, z):
    cp = SubElement(parent, ns("CartesianPoint"))
    for c in (x, y, z): SubElement(cp, ns("Coordinate")).text = f"{c:.3f}"

def planar_polygon(parent, verts):
    pg = SubElement(parent, ns("PlanarGeometry"))
    pl = SubElement(pg, ns("PolyLoop"))
    for v in verts: cart_point(pl, *v)

def shell_polyloop(closed_shell, verts):
    pl = SubElement(closed_shell, ns("PolyLoop"))
    for v in verts: cart_point(pl, *v)

# ---- CSV preload + surface classification ----
ALL_ROWS = list(csv.DictReader(open(CSV_IN)))
ALL_NAMES_BY_FLOOR = {fl: {r["HAP Space Name"] for r in ALL_ROWS if r["Floor"] == fl} for fl in STOREY_Z}

def vertical_neighbor(space_name, fl, direction):
    """ID-mirror convention: F1-CL-01 stacks under F2-CL-01, etc.
    Returns neighbor name if it exists in CSV, else None — caller falls back to single-adj."""
    target_fl = FLOORS_ABOVE.get(fl) if direction == "above" else FLOORS_BELOW.get(fl)
    if not target_fl or "-" not in space_name:
        return None
    candidate = f"{target_fl}-{space_name.split('-', 1)[1]}"
    return candidate if candidate in ALL_NAMES_BY_FLOOR.get(target_fl, set()) else None

def classify_floor(sp):
    fl, name = sp["Floor"], sp["HAP Space Name"]
    fb_raw  = (sp.get("Floor_Boundary") or "").strip()
    fb      = fb_raw.lower()
    if "slab" in fb:
        return ("SlabOnGrade", "cons-slab", None, "Slab-on-Grade")
    if "adjacent conditioned" in fb:
        adj = vertical_neighbor(name, fl, "below")
        descr = f"Interior, above {adj}" if adj else f"Interior, above conditioned [CSV: {fb_raw}]"
        return ("InteriorFloor", "cons-intfloor", adj, descr)
    if "adjacent unconditioned" in fb:
        return ("InteriorFloor", "cons-intfloor", None, f"Interior, above unconditioned [CSV: {fb_raw}]")
    return ("InteriorFloor", "cons-intfloor", None, f"Interior [CSV: {fb_raw or 'unspecified'}]")

def classify_ceiling(sp):
    fl, name = sp["Floor"], sp["HAP Space Name"]
    cb_raw  = (sp.get("Ceiling_Boundary") or "").strip()
    cb      = cb_raw.lower()
    if "roof" in cb:
        return ("Roof", "cons-roof", None, "Roof")
    if "adjacent unconditioned" in cb:
        return ("InteriorFloor", "cons-intfloor", None,
                f"Interior, under unconditioned space [CSV: {cb_raw}]")
    if "adjacent conditioned" in cb:
        adj = vertical_neighbor(name, fl, "above")
        descr = f"Interior, under {adj}" if adj else f"Interior, under conditioned [CSV: {cb_raw}]"
        return ("InteriorFloor", "cons-intfloor", adj, descr)
    return ("InteriorFloor", "cons-intfloor", None, f"Interior [CSV: {cb_raw or 'unspecified'}]")

def classify_wall(sp):
    wc_raw = (sp.get("Wall_Construction") or "").strip()
    wc     = wc_raw.lower()
    if "internal partition" in wc:
        return ("InteriorWall", "cons-intwall", "Interior partition")
    return ("ExteriorWall", "cons-extwall", "Exterior wall")

# ---- pending surface queue ----
pending_surfaces = []
def queue_surface(sid, surface_type, cons_ref, adj_space, name, verts, tilt, azimuth, adj_space_2=None):
    pending_surfaces.append(dict(
        sid=sid, surface_type=surface_type, cons_ref=cons_ref,
        adj_space=adj_space, adj_space_2=adj_space_2,
        name=name, verts=verts, tilt=tilt, azimuth=azimuth))

# ---- iterate CSV: emit Space + queue 6 Surfaces per Space ----
counters     = {f: 0.0 for f in STOREY_Z}
space_counts = {f: 0   for f in STOREY_Z}
total_area   = 0.0

for sp in ALL_ROWS:
    fl    = sp["Floor"]
    name  = sp["HAP Space Name"]
    area  = float(sp["Area_m2"])
    side  = math.sqrt(area)
    x, y  = counters[fl], 0.0
    z     = STOREY_Z[fl]
    counters[fl]     += side + GAP
    space_counts[fl] += 1
    total_area       += area

    # ---- Space element ----
    conditioned = "HeatedAndCooled" if sp["Target_Air_System"] != "NONE" else "Unconditioned"
    space = SubElement(building, ns("Space"), id=name,
                       buildingStoreyIdRef=f"storey-{fl}",
                       zoneIdRef=f"zone-{name}",
                       conditionType=conditioned)
    SubElement(space, ns("Name")).text        = name
    SubElement(space, ns("Description")).text = (sp.get("Notes") or "")[:120]
    SubElement(space, ns("Area")).text        = f"{area:.2f}"
    SubElement(space, ns("Volume")).text      = f"{area * H:.2f}"

    # internal loads — straight from CSV (the manually measured data)
    occ = int(sp["Occupants"] or 0)
    if occ > 0:
        SubElement(space, ns("PeopleNumber"), unit="NumberOfPeople").text = str(occ)
        SubElement(space, ns("PeopleHeatGain"), unit="WattPerPerson", heatGainType="Sensible").text = f"{PERSON_SENSIBLE_W:.1f}"
        SubElement(space, ns("PeopleHeatGain"), unit="WattPerPerson", heatGainType="Latent").text   = f"{PERSON_LATENT_W:.1f}"
        SubElement(space, ns("PeopleHeatGain"), unit="WattPerPerson", heatGainType="Total").text    = f"{PERSON_SENSIBLE_W + PERSON_LATENT_W:.1f}"
    lpd = float(sp["Lighting_W_per_m2"]  or 0)
    epd = float(sp["Equipment_W_per_m2"] or 0)
    if lpd > 0:
        SubElement(space, ns("LightPowerPerArea"), unit="WattPerSquareMeter").text = f"{lpd:.2f}"
    if epd > 0:
        SubElement(space, ns("EquipPowerPerArea"), unit="WattPerSquareMeter").text = f"{epd:.2f}"

    # ShellGeometry — HAP v6.2 reads Space.ShellGeometry.ClosedShell unconditionally during
    # gbXML import. Without it the importer crashes with -2146233079 ("Nullable object must
    # have a value"). Coexists with the Campus-level Surfaces.
    shell = SubElement(SubElement(space, ns("ShellGeometry"), id=f"shell-{name}", unit="Meters"),
                       ns("ClosedShell"))
    shell_polyloop(shell, [(x,      y,      z),   (x+side, y,      z),   (x+side, y+side, z),   (x,      y+side, z)])     # floor
    shell_polyloop(shell, [(x,      y,      z+H), (x,      y+side, z+H), (x+side, y+side, z+H), (x+side, y,      z+H)])   # ceiling
    shell_polyloop(shell, [(x,      y,      z),   (x+side, y,      z),   (x+side, y,      z+H), (x,      y,      z+H)])   # S
    shell_polyloop(shell, [(x+side, y,      z),   (x+side, y+side, z),   (x+side, y+side, z+H), (x+side, y,      z+H)])   # E
    shell_polyloop(shell, [(x+side, y+side, z),   (x,      y+side, z),   (x,      y+side, z+H), (x+side, y+side, z+H)])   # N
    shell_polyloop(shell, [(x,      y+side, z),   (x,      y,      z),   (x,      y,      z+H), (x,      y+side, z+H)])   # W

    # ---- 6 Surfaces per Space, queued for Campus-level emission ----
    # Surface types + constructions are driven by CSV boundary fields (see classify_* helpers).
    # ID-mirror picks a second AdjacentSpaceId for floors/ceilings between stacked conditioned spaces.

    # Floor (tilt=180, polygon wound so normal points down)
    floor_type, floor_cons, floor_adj2, floor_descr = classify_floor(sp)
    queue_surface(
        sid=f"surf-{name}-FL", surface_type=floor_type, cons_ref=floor_cons,
        adj_space=name, adj_space_2=floor_adj2,
        name=f"{name} — Floor ({floor_descr})",
        verts=[(x, y, z), (x, y+side, z), (x+side, y+side, z), (x+side, y, z)],
        tilt=180, azimuth=0)

    # Ceiling (tilt=0, polygon wound so normal points up)
    ceil_type, ceil_cons, ceil_adj2, ceil_descr = classify_ceiling(sp)
    queue_surface(
        sid=f"surf-{name}-CL", surface_type=ceil_type, cons_ref=ceil_cons,
        adj_space=name, adj_space_2=ceil_adj2,
        name=f"{name} — Ceiling ({ceil_descr})",
        verts=[(x, y, z+H), (x+side, y, z+H), (x+side, y+side, z+H), (x, y+side, z+H)],
        tilt=0, azimuth=0)

    # 4 walls — surfaceType from CSV.Wall_Construction (Internal Partition vs External)
    wall_type, wall_cons, wall_descr = classify_wall(sp)
    walls = [
        ("S", [(x,      y,      z), (x+side, y,      z), (x+side, y,      z+H), (x,      y,      z+H)], 180),
        ("E", [(x+side, y,      z), (x+side, y+side, z), (x+side, y+side, z+H), (x+side, y,      z+H)],  90),
        ("N", [(x+side, y+side, z), (x,      y+side, z), (x,      y+side, z+H), (x+side, y+side, z+H)],   0),
        ("W", [(x,      y+side, z), (x,      y,      z), (x,      y,      z+H), (x,      y+side, z+H)], 270),
    ]
    for wid, wverts, azi in walls:
        queue_surface(
            sid=f"surf-{name}-{wid}", surface_type=wall_type, cons_ref=wall_cons,
            adj_space=name,
            name=f"{name} — {wid} Wall ({wall_descr})",
            verts=wverts, tilt=90, azimuth=azi)

# ---- Emit all queued Surfaces at Campus level ----
for s in pending_surfaces:
    surf = SubElement(campus, ns("Surface"),
                      id=s["sid"], surfaceType=s["surface_type"],
                      constructionIdRef=s["cons_ref"])
    SubElement(surf, ns("Name")).text = s["name"]
    SubElement(surf, ns("AdjacentSpaceId"), spaceIdRef=s["adj_space"])
    if s.get("adj_space_2"):
        SubElement(surf, ns("AdjacentSpaceId"), spaceIdRef=s["adj_space_2"])
    planar_polygon(surf, s["verts"])
    # RectangularGeometry must be COMPLETE — Azimuth + CartesianPoint + Tilt + Height + Width.
    rg = SubElement(surf, ns("RectangularGeometry"))
    SubElement(rg, ns("Azimuth")).text = f"{s['azimuth']:.0f}"
    cart_point(rg, *s["verts"][0])                                       # origin = polygon's first vertex
    SubElement(rg, ns("Tilt")).text    = f"{s['tilt']:.0f}"
    SubElement(rg, ns("Height")).text  = f"{math.dist(s['verts'][1], s['verts'][2]):.3f}"
    SubElement(rg, ns("Width")).text   = f"{math.dist(s['verts'][0], s['verts'][1]):.3f}"

# ---- Zones at root (one per Space, same name; Space.zoneIdRef points here) ----
for sp in ALL_ROWS:
    n = sp["HAP Space Name"]
    z = SubElement(root, ns("Zone"), id=f"zone-{n}")
    SubElement(z, ns("Name")).text = n

# ---- Write ----
OUT.parent.mkdir(parents=True, exist_ok=True)
raw = tostring(root, encoding="utf-8", xml_declaration=True)
OUT.write_bytes(minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8"))

# ---- Stats ----
n_spaces   = sum(space_counts.values())
n_surfaces = len(pending_surfaces)
by_type    = {}
for s in pending_surfaces:
    by_type[s["surface_type"]] = by_type.get(s["surface_type"], 0) + 1
n_with_2adj = sum(1 for s in pending_surfaces if s.get("adj_space_2"))

print(f"Wrote {OUT}")
_fl = " ".join(f"{f}={space_counts.get(f, 0)}" for f in STOREY_Z)
print(f"  Spaces:        {n_spaces}  ({_fl})")
print(f"  Surfaces:      {n_surfaces}  (6 per space; floor + ceiling + 4 walls)")
print(f"  By type:       " + "  ".join(f"{t}={n}" for t, n in sorted(by_type.items())))
print(f"  With 2nd adj:  {n_with_2adj}  (vertical adjacency via ID-mirror)")
print(f"  Materials:     {len(MATERIALS)}   Layers: {len(LAYERS)}   Constructions: {len(CONSTRUCTIONS)}")
print(f"  Zones:         {n_spaces}")
print(f"  Total area:    {total_area:.1f} m^2")
print(f"  Row widths:    " + ", ".join(f"{f}={counters[f]:.1f}m" for f in STOREY_Z))
