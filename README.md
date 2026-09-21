# avid-mach4-post

A FreeCAD CAM post processor for **AVID CNC** machines running **Mach4**.

It reproduces the behaviour of the post processor AVID CNC ships for Autodesk
Fusion 360, so g-code exported from FreeCAD looks and runs like the g-code an
AVID machine is used to.

## Install

Copy `avid_mach4_post.py` into FreeCAD's post processor directory, or point
FreeCAD at this repository:

* **Drop-in:** copy the file into
  `…/FreeCAD/Mod/CAM/Path/Post/scripts/` (FreeCAD 1.0+) or
  `…/FreeCAD/Mod/Path/Post/scripts/` (0.21 and earlier).
* **Out of tree:** *Edit → Preferences → CAM → Job Preferences →
  "Post Processor search paths"* and add this checkout.

Then pick **`avid_mach4`** as the Job's post processor.

## Usage

Post processor arguments go in the Job's *Output → Post Processor Arguments*
box. The defaults already match the AVID Fusion post:

```
--inches --safe-retracts g28 --optional-stop --use-m6
```

Common adjustments:

| Argument | Effect |
| --- | --- |
| `--metric` | output millimetres and `G21` instead of inches and `G20` |
| `--safe-retracts g30` | retract via `G30` instead of `G28` |
| `--safe-retracts g53` | machine-coordinate retracts, no `G28`/`G30` |
| `--dust-collector` | `M7` in the header, `M9` in the footer |
| `--line-numbers` | prefix every block with an `N` word |
| `--radius-arcs` | emit arcs as `R` instead of `I`/`J`/`K` |
| `--no-optional-stop` | never emit `M1` before a tool change |
| `--feed-units mm-per-minute` | only if a path's `F` is already mm/min (FreeCAD's is mm/s) |
| `--preamble "…;…"` | extra blocks after the preamble, `;` separated |

Run `python -c "import avid_mach4_post; print(avid_mach4_post.TOOLTIP_ARGS)"`
for the full list.

## Sample output

```gcode
(T2  D=0.25 CR=0. - ZMIN=-0.0311 - 14 FLAT)
G90 G94 G91.1 G40 G49 G17
G20
G28 G91 Z0.
G90

(FIXTURE)
G54

(TC 14 FLAT)
M5
T2 M6
S12000 M3

(ADAPTIVE)
G54
G0 G43 Z0.1969 H2
X3. Y2.874
Z0.1181
G1 Z0. F80.
G3 Y3.126 Z-0.0311 I0. J0.1248 F200.
…
G28 G91 Z0.
G90
G28 G91 X0. Y0.
G90
M30
```

More complete examples live in [`tests/fixtures/`](tests/fixtures).

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

The test suite runs without FreeCAD — see [AGENTS.md](AGENTS.md) for the
design constraints that keep it that way.

## Safety

Always inspect the generated program and dry-run it above the work before
cutting. This software comes with no warranty; see [LICENSE](LICENSE).
