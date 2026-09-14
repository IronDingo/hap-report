# openHAP

A reusable, FOSS, Linux-native pipeline that turns a building drawing into the
structured cooling-load dataset a HAP-style report needs — without AutoCAD, HAP,
or Windows. It's a [Claude Code](https://claude.com/claude-code) skill plus a
toolset of `ezdxf` / `shapely` extraction scripts.

You bring the drawing and the locked design values; the pipeline regenerates the
numbers — auditable and reproducible, not a black box.

## Pipeline

```
Stage 0  Scope & inputs         drawing, in-scope floors, climate, standards, envelope values
Stage 1  DWG → DXF              external ODA File Converter (not a Python step)
Stage 2  Extract numbers        uv run the tools/ scripts → extracts/
Stage 3  Clarifications & risks  impact-ranked gap audit (H/M/L on peak cooling load)
Stage 4  Final data package     dense, sourced markdown tables → report / HTML designer
```

Full procedure + project map: [`SKILL.md`](SKILL.md). Toolset reference:
[`tools/TOOLS.md`](tools/TOOLS.md).

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) (handles dependencies via PEP 723
inline metadata — no venv). Convert your DWG to DXF first (ODA File Converter),
then drive everything through the `openhap` runner:

```bash
alias openhap='uv run /path/to/openhap/openhap.py'

openhap init my-project && cd my-project   # scaffold hap-project.toml + extracts/ + gbxml/
# edit hap-project.toml → set [drawing].dxf
openhap doctor                             # preflight: uv, dep cache, drawing opens, config gaps
openhap suggest floors  --write            # draft [floors.blocks] from the DXF — uncomment the real ones
openhap suggest windows --write            # draft [[windows.include]] — uncomment/label the real families
openhap run --all                          # runs every stage (except gbxml) in dependency order
openhap status                             # live project map from the filesystem
```

**One project, one config file.** All per-project facts — drawing path, floor
blocks, in-scope floors, window families, north reference, envelope/site values
— live in `hap-project.toml`. The tools themselves stay generic: industry
standards are documented defaults, and nothing project-specific is baked into
code. Any stage whose config is unset stops with a message naming the exact key
and the command that drafts it.

Env vars (`DXF_PATH`, `HAP_EXTRACTS`, `SPACES_CSV`, `HAP_GBXML_OUT`) still
override the file. To run a single tool directly (the old workflow), point it at
a config with `HAP_PROJECT=/abs/hap-project.toml uv run tools/03_build_takeoff.py`.
See [`tools/TOOLS.md`](tools/TOOLS.md) for the run-order table, the config
schema, and per-tool inputs/outputs.

### Offline behaviour

Built for unstable connections: every tool launches `uv run --offline` first, so
a warm dependency cache means zero network. On a genuine cache miss the runner
retries online automatically and says so. `openhap doctor` reports up front whether
the cache can carry a fully offline session.

## Tests

Dependency-free, offline, no pytest — run directly:

```bash
python3 tests/test_runner.py       # runner + config unit tests
python3 tests/test_pipeline.py     # chain A end-to-end (known-answer areas)
python3 tests/test_pipeline_b.py   # chain B + windows + north + orient (known-answer)
```

The pipeline tests generate synthetic DXFs (`tests/make_fixture*.py`) whose room
areas, window sizes, and compass answers are fixed by construction, then assert
the tools reproduce them. They SKIP (not fail) if uv or the dependency cache is
unavailable offline.

## Layout

```
openhap.py            the runner: init / doctor / status / run / suggest
SKILL.md          the 5-stage procedure + project map (Claude-facing)
tools/            ezdxf / shapely extraction scripts (run via uv)
tools/hap_config.py  per-project config loader shared by every tool
tools/TOOLS.md    run order, config-key map, env vars, spaces-CSV schema
tests/            make_fixture*.py fixtures + dependency-free known-answer tests
hap-project.toml  (per project, written by `openhap init`) the single source of truth
```

## Use as a Claude Code skill

Copy or symlink the repo into `~/.claude/skills/openhap/` and it loads as the
`/openhap` skill.

## Notes

- Industry standards are kept as documented defaults (AIA NCS layer names,
  ASHRAE 62.1 ventilation rates, ASHRAE Ch. 26 surface defaults) — adjust per
  project.
- No drawings, extracted data, or project numbers live in this repo by design
  (see [`.gitignore`](.gitignore)). The pipeline reproduces them from your inputs.
- The EnergyPlus validation track (model generator + result parsers) is a
  separate, more tightly-coupled step and is intentionally not included.
