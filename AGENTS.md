# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this repo is

A single custom **post processor for the FreeCAD CAM workbench** that emits
g-code for **AVID CNC** machines running **Mach4**.

The behavioural specification is the post processor AVID CNC publishes for
Autodesk Fusion 360 (`avid cnc.cps`, an Autodesk `.cps` JavaScript post).
When you need to decide what this post *should* do, the Fusion post is the
source of truth — match its output, not some other vendor's convention.

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

## Conventions that matter to the output

* **FreeCAD internal units are millimetres and mm/min.** All conversion
  happens in the `Formatter` `scale` factor. Never convert twice.
* **Fusion style numbers**: trailing zeros trimmed, decimal point always
  present — `X0.`, `Z-1.25`, `F60.`. That is `Formatter(force_decimal=True,
  trim=True)`. Integers (`S`, `T`, `H`, `N`) use `force_decimal=False`.
* **Comments** are upper-cased and filtered to `PERMITTED_COMMENT_CHARS`;
  Mach4 rejects anything else inside parentheses, including `:` and nested
  `()`.
* **Modality**: `OutputVariable` suppresses unchanged axis/feed words,
  `Modal` suppresses unchanged g-code groups. Both mirror Fusion's
  `createVariable` / `createModal`. Reset them whenever the control's state
  is invalidated (tool change, retract, `G80`).
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

## Working on the post

* Prefer translating FreeCAD's existing command stream over synthesising
  motion. The post is a formatter plus a small state machine, not a CAM
  kernel.
* New user-facing options go through `_build_parser()` as a paired
  `--flag` / `--no-flag` (via the local `flag()` helper) so FreeCAD's
  post-processor argument box behaves predictably, and get a default that
  matches the AVID Fusion post.
* `TOOLTIP_ARGS` is generated from the parser — it does not need manual
  updating.
* Anything that changes emitted g-code should also update
  `tests/fixtures/*.tap` in the same commit.

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
