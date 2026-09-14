# Tools — ezdxf extraction toolset

The Stage-2 engine for the `openHAP` skill: read a DXF, produce the numbers.
A curated set of proven scripts — the dead-end iterations, exploratory probes,
and project-glue post-processors were left out (see *Not bundled* below).

## ⚠ Read before running

**Nothing project-specific is baked in.** Per-project facts live in one
`hap-project.toml`; the tools read it through `hap_config.py`. A tool exits
immediately, naming the missing key, if a value it needs is unset — it will not
run on stale assumptions or auto-discover your drawing.

The normal driver is the **`openhap` runner** (`../openhap.py`): `openhap run <stage>`
resolves the config and launches the tool via `uv run` (skipping stages whose
outputs already exist). See the top-level README for the runner's commands. To
run a tool on its own, point it at a config:

```bash
HAP_PROJECT=/abs/path/to/hap-project.toml uv run 01_layer_audit.py
```

**Resolution order, first hit wins:** CLI arg (where accepted, e.g. the DXF
path) → environment variable → `hap-project.toml` → guard-exit. Env vars
override the file for one-off runs:

| Var | Used by | Meaning |
|---|---|---|
| `HAP_PROJECT` | all | path to the `hap-project.toml` (else walked up from CWD) |
| `DXF_PATH` | 01_layer_audit, 02_extract_geometry, 01_floor_inventory, 07d, 08, 09 | absolute path to the source DXF (or pass as arg 1) |
| `HAP_EXTRACTS` | all | output dir for extracts (default `./extracts`) |
| `SPACES_CSV` | generate_hap_gbxml | path to the spaces register CSV (or arg 1) |
| `HAP_GBXML_OUT` | generate_hap_gbxml | gbXML output path (default `gbxml/building.xml`) |

**Config keys** each tool reads (drafts for the block/window ones come from
`openhap suggest floors|windows`):

| Key | Used by | Was (old CONFIG var) |
|---|---|---|
| `[floors.blocks]` | inventory, rooms, windows, cw | `FLOORS` / `FLOOR_BLOCKS` |
| `[floors].in_scope` | seed, orient | `IN_SCOPE_FLOORS` |
| `[[windows.include]]` / `[windows].exclude` | windows, cw | `INCLUDE_PATTERNS` / `EXCLUDE_PATTERNS` |
| `[layers].room/label/prefixes` | audit, geometry, takeoff, seed | `ROOM_LAYER` / `LABEL_LAYER` / `LAYER_PREFIXES` |
| `[north].footprint_block/landmark_block/landmark_quadrant` | north | `FOOTPRINT_BLOCK` / `LANDMARK_BLOCK` / `LANDMARK_QUADRANT` |
| `[cw]`, `[seed]`, `[windows]` scalars | cw, seed, windows | clustering / radius tolerances |
| `[gbxml.*]`, `[site]`, `[project]` | gbxml | `MATERIALS`/`LAYERS`/`CONSTRUCTIONS`, site, `STOREY_Z`, gains |

`[gbxml].north_azimuth = "auto"` derives the CAD azimuth from the north stage's
`north_mapping.json` — no hand-copy.

Industry standards remain documented defaults — AIA NCS layer names (`A-AREA`,
`A-AREA-IDEN`, discipline prefixes), ASHRAE 62.1 ventilation rates, ASHRAE Ch. 26
ExtIR/Roughness. Override them per project via the keys above.

**Run with `uv run <tool>.py`** — each is PEP 723 self-contained (inline deps:
`ezdxf`, `shapely`). No venv setup needed.

## Run order

Two independent takeoff approaches plus a window/orientation pass and an export.

| # | Tool | Reads | Writes | Notes |
|---|---|---|---|---|
| **Chain A — generic core takeoff** (clean A-AREA spatial join) ||||
| A1 | `01_layer_audit.py` | DXF | `layer_audit.txt` + stdout | diagnostic: are labels TEXT or block attribs? names the floor blocks |
| A2 | `02_extract_geometry.py` | DXF | `polylines/inserts/texts.json` | mm→m via `$INSUNITS`; sweeps all layouts |
| A3 | `03_build_takeoff.py` | A2's JSON | `takeoff_rooms.csv` | point-in-polygon room↔label join |
| **Chain B — deep extraction** (floor-block walking, for nested Revit exports) ||||
| B1 | `01_floor_inventory.py` | DXF + `FLOORS` | `floor_*.json` | per floor-block bbox, entities, text, attribs |
| B2 | `04_layers_and_rooms.py` | `floor_*.json` + `FLOORS` | `room_inventory.csv`, `layers.csv` | layer catalogue + room rows |
| B3 | `05_hap_seed.py` | `room_inventory.csv` + `IN_SCOPE_FLOORS` | `hap_seed.csv` | seeds spaces w/ **ASHRAE 62.1** OA rates |
| **Windows & orientation** (solar through glazing = top load driver) ||||
| W1 | `07d_count_windows_v2.py` | DXF + `room_inventory.csv` + `FLOOR_BLOCKS` + `INCLUDE_PATTERNS` | `window_takeoff_v2.csv` | per-room window count + glass area |
| W2 | `08_cw_assemblies.py` | DXF + `room_inventory.csv` + `FLOOR_BLOCKS` | `cw_assemblies.csv/.md` | groups curtain-wall panes into facade assemblies (heights measured separately) |
| W3 | `09_north_mapping.py` | DXF + `FOOTPRINT_BLOCK` + `LANDMARK_BLOCK` + `LANDMARK_QUADRANT` | `north_mapping.json` | world↔compass rotation (drives all solar) |
| W4 | `10_window_orientation.py` | `window_takeoff_v2.csv` + `north_mapping.json` | per-window/per-room compass | reads true-north from W3's output |
| **Export** ||||
| X1 | `generate_hap_gbxml.py` | `SPACES_CSV` + envelope CONFIG | gbXML 6.01 | for HAP round-trip import; needs a curated spaces register CSV |

Dependencies: **A1→A2→A3** and **B1→B2→B3** are strict chains. **W1** needs B2's
`room_inventory.csv`; **W4** needs W1 + W3. **W2, W3** are otherwise standalone.
**X1** consumes a hand-built spaces register, not the auto-extract.

## Spaces register CSV (X1 input) — expected columns

`HAP Space Name`, `Floor`, `Area_m2`, `Target_Air_System` (`NONE` = unconditioned),
`Occupants`, `Lighting_W_per_m2`, `Equipment_W_per_m2`, `Notes`,
`Floor_Boundary` / `Ceiling_Boundary` / `Wall_Construction` (drive surface
classification: keywords `slab`, `adjacent conditioned`, `adjacent unconditioned`,
`roof`, `internal partition`).

## Not bundled (and why)

- **EnergyPlus validation track** — IDF generator + EP-output parsers. Valuable
  but *secondary* (corroborating calc) and the most coupled (need a hand-built
  spaces CSV and specific EnergyPlus output filenames). Keep them with the source
  project if you run the EP convergence check.
- **Superseded iterations** — earlier window-count drafts replaced by `07d`.
- **Exploratory probes** — one-shot scripts used to discover the export had no
  room polygons.
- **Throwaway / one-off** — early extract experiments, block-name listers, a
  basement-dedup diff, PNG renderers, and a vault-coupled markdown formatter.
