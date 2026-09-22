"""Diagnostic post processor: dumps what FreeCAD actually hands a post.

This is **not** a post processor for cutting anything.  It writes a plain
text report describing every object in ``objectslist`` and every command in
their paths, so that assumptions in ``avid_mach4_post.py`` can be checked
against a real export instead of being guessed at.

Install it exactly like the real post -- copy this file into FreeCAD's user
macro directory -- then pick ``avid_probe`` as the Job's post processor and
post as usual.  The g-code it returns is inert (comments and ``M30``); the
report goes to a separate ``.txt`` file whose path is named in the g-code
and printed to the Report view.

Nothing here is imported by the real post, and nothing here is on the
machine's side of the fence.
"""

import datetime
import os
import platform
import sys

__all__ = ["export", "TOOLTIP", "TOOLTIP_ARGS", "UNITS"]

TOOLTIP = """
Diagnostic only -- dumps the objects and commands FreeCAD passes to a post
processor into a text file.  Produces no usable g-code.
"""

TOOLTIP_ARGS = """
--out <path>   write the report here instead of next to the output file
"""

UNITS = "G21"

#: Attributes worth reporting on every object handed to the post.
OBJECT_ATTRS = (
    "Name", "Label", "TypeId", "Active", "Placement", "ToolController",
    "Tool", "ToolNumber", "CoolantMode", "Job", "InList", "Group",
    "ClearanceHeight", "SafeHeight", "StartDepth", "FinalDepth",
    "HorizFeed", "VertFeed", "HorizRapid", "VertRapid", "SpindleSpeed",
    "SpindleDir",
)

#: Attributes worth reporting on a Tool.
TOOL_ATTRS = (
    "Name", "Label", "Diameter", "CornerRadius", "LengthOffset",
    "CuttingEdgeHeight", "ShapeName", "ToolType",
)


def _describe(value, depth=0):
    """Render a value with its type, without ever raising."""
    try:
        if value is None:
            return "None"
        kind = type(value).__name__
        if isinstance(value, (str, int, float, bool)):
            return f"{value!r} <{kind}>"
        # FreeCAD Quantity and similar carry .Value / .UserString
        parts = []
        for attr in ("Value", "UserString", "Unit"):
            if hasattr(value, attr):
                try:
                    parts.append(f"{attr}={getattr(value, attr)!r}")
                except Exception as exc:
                    parts.append(f"{attr}=<error {exc}>")
        if parts:
            return f"<{kind} {' '.join(parts)}>"
        if isinstance(value, (list, tuple)) and depth == 0:
            inner = ", ".join(_describe(v, depth + 1) for v in value[:8])
            more = "" if len(value) <= 8 else f", ... ({len(value)} total)"
            return f"<{kind} [{inner}{more}]>"
        return f"{value!r} <{kind}>"
    except Exception as exc:  # pragma: no cover - diagnostics must not fail
        return f"<undescribable: {exc}>"


def _commands_of(obj, use_placement):
    """Return an object's commands, optionally through getPathWithPlacement."""
    path = getattr(obj, "Path", None)
    if path is None:
        return None
    if use_placement:
        for module_name in ("PathScripts.PathUtils", "Path.Base.Util"):
            try:
                import importlib

                module = importlib.import_module(module_name)
                return list(module.getPathWithPlacement(obj).Commands), \
                    module_name
            except Exception:
                continue
        return None, None
    try:
        return list(path.Commands), None
    except Exception as exc:
        return f"<error reading Commands: {exc}>", None


def _report(objectslist, argstring):
    out = []
    add = out.append

    add("=" * 72)
    add("avid_probe_post diagnostic report")
    add("=" * 72)
    add(f"generated      : {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    add(f"python         : {sys.version.split()[0]} on {platform.platform()}")
    try:
        import FreeCAD

        add(f"freecad        : {'.'.join(FreeCAD.Version()[:3])} "
            f"build {FreeCAD.Version()[3]}")
        add(f"user macro dir : {FreeCAD.getUserMacroDir(True)}")
    except Exception as exc:
        add(f"freecad        : <not importable: {exc}>")
    add(f"argstring      : {argstring!r}")
    add(f"objectslist    : {len(objectslist)} objects")
    add("")

    add("-" * 72)
    add("QUESTION 1 -- is the Job object passed to the post?")
    add("-" * 72)
    names = [getattr(o, "Name", "?") for o in objectslist]
    add(f"object .Name values, in order: {names}")
    add("If none of these is the Job, the post's job label and machine")
    add("header can never appear, and _find_job() always returns None.")
    add("")

    for index, obj in enumerate(objectslist):
        add("-" * 72)
        add(f"OBJECT [{index}]  {type(obj).__name__}")
        add("-" * 72)
        for attr in OBJECT_ATTRS:
            if hasattr(obj, attr):
                add(f"  {attr:18} = {_describe(getattr(obj, attr, None))}")
        add(f"  has Path           = {hasattr(obj, 'Path')}")

        tool = getattr(obj, "Tool", None)
        if tool is not None:
            add("  Tool attributes:")
            for attr in TOOL_ATTRS:
                if hasattr(tool, attr):
                    add(f"    {attr:18} = "
                        f"{_describe(getattr(tool, attr, None))}")

        raw, _ = _commands_of(obj, use_placement=False)
        placed, module_name = _commands_of(obj, use_placement=True)
        if raw is None:
            add("  (no Path on this object)")
            add("")
            continue
        if isinstance(raw, str):
            add(f"  {raw}")
            add("")
            continue

        add(f"  Path.Commands      = {len(raw)} commands")
        if placed is None:
            add("  getPathWithPlacement: unavailable in this FreeCAD")
        elif isinstance(placed, list):
            differs = len(placed) != len(raw) or any(
                str(a) != str(b) for a, b in zip(raw, placed))
            add(f"  getPathWithPlacement ({module_name}): "
                f"{len(placed)} commands, "
                f"{'DIFFERS from raw' if differs else 'identical to raw'}")
            if differs:
                add("  ** the operation has a non-identity Placement **")
                for a, b in list(zip(raw, placed))[:5]:
                    add(f"     raw    {a}")
                    add(f"     placed {b}")

        add("")
        add(f"  --- raw commands of [{index}] ---")
        for n, command in enumerate(raw):
            try:
                name = getattr(command, "Name", "<no Name>")
                params = getattr(command, "Parameters", None)
                rendered = ", ".join(
                    f"{k}={v!r} <{type(v).__name__}>"
                    for k, v in sorted((params or {}).items()))
                add(f"  [{n:4}] Name={name!r:12} str={str(command)!r}")
                if rendered:
                    add(f"         Parameters: {rendered}")
            except Exception as exc:
                add(f"  [{n:4}] <undescribable: {exc}>")
        add("")

    add("-" * 72)
    add("QUESTION 2 -- how is a multi-code line tokenised?")
    add("-" * 72)
    add("Look above for any command whose Name is a bare 'G53', 'G90' or")
    add("similar with no parameters, immediately followed by the motion it")
    add("was written with. That means FreeCAD splits e.g. 'G53 G0 Z0' into")
    add("two commands, and a post that emits them separately loses the G53.")
    add("Add a Custom operation containing exactly these three lines to")
    add("make the answer unambiguous:")
    add("    G53 G0 Z0")
    add("    G0 X10 Y10 Z5")
    add("    G1 Z-1 F100")
    add("")

    add("-" * 72)
    add("QUESTION 3 -- what unit are feeds in?")
    add("-" * 72)
    add("Compare a ToolController's HorizFeed above (a Quantity, with its")
    add("UserString) against the F value in the G1 commands of the")
    add("operations that use it. F is expected to be millimetres per")
    add("SECOND, i.e. UserString/60.")
    add("")
    add("=" * 72)
    add("end of report")
    add("=" * 72)
    return "\n".join(out)


def _report_path(filename, argstring):
    """Where to write the report."""
    tokens = (argstring or "").split()
    if "--out" in tokens:
        try:
            return tokens[tokens.index("--out") + 1]
        except IndexError:
            pass
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"avid_probe_{stamp}.txt"
    if filename and filename != "-":
        return os.path.join(os.path.dirname(os.path.abspath(filename)), name)
    return os.path.join(os.path.expanduser("~"), name)


def export(objectslist, filename, argstring=""):
    """FreeCAD CAM entry point.  Writes a report; returns inert g-code."""
    if not isinstance(objectslist, (list, tuple)):
        objectslist = [objectslist]
    try:
        report = _report(objectslist, argstring)
    except Exception as exc:  # pragma: no cover - diagnostics must not fail
        import traceback

        report = f"probe failed: {exc}\n\n{traceback.format_exc()}"

    target = _report_path(filename, argstring)
    written = target
    try:
        with open(target, "w") as handle:
            handle.write(report + "\n")
    except Exception as exc:
        written = f"<could not write {target}: {exc}>"

    print("avid_probe_post: report written to", written)
    print(report)

    return "\n".join([
        "(AVID PROBE POST - DIAGNOSTIC ONLY - THIS IS NOT A PROGRAM)",
        "(DO NOT RUN THIS FILE ON A MACHINE)",
        f"(REPORT WRITTEN TO {os.path.basename(str(written)).upper()})",
        "(THE FULL PATH IS ALSO IN THE REPORT VIEW)",
        "M30",
        "",
    ])


if __name__ == "__main__":  # pragma: no cover - smoke test without FreeCAD
    class _Cmd:
        def __init__(self, name, params):
            self.Name, self.Parameters = name, params

        def __str__(self):
            return self.Name

    class _Path:
        Commands = [_Cmd("G0", {"X": 1.0}), _Cmd("G1", {"Z": -1.0, "F": 16.6})]

    class _Op:
        Name, Label, Active = "Profile", "Profile", True
        Path = _Path()

    print(export([_Op()], "-", ""))
