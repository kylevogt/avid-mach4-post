# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this repo is

A single custom **post processor for the FreeCAD CAM workbench** that emits
g-code for **AVID CNC** machines running **Mach4**.

The behavioural specification is **the g-code an AVID machine expects** —
the shape of the programs AVID's own Fusion 360 post produces, as observed
in real `.tap` files. When you need to decide what this post *should* do,
match that output, not some other vendor's convention.

**Match the output, not the source.** AVID's Fusion post (`avid cnc.cps`) is
an Autodesk `.cps` file carrying an "All rights reserved" copyright notice.
Reproducing the g-code it emits is fine; transcribing or translating its
internals is not, and this repo is public and AGPL. Use the golden files in
`tests/fixtures/` and real exports as the reference, and do not name or
mirror `.cps` function names in the code or comments.

(Relatedly: AGPL means this post cannot be merged upstream into FreeCAD,
which is LGPL, without relicensing.)

## Layout

| Path | Purpose |
| --- | --- |
| `avid_mach4_post.py` | The post processor. Single file, drop-in for FreeCAD. |
| `tests/conftest.py` | FreeCAD test doubles (`FakeOperation`, `cmd`, ...) and fixtures. |
| `tests/test_*.py` | Unit tests. |
| `tests/fixtures/*.tap` | Golden reference programs. |
| `.github/workflows/ci.yml` | pytest matrix + ruff on PRs to `main`. |

## Hard constraints

1. **`avid_mach4_post.py` must stay importable without FreeCAD.**
   FreeCAD is not installable on the CI runners, and the whole test suite
   depends on plain-CPython importability. Anything FreeCAD specific goes
   behind a lazy `import` inside a function (see `_maybe_show_editor`), and
   FreeCAD objects are only ever *duck typed* (`getattr` / `hasattr`), never
   isinstance-checked.

2. **Keep it a single file.** FreeCAD discovers post processors by scanning
   for `*_post.py`; users install this by copying one file. Do not split it
   into a package.

3. **The module name must keep the `_post.py` suffix** — that is what makes
   FreeCAD list it as `avid_mach4` in the Job's post processor dropdown.

4. **`export(objectslist, filename, argstring)` is the public API.**
   FreeCAD calls it. A `filename` of `"-"` returns the g-code without
   writing a file; the tests rely on this.

## What FreeCAD actually passes to `export`

`objectslist` is not a tidy list of operations. A real job arrives as:

1. the **Job** object,
2. a **Fixture** pseudo operation whose whole path is the work offset
   (`G54`),
3. one **ToolController** object per tool, carrying the `M6`/`M3` blocks on
   its own `Path` — note it *is* the controller, so it has `.Tool` and
   `.ToolNumber` but **no `.ToolController` attribute**,
4. then the operations that actually cut, each with `.ToolController`.

Consequences that have already bitten once, all covered by
`tests/test_freecad_job_graph.py` and the `freecad_job_graph.tap` golden:

* "first section" is **not** "first tool change" — the Fixture pseudo op
  gets there first, so count tool changes for the `M1` optional stop.
* The tool table has to be enriched across objects: the ToolController knows
  the tool, the operations know the Z depths, and neither alone is enough.
* Several sections can open before anything moves, so the start-up retract
  must be suppressed when the tool is already parked (`self.retracted`,
  cleared whenever Z is commanded).
* **The XY-before-Z convention is AVID's; the reordering that achieves it is
  ours.** A real AVID/Fusion program positions XY and only then brings Z
  down (`tests/fixtures/avid_fusion_shape.tap`, pinned to one). But an
  Autodesk post does not reorder anything — it is handed the section's
  initial position as data and writes the approach itself. FreeCAD gives
  this post a flat command stream instead, so it has to *infer* the same
  intent and move blocks around. The convention is borrowed; the inference
  is invented here, and it is the only place this post changes the order of
  what FreeCAD emitted. Every bug in this area so far has come from the
  inference, not the convention. Treat it as the highest-risk code in the
  file and keep `TestXYBeforeZ`, `TestZLiftIsNeverDeferred` and
  `TestNoSafeRetracts` green.
* **Real operations open with `G0 Z<clearance>` and only then rapid in XY.**
  Emitted verbatim after a `G28` that plunges the tool to within a few
  millimetres of the table wherever the spindle is parked, then traverses
  the work at that height. `write_rapid` holds a Z-only rapid back until
  the XY rapid behind it has been written (`self.xy_positioned`,
  `self.pending_rapid_z`); the moves are reordered, never synthesised or
  dropped. See `TestXYBeforeZ`.
* **The reorder never holds back a lift** (`_may_defer` / `_is_lift`). A Z
  rapid issued while the tool is still down in the work is what takes it
  out of the cut; deferring that one drags the cutter sideways through the
  material. Three states, in order: `retracted` (an explicit retract
  happened — always defer, a `G28` height is a machine coordinate the post
  cannot compare against), `at_section_head` (a section boundary with no
  retract, as under `--safe-retracts none` — defer unless the move is a
  known lift), and otherwise never. Do not widen this to "a tool change is
  pending": an `M6` part way through an operation is not a section
  boundary. See `TestZLiftIsNeverDeferred` and `TestNoSafeRetracts`.
* **`G43` is established before any Z move after a tool change.** It rides
  the first plain Z word in `write_linear` (rapid *or* plunge — an operation
  whose first Z move is the plunge would otherwise cut the whole pass on the
  previous tool's offset and jump when G43 finally landed on the retract).
  Anything else that can move Z — a helical arc, a canned cycle, a probe
  going through `_passthrough` — calls `apply_tool_length_offset()` for a
  standalone `G43 H<n>` first. If you add another path that emits Z, wire it
  up too.
* **`write_absolute_mode()` is the only way to emit `G90`/`G91`.** A real
  change of coordinate mode invalidates every cached axis word *and* any Z
  rapid being held back; a restated `G90` that changes nothing must do
  neither, because FreeCAD's Drilling op restates `G90` in the middle of
  its path, between the opening `G0 Z<clearance>` and the first traverse.
  `TestRealDrillingStream` pins that exact stream.
* **A `G20`/`G21` in the command stream is dropped** (`UNIT_CODES`). Every
  number the post writes is already scaled to the unit chosen by
  `--inches`/`--metric` and announced in the preamble; passing a unit word
  through would have Mach4 reinterpret all of them.
* **Modal bookkeeping runs even under `--no-modal`.** `--no-modal` decides
  whether a word is *repeated*, not whether the post knows which plane or
  coordinate mode the control is in; `_plane_word`, `_is_incremental` and
  the arc endpoint maths all read that state. Never gate a `modal.format()`
  call on `args.modal`.
* **A block that is not emitted must not claim its modal group.** Consuming
  `motion_modal` for a block that turns out to be all-suppressed lets the
  next block inherit a mode the machine was never put into — that is how a
  `G1` goes out as a bare axis word under `G0`. `_motion_word()` is called
  only once a block is certain to be written.
* **Tool changes always retract first.** `begin_section` covers an operation
  that opens with `M6`; `write_tool_change` covers one that arrives part way
  through. Do not remove either.

## Conventions that matter to the output

* **FreeCAD internal units are mm/kg/s.** Lengths in a `Path.Command`
  parameter are millimetres, but **velocities are mm/SECOND, not mm/min** —
  FreeCAD's base time unit is the second, which is why every post FreeCAD
  ships wraps `F` in `Units.Quantity(value, FreeCAD.Units.Velocity)` before
  converting. Getting this wrong emits a program that runs at 1/60 speed.
  The conversion lives in `FEED_UNIT_SCALE` and the `Formatter` `scale`
  factor; never convert twice. Tests state feeds through the `mmpm()` /
  `ipm()` helpers in `tests/conftest.py` so the intent stays readable.
* **Number style**: trailing zeros trimmed, decimal point always
  present — `X0.`, `Z-1.25`, `F60.`. That is `Formatter(force_decimal=True,
  trim=True)`. Integers (`S`, `T`, `H`, `N`) use `force_decimal=False`.
* **Comments** are upper-cased and filtered to `PERMITTED_COMMENT_CHARS`;
  Mach4 rejects anything else inside parentheses, including `:` and nested
  `()`. Characters whose removal would change the *meaning* of a tool name
  go through `COMMENT_SUBSTITUTIONS` first — dropping the `/` turned
  "1/4 Flat" into `14 FLAT`, which reads as a 14 mm cutter.
* **Modality**: `OutputVariable` suppresses unchanged axis/feed words,
  `Modal` suppresses unchanged g-code groups. Reset them whenever the
  control's state is invalidated (tool change, retract, `G80`).
* **Arc centres are incremental** (`G91.1` in the preamble), matching both
  FreeCAD's native output and the AVID post.

## Testing

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # unit tests
.venv/bin/ruff check .            # lint (line length 79)
```

* Tests must never import FreeCAD. Build inputs from `tests/conftest.py`
  helpers instead: `cmd("G1", X=25.4, F=1016)` and `FakeOperation(...)`.
* `AvidPost.build(objects, now=...)` takes an injectable timestamp — use it
  for anything that asserts on header content, so output stays deterministic.
* **Golden files**: `tests/fixtures/*.tap` are full reference programs. After
  an intentional output change run `UPDATE_GOLDEN=1 python -m pytest
  tests/test_golden.py` and **read the resulting diff** — it is the clearest
  signal of what your change did to real g-code. Never regenerate them to
  make a red test go green without understanding the diff.
* When you add or change post behaviour, add a test that asserts on the
  emitted *block*, not on internal state.
* **Unit tests cannot catch a wrong assumption about FreeCAD.** The feed
  unit bug passed 127 green tests because the tests encoded the same wrong
  assumption as the code. Anything that depends on what FreeCAD puts in the
  command stream needs checking against a real export, not just a fixture
  you wrote yourself.

## Working on the post

* Prefer translating FreeCAD's existing command stream over synthesising
  motion. The post is a formatter plus a small state machine, not a CAM
  kernel.
* New user-facing options go through `_build_parser()` as a paired
  `--flag` / `--no-flag` (via the local `flag()` helper) so FreeCAD's
  post-processor argument box behaves predictably.
* **Defaults reproduce a real AVID/Fusion export, except for retracts.**
  `tests/fixtures/avid_fusion_shape.tap` is pinned to one and generated with
  `--safe-retracts none` and nothing else; if you add an option, its default
  is whatever that file shows. One consequence worth stating: there is no
  generated-by/timestamp header — `--header` asks for one. A machine block
  is written only when the job actually names a machine, because that is
  what Fusion does; do not reinstate the invented "Avid CNC" fallback.
* `TOOLTIP_ARGS` is generated from the parser — it does not need manual
  updating.
* Anything that changes emitted g-code should also update
  `tests/fixtures/*.tap` in the same commit.

## The `--safe-retracts` default

A real AVID/Fusion program (see `tests/fixtures/avid_fusion_shape.tap`,
pinned to one) has **no retract blocks at all**: `useG28` is off, so
`writeRetract` emits nothing — no `G28` after the preamble and no move to
machine home before `M30`. `--safe-retracts none` reproduces that.

The default is `g28` anyway, and it is the one deliberate divergence in the
defaults: tools are changed by hand on these machines, and a tool change
should happen with the spindle parked at the top of Z rather than a few
millimetres above the work. `g30` is the same through G30; `g53` retracts
to `--home-x`/`-y`/`-z` in machine coordinates (it used to emit nothing for
Z, which meant the footer sent the tool to machine home in XY at whatever
depth the last operation stopped at — do not reinstate that). Whatever the
mode, **Z always moves in a block of its own, before any XY traverse**.

Do not change the `--safe-retracts` default without saying so in the PR
description: it decides where a manual tool change happens.

## Safety note

This post produces g-code that drives a machine capable of injuring people
and destroying itself. Changes to retract logic, tool changes, spindle
control and work offsets are the high-risk areas. Be conservative there,
state clearly in the PR description what motion changed, and never silently
alter a default.

## CI

`.github/workflows/ci.yml` runs on GitHub-hosted runners for every PR into
`main`: pytest on Python 3.9 / 3.11 / 3.13 plus a ruff lint job. Both jobs
must be green before merge.
