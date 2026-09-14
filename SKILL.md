---
name: openhap
description: >
  Reproduce HAP-equivalent cooling-load report data from a building drawing.
  Walks a 5-stage pipeline: convert DWG→DXF, run ezdxf takeoff scripts to get
  numbers, fold in the locked envelope/design values the user supplies, ask
  clarifying questions and flag missing-input risks (impact-ranked), then print
  the final structured data package for a report / HTML-designer Claude. Use for
  HVAC cooling-load report data reproduction, room takeoff from a DWG, or
  rebuilding a load-calc submission dataset. Prints a project map to orient.
---

# openHAP — HAP report data bot

Turn a building drawing + a set of locked design values into the structured
dataset a report designer needs. You **orchestrate existing tools**; you do not
invent numbers.

## Project map — print this on load and at every stage boundary

```
╔════════════════════════════════════════════════════════════════════════════╗
║  HAP REPORT DATA BOT · PROJECT MAP                                         ║
║  cooling-load dataset:  DWG ─▶ extracts ─▶ data package         <project>  ║
╠════════════════════════════════════════════════════════════════════════════╣
║  PIPELINE                                               status · artifact  ║
║                                                                            ║
║  ✔  Stage 0 · Scope & inputs                       envelope, design cond.  ║
║     │   drawing path · floors/zones · climate · standards                  ║
║  ✔  Stage 1 · DWG ─▶ DXF  (ODA, external)            skipped — DXF exists  ║
║     │   ~/Downloads/ODA-Output/<DRAWING>.dxf                               ║
║  ◐  Stage 2 · Extract  (uv run)                    extracts/ ·csv·json·md  ║
║     │   ✔ layers  ✔ geom  ◐ takeoff  ○ windows  ○ gbXML/IDF                ║
║  ▶  Stage 3 · Clarifications & risks                     ranked gap audit  ║
║     │   ◀── YOU ARE HERE          ⚠ 2 H-impact gaps open                   ║
║  ○  Stage 4 · Final data package                       dense md ─▶ report  ║
║         header·conditions·envelope·loads·vent·calc·rollup                  ║
║                                                                            ║
╠════════════════════════════════════════════════════════════════════════════╣
║  WHERE THINGS LIVE                 │ RISKS  (⚠ open)                       ║
║  ~/<project>/                      │ ⚠ H  envelope U-value unset           ║
║  ├─ README.md ···· run order       │ ⚠ H  ventilation rate guessed         ║
║  ├─ scripts/ ····· ezdxf · uv      │ ⚠ M  shading geom incomplete          ║
║  ├─ extracts/ ◐ ·· outputs         │                                       ║
║  ├─ gbxml/ ○ ····· HAP round-trip  │ resolve all ⚠H                        ║
║  └─ idf/ ○ ······· EnergyPlus      │ before Stage 4 prints                 ║
╚════════════════════════════════════════════════════════════════════════════╝
```

**Legend** `✔` done · `◐` partial · `○` todo · `⚠` risk · `▶ … ◀── YOU ARE HERE`
current stage. **Impact** `H` ≥20% · `M` 5–20% · `L` ≤5% of peak cooling load.

Keep it live: re-print the whole block on load and after each stage completes,
move the `◀── YOU ARE HERE` marker, refresh each glyph from what actually exists
in `extracts/` `gbxml/` `idf/`, and pull open `⚠` items straight from the Stage 3
gap audit so the panel never goes stale. All `⚠H` must clear before Stage 4.
`openhap status` computes the per-stage glyphs (done / todo / blocked) mechanically
from which output files exist — read it, then narrate the map.

The map values above are an illustrative default. Replace `<project>` and the
status/risk rows with the real state of the loaded project.

## Iron rules (never break)

1. **No fabrication.** Every U-value, density, SHGC, absorptance, area, occupancy
   comes from DXF extraction, the project's own source docs, or an explicit
   ASHRAE citation. Never a "typical" value. Missing → it goes in the Stage 3
   clarifications list, not into the report.
2. **One source of truth for constants.** Don't hardcode project numbers in this
   skill. The user supplies the locked envelope/design values at Stage 0 (or
   points to the project's own spec docs / project HVAC agent). This skill is the
   *procedure*, not the data.
3. **Peer-review before write.** Show substantive scripts/docs as a chat code
   block first; only write files after the user signs off.
4. **One step at a time.** Run one stage, report the result plainly (including
   zero / implausible results), get a green light, then continue. No parallel
   data hunting.

## Stage 0 — Scope and inputs

Confirm before touching anything:
- Which drawing? (path to the DWG/DXF)
- What's in scope? (which floors / zones / building portion)
- Climate / design conditions (city, ASHRAE 0.4 % design DB/WB)
- Standards basis (ASHRAE 62.1 ventilation / 90.1 LPD / Fundamentals Ch.18 CLTD)
- The locked envelope values the user is providing ("the values we gave"):
  wall / roof / window U, SHGC, solar absorptance, floor-to-floor +
  floor-to-ceiling heights, infiltration ACH.

If the project already has these locked in its own spec docs, read them from
there instead of re-asking.

`openhap init <project>` scaffolds the `hap-project.toml` these values go into (the
`[drawing]`, `[floors]`, `[site]`, and `[gbxml]` sections). Capturing them there
now means the rest of the pipeline is just `openhap run`.

## Stage 1 — Convert the drawing (DWG → DXF)

**This is NOT a python step.** DXF conversion is the external **ODA File
Converter**.

- If a DXF already exists (commonly `~/Downloads/ODA-Output/<DRAWING>.dxf`), use
  it — skip conversion and mark Stage 1 ✔ skipped on the map.
- If only a DWG exists, run ODA File Converter (to R12/AC1009 DXF), or tell the
  user the one manual step. Do **not** write a python "DWG converter."
- ⚠ Quote the drawing path always — filenames may carry spaces or non-ASCII
  characters; don't trust bash globs to grab them.

## Stage 2 — Get the numbers (run the ezdxf scripts)

The toolset ships in `tools/`, driven by the **`openhap` runner** (`openhap.py`). One
`hap-project.toml` holds every project fact; the tools read it — **nothing
project-specific is baked in.** See `tools/TOOLS.md` for the run-order table, the
config-key map, and inputs/outputs.

Scaffold, then set the drawing and let the runner drive:

```bash
openhap init <project> && cd <project>    # writes hap-project.toml + extracts/ + gbxml/
# edit hap-project.toml → [drawing].dxf
openhap doctor                            # uv, dep cache, drawing opens, config gaps
openhap run audit                         # layer audit → names the floor blocks
openhap suggest floors  --write           # drafts [floors.blocks] from the DXF
openhap suggest windows --write           # drafts [[windows.include]]
```

Uncomment/label the real candidates in the toml, then run stages by name (the
runner pulls in prerequisites and skips stages whose outputs already exist):

```bash
openhap run takeoff        # chain A: audit → geometry → takeoff (rooms+labels)
openhap run seed           # chain B: inventory → rooms → seed (ASHRAE 62.1 OA)
openhap run orient         # windows → north → orient (per-window N/S/E/W)
openhap run --all          # everything except gbxml
openhap status             # live project map from the filesystem
openhap run gbxml          # gbXML for the HAP round-trip (needs the spaces register CSV)
```

Still run **one stage at a time, reporting each result** (the runner does one at
a time by default). A tool that finds an unset key **exits naming it** — by
design, not an error. If a stage returns 0 windows or implausible areas, **say
so** — don't paper over it. Watch for: units (drawings are often mm →
auto-converted to m), and whether the export carries real room polygons or only
labels + wall lines (common Revit-export limitation — per-room areas may need
wall-buffering or a re-export). `openhap status` shows which stages are done (their
outputs exist); after changing the drawing or config, rerun with `openhap run
<stage> --force`.

The EnergyPlus validation track (IDF generator + EP-output parsers) is **not
bundled** — secondary/corroborating and tightly coupled; run it from the source
project if needed (see `tools/TOOLS.md`).

## Stage 3 — Clarifications + risks (the gap audit)

Before emitting any report data, produce an impact-ranked gap audit. For each
gap: the **input**, **Impact** (H ≥20 % / M 5–20 % / L ≤5 % on peak cooling
load), **Effort**, and a note. Then:
- **Ask** the user (or recommend a single email to the client) for the cheap,
  high-impact unknowns — per-room occupancy, equipment loads (laptops / lab?),
  operating schedule, top-floor roof exposure / ceiling status.
- **Flag** the assumptions you're forced to make and the risk if wrong (e.g.
  north-axis mapping drives every solar number; top-floor roof exposure can
  double a floor's load; template-estimated areas for irregular rooms).

Surface these as the `⚠` rows on the map. **Do not proceed to Stage 4 on any
H-impact gap** without an explicit user decision: real value vs. stated
assumption.

## Stage 4 — Print the final data package

Emit the dataset the report / HTML-designer Claude consumes. **Plain markdown,
dense factual tables, every number sourced.** Sections, all required, in order:

1. **Project header** — title, site, designer, standards basis
2. **Design conditions** — outdoor (ASHRAE 0.4 % DB/WB) + indoor setpoints
3. **Envelope assemblies** — wall / roof / glazing: construction, U, SHGC, α
4. **Internal loads** — occupancy, lighting (W/m²), equipment (W/m²)
5. **Ventilation** — OA rates (ASHRAE 62.1 cfm/person + cfm/ft²)
6. **Worst-case room calc** — line-by-line: solar + conduction + internal,
   sensible + latent + total → BTU/hr → tons → selected SKU
7. **Equipment schedule** — per-floor, per-space SKU + count
8. **Building rollup** — hand calc vs EnergyPlus, convergence %
9. **Peak-hour load component shares** — people / envelope / solar / lights %

Close with the rule the designer must obey: **"Use only the numbers in this
package."** Then list the source files behind each number in priority order
(earlier overrides later on overlap), so the designer can verify.

Show this package in chat. Only write it to a file if the user asks.
