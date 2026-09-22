# avid-mach4-post

A FreeCAD CAM post processor for **AVID CNC** machines running **Mach4**.

It emits programs in the shape an AVID machine is used to seeing — the same
number formatting, tool-change sequence, safe retracts and arc form as the
g-code AVID's own Fusion 360 post produces — so FreeCAD output runs without
hand editing.

## Install

Copy `avid_mach4_post.py` into FreeCAD's **user macro directory**. FreeCAD's
CAM workbench searches that directory for `*_post.py` files, so the post shows
up in the Job's post processor dropdown as **`avid_mach4`** after a restart.

| OS | User macro directory |
| --- | --- |
| macOS | `~/Library/Application Support/FreeCAD/Macro/` |
| Linux | `~/.local/share/FreeCAD/Macro/` |
| Windows | `%APPDATA%\FreeCAD\Macro\` (i.e. `C:\Users\<you>\AppData\Roaming\FreeCAD\Macro\`) |

**FreeCAD 1.1 and later insert a version folder** before `Macro`, named
`v<major>-<minor>` — so on macOS the path becomes
`~/Library/Application Support/FreeCAD/v1-1/Macro/`, on Linux
`~/.local/share/FreeCAD/v1-1/Macro/`, and on Windows
`%APPDATA%\FreeCAD\v1-1\Macro\`. FreeCAD 1.0 and earlier have no version
folder.

If in doubt, let FreeCAD tell you. Either read it off *Edit → Preferences →
Python → Macro → Macro recording settings → Macro path*, or paste this into
the FreeCAD Python console:

```python
import FreeCAD; print(FreeCAD.getUserMacroDir())
```

Then copy the file there:

```bash
# macOS (FreeCAD 1.1+ — drop the v1-1 for 1.0)
cp avid_mach4_post.py ~/Library/Application\ Support/FreeCAD/v1-1/Macro/

# Linux
cp avid_mach4_post.py ~/.local/share/FreeCAD/v1-1/Macro/
```

Restart FreeCAD, then pick **`avid_mach4`** as the Job's post processor.

### Other locations FreeCAD searches

* *Edit → Preferences → CAM → Job Preferences → General → Defaults → Path* —
  "Path to look for templates, post processors, tool tables and other external
  files." Point it at this checkout to run the post straight from git.
* `…/Mod/CAM/Path/Post/scripts/` inside the FreeCAD installation
  (`…/Mod/Path/Post/scripts/` on 0.21 and earlier). This works, but it lives
  inside the application itself, so an upgrade or reinstall wipes it — on
  macOS it is also inside the signed `.app` bundle. Prefer the macro
  directory.

## Usage

Post processor arguments go in the Job's *Output → Post Processor Arguments*
box. **The defaults reproduce what AVID's own Fusion post emits**, with one
deliberate exception — retracts:

```
--inches --safe-retracts g28 --no-header --optional-stop --use-m6
```

Matching Fusion: inches and `G20`, the `G90 G94 G91.1 G40 G49 G17`
preamble, a header of program name + machine (only if the job names one) +
tool table and no timestamp, `M5` / `T<n> M6` / `S<rpm> M3` / work offset /
`G0 X<x> Y<y>` / `G43 Z<z> H<n>` at each tool change, `I`/`J` arcs, and
`M30`. [`tests/fixtures/avid_fusion_shape.tap`](tests/fixtures/avid_fusion_shape.tap)
is pinned to a real AVID/Fusion export and reproduced with
`--safe-retracts none` and nothing else.

### Retracts

`--safe-retracts g28` puts `G28 G91 Z0.` / `G90` before every tool change,
so the spindle is parked at the top of Z when you swap a tool by hand
rather than sitting a few millimetres above the work. It also parks Z and
sends X/Y to machine home at program end. This assumes Mach4's G28 position
is machine zero, which on an AVID is the top of Z after homing — **check
that on your machine before the first cut.**

| Mode | What it emits |
| --- | --- |
| `g28` (default) | `G28 G91 Z0.` / `G90` before every tool change, then `G28 G91 X0. Y0.` / `G90` at the end |
| `g30` | the same through `G30` |
| `g53` | `G53 G0 Z<--home-z>`, then `G53 G0 X<--home-x> Y<--home-y>` |
| `none` | nothing — what AVID's Fusion post does with `useG28` off |

`--safe-retracts none` is the setting that matches an AVID/Fusion program
exactly: no `G28` after the preamble, no move to machine home before `M30`,
and each operation's own clearance-height Z move left to lift the tool.

Common adjustments:

| Argument | Effect |
| --- | --- |
| `--metric` | output millimetres and `G21` instead of inches and `G20` |
| `--safe-retracts none` | no retracts at all — matches AVID's Fusion output |
| `--safe-retracts g30` | retract via `G30` |
| `--safe-retracts g53` | machine-coordinate retracts, no `G28`/`G30` |
| `--header` | add a generated-by and timestamp header |
| `--home-z -25.4` | Z machine position `--safe-retracts g53` retracts to |
| `--dust-collector` | `M7` in the header, `M9` in the footer |
| `--line-numbers` | prefix every block with an `N` word |
| `--radius-arcs` | emit arcs as `R` instead of `I`/`J`/`K` |
| `--no-optional-stop` | never emit `M1` before a tool change |
| `--feed-units mm-per-minute` | only if a path's `F` is already mm/min (FreeCAD's is mm/s) |
| `--preamble "…;…"` | extra blocks after the preamble, `;` separated |

`--home-x`, `--home-y` and `--home-z` are in **millimetres**, whatever the
output unit — they are machine coordinates, and FreeCAD's internal unit is
the millimetre.

Run `python -c "import avid_mach4_post; print(avid_mach4_post.TOOLTIP_ARGS)"`
for the full list.

## Sample output

With no arguments, from a real FreeCAD job graph:

```gcode
(T2  D=0.25 CR=0. - ZMIN=-0.0311 - 1-4 FLAT)
G90 G94 G91.1 G40 G49 G17
G20
G28 G91 Z0.
G90

(FIXTURE)
G54

(TC 1-4 FLAT)
M5
T2 M6
S12000 M3

(ADAPTIVE)
G54
G0 X3. Y2.874
G43 Z0.1969 H2
Z0.1181
G1 Z0. F80.
G3 Y3.126 Z-0.0311 I0. J0.1248 F200.
G1 Y2.874
G0 Z0.1969

G28 G91 Z0.
G90
G28 G91 X0. Y0.
G90
M30
```

The `(FIXTURE)` and `(TC …)` blocks are FreeCAD's doing — it hands the post
the work offset and each tool controller as pseudo operations of their own,
where Fusion would fold them into the operation that follows. The g-code is
equivalent.

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

Things worth knowing about the motion this post emits:

* **The toolpath is FreeCAD's, in FreeCAD's order.** The post reorders
  nothing: an operation goes to its clearance height, traverses in XY
  there, drops to its safe height and cuts, exactly as FreeCAD planned it.
  The only blocks the post *adds* are the retracts below; the only ones it
  drops command no movement at all. So the clearance and safe heights set
  on each operation are what keep the tool out of trouble — as they already
  are for every traverse between operations.
* **`G43` is applied before any Z move after a tool change** — it rides the
  first plain Z word (rapid or plunge), and is stated on a line of its own
  ahead of a helical arc, a drilling cycle or a probe.
* **Every operation starts in `G90`**, so one operation cannot leave the
  next one issuing incremental moves, and no cached coordinate survives a
  change of coordinate mode.
* **A `G20`/`G21` in the path is ignored.** The output unit is set by
  `--inches`/`--metric` and every number is scaled to it; letting a stray
  unit word through would rescale the whole program.
* **Every tool change is preceded by a retract to the top of Z** by
  default, so the spindle is out of the way when you swap a tool. Retracts
  lift Z in a block of their own and only then traverse. `--safe-retracts
  none` turns them off, matching AVID's Fusion output. See
  [Retracts](#retracts).

## Provenance and licence

The behaviour targeted here is the *g-code* AVID CNC machines expect, taken
from real exports and pinned by the reference programs in
[`tests/fixtures/`](tests/fixtures). The implementation is original: it is
not a translation of AVID's Autodesk `.cps` post, which carries an
"All rights reserved" notice.

This project is licensed under the AGPL-3.0 (see [LICENSE](LICENSE)). Note
that AGPL is incompatible with FreeCAD's LGPL, so the post cannot be merged
upstream into FreeCAD as it stands.
