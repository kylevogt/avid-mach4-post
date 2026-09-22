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

`objectslist` is not a tidy list of operations. Read
`Path/Post/Processor.py::_buildPostList` in the FreeCAD source for the
authority; as of 1.0 a job arrives as:

1. a **Fixture** pseudo operation (`_TempObject`, `Name == "Fixture"`)
   whose path is the work offset — plus, for every fixture *after* the
   first, a `G0 Z<stock ZMax + ClearanceHeightOffset>`,
2. one **ToolController** object per tool change, carrying the `M6`/`M3`
   blocks on its own `Path` — note it *is* the controller, so it has
   `.Tool` and `.ToolNumber` but **no `.ToolController` attribute**,
3. then the operations that actually cut, each with `.ToolController`.

Verified against a real export from FreeCAD **26.3.0** with
`tools/avid_probe_post.py` (`objectslist` was exactly
`['Fixture', 'TC__1_4__Flat', 'Adaptive']`). Two things that report settled
and that are easy to get wrong:

* **The objects are `Postable` wrappers, not document objects.** They proxy
  attribute access to the underlying object, which is why duck typing keeps
  working — but they do not all carry the attributes a document object
  would. The Fixture wrapper has no `TypeId`, no `Placement` and no
  `Active`; the ToolController wrapper reports `ToolController = None` and
  carries `.Tool`/`.ToolNumber` itself. Never `isinstance` check, and never
  assume an attribute exists.
* **`ClearanceHeight` / `SafeHeight` are on the operation** (5.0 mm and
  3.0 mm in that export) if a future change ever needs them.

**The Job object is not in the list.** `_buildPostList` never appends it,
so `_find_job()` returns `None` on a real export and the job-label and
machine comments never appear — which is why a real export starts straight
at the tool table. The code stays because a caller *may* pass a Job (and
the tests do), but do not rely on it. Confirm anything else about the list
with `tools/avid_probe_post.py` rather than by assuming.

Ordering matters and is set by the Job's `OrderOutputBy`. Under the
default `Fixture` ordering, `currTool` is tracked *across* fixtures, so a
second fixture using the same tool gets **no** ToolController and therefore
no tool change — the only thing separating the two parts is the fixture
word. That is why a change of work offset has to invalidate every cached
axis word (see below).

Consequences that have already bitten once, all covered by
`tests/test_freecad_job_graph.py` and the `freecad_job_graph.tap` golden:

* FreeCAD's own posts read their commands through
  `PathUtils.getPathWithPlacement(obj)`, not `obj.Path.Commands`: an
  operation carries a `Placement`, and reading the commands raw posts it at
  the wrong coordinates when that Placement is not the identity.
  `iter_commands()` does the same, behind a lazy optional import.
* "first section" is **not** "first tool change" — the Fixture pseudo op
  gets there first, so count tool changes for the `M1` optional stop.
* The tool table has to be enriched across objects: the ToolController knows
  the tool, the operations know the Z depths, and neither alone is enough.
* Several sections can open before anything moves, so the start-up retract
  must be suppressed when the tool is already parked (`self.retracted`,
  cleared whenever Z is commanded).
* **`F` is never emitted on a rapid, and a tool controller's rapid rates
  are ignored.** `G0` has no way to carry a speed — the machine runs it at
  whatever its motor tuning says, which is where rapid rate belongs. An
  earlier version converted `G0` to `G1` at the controller's rate to honour
  it; that was removed deliberately, because it made every positioning move
  answer to the feed override and bought nothing Mach4 was not already
  doing. Note FreeCAD *does* put an `F` on rapids (`F:0` when the rates are
  unset, the rate itself when they are set) — dropping it is deliberate,
  and emitting it would set the modal feed to a rapid rate or to zero.
* **A block that commands no movement is never emitted, feed included.**
  FreeCAD emits bare `G0` commands with no axis words at all, and `G0 Z<x>`
  where Z is already at x — 11 such blocks in a real three-operation
  export. `write_linear` returns before consuming the feed so no stray
  `F220.` line is left behind. Safe because FreeCAD states `F` on *every*
  cutting move (1718 of 1718 in that export); if that ever stops being
  true, the feed has to be carried forward instead. See
  `TestBlocksThatMoveNothing`.
* **FreeCAD's motion order is passed through untouched.** An operation goes
  to `ClearanceHeight`, traverses in XY *there*, drops to `SafeHeight` and
  cuts; between passes it lifts back to clearance before traversing again.
  That is already safe — the clearance plane is the height FreeCAD
  designates for rapid traverses, and the whole job depends on it between
  operations — so this post does not reorder it.

  An earlier version buffered the opening `G0 Z<clearance>` and emitted it
  after the XY rapid, to reach the `G0 X.. Y..` / `G43 Z.. H1` shape an
  AVID/Fusion program has. That was dropped deliberately. Fusion can write
  that shape because it is handed the section's initial position and
  composes the approach itself; this post only ever sees a flat command
  stream, so matching the shape meant *inferring* intent and moving blocks
  around. Every bug in that area came from the inference, not from
  FreeCAD's ordering. **Do not reintroduce it** — the post reorders
  nothing, and that is the property that makes it reviewable.
* **`G43` is established before any Z move after a tool change.** It rides
  the first plain Z word in `write_linear` (rapid *or* plunge — an operation
  whose first Z move is the plunge would otherwise cut the whole pass on the
  previous tool's offset and jump when G43 finally landed on the retract).
  Anything else that can move Z — a helical arc, a canned cycle, a probe
  going through `_passthrough` — calls `apply_tool_length_offset()` for a
  standalone `G43 H<n>` first. If you add another path that emits Z, wire it
  up too.
* **`write_absolute_mode()` is the only way to emit `G90`/`G91`.** A real
  change of coordinate mode invalidates every cached axis word; a restated
  `G90` that changes nothing must not, because FreeCAD's Drilling op
  restates `G90` in the middle of its path. `TestRealDrillingStream` pins
  that exact stream.
* **Anything that moves the coordinate frame invalidates the cached axis
  words.** A cached word is only safe to suppress while it still means the
  same physical place. `write_work_offset` resets the axis outputs on a
  change of fixture, `write_absolute_mode` on a G90/G91 switch, and the
  `G43`/`G49` branch resets `z_output`. Miss one and a move that really is
  a move gets dropped — that is how a G55 job once plunged at the G54
  part's location. See `TestWorkOffsets`.
* **The fixture the job selected is restated after a tool change, not
  `G54`.** FreeCAD puts the Fixture pseudo op *before* the ToolController,
  so a hard-coded `G54` fallback there silently moved the whole program
  onto the wrong fixture. `active_work_offset` remembers what the stream
  asked for; `G54` is only the fallback when it never asked.
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

* **Rapids carry `F` and it is zero.** A ToolController whose `HorizRapid`
  and `VertRapid` are unset reports `0.00 in/min`, and FreeCAD puts `F:0.0`
  into the `G0` commands themselves — 3 of the 16 rapids in the verified
  export. `write_linear` drops `F` on `G0` (`if code != 0`); if that guard
  ever goes, the program emits `F0.` and the machine faults or crawls.
* **FreeCAD internal units are mm/kg/s.** Lengths in a `Path.Command`
  parameter are millimetres, but **velocities are mm/SECOND, not mm/min** —
  FreeCAD's base time unit is the second, which is why every post FreeCAD
  ships wraps `F` in `Units.Quantity(value, FreeCAD.Units.Velocity)` before
  converting. Getting this wrong emits a program that runs at 1/60 speed.
  The conversion lives in `FEED_UNIT_SCALE` and the `Formatter` `scale`
  factor; never convert twice. **Confirmed against a real export**: a
  ToolController set to `220.00 in/min` reports `Quantity Value=93.1333`
  with `Unit: mm/s`, and the `G3` commands carry `F=93.133333`; 93.1333 x
  60 / 25.4 = 220.00. Tests state feeds through the `mmpm()` /
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
  kernel. It emits the same moves, at the same coordinates, in the same
  order as the path it was given. The only blocks it adds are retracts, and
  the only ones it drops command no movement. It never changes a move.
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
mode, a retract moves **Z in a block of its own**, before any XY move.

Do not change the `--safe-retracts` default without saying so in the PR
description: it decides where a manual tool change happens.

## The end of the program

The footer retracts **Z only**. It used to follow that with a traverse to
machine home in XY, which is a full-width move across the table at
whatever height Z stopped at — work holding, dust shoes and clamps are all
in that path, and nothing about the end of a program needs the gantry
parked. `--home-xy-at-end` asks for the old behaviour and is off by
default. The XY home block is still pinned end to end by
`tests/fixtures/line_numbers.tap`, which is posted with
`--safe-retracts g53 --home-xy-at-end`, so both footers stay covered by a
golden.

The Z retract is emitted unconditionally, even when the last operation
already ended parked at clearance height: that block is what guarantees
the tool is clear, and it is not worth making it depend on the post's own
position tracking.

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
