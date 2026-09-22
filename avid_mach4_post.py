"""AVID CNC / Mach4 post processor for the FreeCAD CAM workbench.

The g-code an AVID CNC machine running Mach4 is used to seeing has a
particular shape, and this post emits programs in that shape so FreeCAD
output runs without hand editing.  The conventions it targets:

* ``(COMMENTS LIKE THIS)`` -- upper cased and filtered down to the character
  set Mach4 accepts inside a comment.
* Numbers with trailing zeros trimmed but the decimal point always present
  (``X0.``, ``Z-1.25``, ``F60.``).
* Preamble ``G90 G94 G91.1 G40 G49 G17`` followed by ``G20``/``G21``.
* Tool changes emitted as ``M5`` / coolant off / ``M1`` / ``T<n> M6`` /
  ``S<rpm> M3`` / work offset, with ``G43 H<n>`` established before the
  first Z move of the new tool.
* ``G28 G91 Z0.`` + ``G90`` before every tool change and at program end, so
  the spindle is parked at the top of Z when a tool is swapped by hand
  (also ``G30``, ``G53``, or ``none`` -- the AVID Fusion post's own
  behaviour with ``useG28`` off).
* Arcs in incremental ``I``/``J``/``K`` form, optionally as ``R``.
* Optional dust collector support (``M7`` in the header, ``M9`` in the footer).
* Program end with ``M30``.

Every default is chosen so that a FreeCAD job posts the same way the same
job would out of Fusion, with one deliberate exception: retracts.
``tests/fixtures/avid_fusion_shape.tap`` is pinned to a real AVID/Fusion
export and reproduced with ``--safe-retracts none``.

Only the emitted g-code is modelled on what an AVID machine expects; the
implementation here is original, and the reference programs in
``tests/fixtures`` are what pin the behaviour.

The module has no hard dependency on FreeCAD so that it can be unit tested
with plain CPython; the FreeCAD specific bits are imported lazily.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import math
import os
import shlex

__all__ = ["export", "TOOLTIP", "TOOLTIP_ARGS", "UNITS"]

TOOLTIP = """
Post processor for AVID CNC machines running Mach4.  The defaults match the
g-code an AVID/Fusion export contains -- trimmed decimal numbers, M6 tool
changes with G43 applied before the first Z move, incremental arc centres
and an M30 program end -- plus a G28 retract to the top of Z before every tool
change.  Use --safe-retracts none for an AVID/Fusion program with no
retracts at all.

Import it with:

    import avid_mach4_post
    avid_mach4_post.export(object, "/path/to/file.tap", "--inches")
"""

# Mach4 only accepts this subset inside a comment; anything else is dropped.
PERMITTED_COMMENT_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,=_-"

# Characters Mach4 rejects that change the meaning of a tool name when they
# are simply dropped -- "1/4 Flat" would otherwise read as "14 FLAT", i.e. a
# 14 mm cutter.  Anything not listed here is still dropped.
COMMENT_SUBSTITUTIONS = {
    "/": "-",
    "\\": "-",
    "\u00b0": "DEG",
    "\u00d8": "D",
    '"': "IN",
    "#": "NO",
    "%": "PCT",
    "&": "AND",
}

#: Default file extension used by the AVID/Mach4 tool chain.
EXTENSION = ".tap"

#: Set by :func:`export`; FreeCAD inspects this after a run.
UNITS = "G20"

MM_PER_INCH = 25.4

# FreeCAD's base unit system is mm/kg/s, so a velocity stored in a Path
# command parameter is in mm/SECOND, not mm/minute.  Every feed has to be
# multiplied by 60 on the way out or the machine crawls at 1/60 speed.
SECONDS_PER_MINUTE = 60.0

FEED_UNIT_SCALE = {
    "mm-per-second": SECONDS_PER_MINUTE,
    "mm-per-minute": 1.0,
}

# Order in which parameters are emitted inside a block.
PARAMETER_ORDER = "XYZABCIJKRQPLSFHDT"

# Motion words whose value is suppressed while unchanged.
MOTION_CODES = {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"}
ARC_CODES = {"G2", "G02", "G3", "G03"}
CYCLE_CODES = {
    "G73", "G74", "G76", "G81", "G82", "G83", "G84", "G85", "G86", "G87",
    "G88", "G89",
}

# State setting codes that FreeCAD repeats between operations.  They are
# routed through a modal group so the output stays terse.
MODAL_GROUPS = {
    "G17": "plane_modal", "G18": "plane_modal", "G19": "plane_modal",
    "G90": "abs_inc_modal", "G91": "abs_inc_modal",
    "G93": "feed_mode_modal", "G94": "feed_mode_modal",
    "G98": "retract_modal", "G99": "retract_modal",
}

WORK_OFFSET_CODES = {"G54", "G55", "G56", "G57", "G58", "G59"} | {
    f"G59.{n}" for n in range(1, 10)
}

# Unit words the post refuses to pass on.  Every number it writes is
# already scaled to the unit chosen by --inches/--metric and announced in
# the preamble; letting a G21 through in an inch program would have Mach4
# read "X1." as one millimetre.
UNIT_CODES = {"G20", "G21"}

# Codes that move the coordinate frame out from under the program without
# moving the machine.  A cached axis word is only safe to suppress while it
# still means the same physical place, so each of these invalidates the
# cache -- see AvidPost.invalidate_axis_cache.
FRAME_CHANGING_CODES = {"G10", "G92", "G92.1", "G92.2", "G92.3"}


# --------------------------------------------------------------------------
# number / word formatting
# --------------------------------------------------------------------------
class Formatter:
    """Turns a number into a g-code word.

    ``trim`` removes trailing zeros, ``force_decimal`` guarantees a decimal
    point is present, which together produce the ``X0.`` / ``Z-1.25`` style
    an AVID machine expects.
    """

    def __init__(self, decimals=4, prefix="", scale=1.0, force_decimal=True,
                 trim=True, zeropad=0):
        self.decimals = decimals
        self.prefix = prefix
        self.scale = scale
        self.force_decimal = force_decimal
        self.trim = trim
        self.zeropad = zeropad

    def format(self, value):
        """Return ``value`` as a formatted word (including the prefix)."""
        if value is None:
            return ""
        number = float(value) * self.scale
        text = "{:.{}f}".format(number, self.decimals)
        if text.startswith("-") and float(text) == 0.0:
            text = text[1:]  # avoid "-0."
        if self.decimals > 0 and self.trim:
            text = text.rstrip("0")
            if text.endswith("."):
                text = text if self.force_decimal else text[:-1]
        elif self.decimals > 0 and not self.force_decimal:
            pass
        if self.force_decimal and "." not in text:
            text += "."
        if self.zeropad:
            negative = text.startswith("-")
            body = text[1:] if negative else text
            body = body.rjust(self.zeropad, "0")
            text = ("-" if negative else "") + body
        return self.prefix + text

    def are_different(self, a, b):
        """True when the two values do not format to the same string."""
        return self.format(a) != self.format(b)


class OutputVariable:
    """A word whose value is only emitted when it changes.

    ``format`` returns an empty string while the value is unchanged, unless
    the variable was created with ``force`` or has been ``reset``.
    """

    def __init__(self, formatter, force=False, enabled=True):
        self.formatter = formatter
        self.force = force
        self.enabled = enabled
        self.current = None

    def reset(self):
        self.current = None

    def disable(self):
        self.enabled = False

    def get_current(self):
        return self.current

    def format(self, value):
        if value is None or not self.enabled:
            return ""
        text = self.formatter.format(value)
        if not self.force and self.current is not None and \
                text == self.current:
            return ""
        self.current = text
        return text


class Modal(OutputVariable):
    """A modal g-code group: only emitted when the active code changes."""

    def format(self, value):
        if value is None:
            return ""
        text = value if isinstance(value, str) \
            else self.formatter.format(value)
        if self.current is not None and text == self.current:
            return ""
        self.current = text
        return text


def format_comment(text):
    """Return ``text`` as a Mach4 safe parenthesised comment.

    Mach4 rejects anything outside :data:`PERMITTED_COMMENT_CHARS` inside
    parentheses -- including nested ``()`` -- so unsupported characters are
    either transliterated through :data:`COMMENT_SUBSTITUTIONS` or dropped.
    """
    expanded = "".join(
        COMMENT_SUBSTITUTIONS.get(c, c) for c in str(text).upper()
    )
    cleaned = "".join(c for c in expanded if c in PERMITTED_COMMENT_CHARS)
    return "(" + cleaned + ")"


# --------------------------------------------------------------------------
# argument handling
# --------------------------------------------------------------------------
def _build_parser():
    parser = argparse.ArgumentParser(prog="avid_mach4", add_help=False)

    def flag(name, dest, default, help_on, help_off):
        parser.add_argument("--" + name, dest=dest, action="store_true",
                            help=help_on)
        parser.add_argument("--no-" + name, dest=dest, action="store_false",
                            help=help_off)
        parser.set_defaults(**{dest: default})

    flag("comments", "comments", True,
         "output comments (default)", "suppress all comments")
    flag("header", "header", False,
         "write a generated-by and timestamp header",
         "no generated-by header, as the AVID Fusion post does (default)")
    flag("write-machine", "write_machine", True,
         "write the machine description into the header (default)",
         "do not write the machine description")
    flag("write-tools", "write_tools", True,
         "write the tool list into the header (default)",
         "do not write the tool list")
    flag("line-numbers", "line_numbers", False,
         "prefix every block with an N word", "no N words (default)")
    flag("spaces", "spaces", True,
         "separate words with a space (default)", "pack words together")
    flag("modal", "modal", True,
         "suppress repeated motion codes and coordinates (default)",
         "repeat every word in every block")
    flag("use-m6", "use_m6", True,
         "emit M6 on tool changes (default)",
         "emit the T word only, without M6")
    flag("preload-tool", "preload_tool", False,
         "preload the next tool at a tool change",
         "do not preload the next tool (default)")
    flag("optional-stop", "optional_stop", True,
         "emit M1 before every tool change but the first (default)",
         "never emit M1")
    flag("tool-length-offset", "tool_length_offset", True,
         "emit G43 H<n> on the first Z move after a tool change (default)",
         "never emit G43")
    flag("radius-arcs", "radius_arcs", False,
         "emit arcs using an R word",
         "emit arcs using I/J/K (default)")
    flag("dust-collector", "dust_collector", False,
         "M7 in the header and M9 in the footer for the dust collector",
         "no dust collector codes (default)")
    flag("coolant", "coolant", True,
         "translate the operation coolant mode into M7/M8/M9 (default)",
         "ignore the operation coolant mode")
    flag("show-editor", "show_editor", True,
         "show the g-code in an editor when running inside FreeCAD (default)",
         "never open the editor")

    parser.add_argument("--inches", dest="inches", action="store_true",
                        help="output in inches, G20 (default)")
    parser.add_argument("--metric", dest="inches", action="store_false",
                        help="output in millimetres, G21")
    parser.set_defaults(inches=True)

    parser.add_argument(
        "--dwell-in-seconds", dest="dwell_in_seconds", action="store_true",
        help="G4 dwells are expressed in seconds (default)")
    parser.add_argument(
        "--dwell-in-milliseconds", dest="dwell_in_seconds",
        action="store_false", help="G4 dwells are expressed in milliseconds")
    parser.set_defaults(dwell_in_seconds=True)

    parser.add_argument(
        "--feed-units", dest="feed_units", default="mm-per-second",
        choices=sorted(FEED_UNIT_SCALE),
        help="unit of the F parameter in the incoming path; FreeCAD stores "
             "velocities in mm/second (default: mm-per-second)")
    parser.add_argument(
        "--safe-retracts", dest="safe_retracts", default="g28",
        choices=["g28", "g30", "g53", "none"],
        help="how to retract between operations and at program end: g28/g30 "
             "send the axes home incrementally, g53 moves to --home-x/y/z in "
             "machine coordinates, none emits no retract at all and leaves "
             "each operation's own clearance-height move to do the job -- "
             "which is what the AVID Fusion post does with its useG28 "
             "property off (default: g28, so that a tool change happens "
             "with the spindle parked at the top of Z)")
    parser.add_argument("--line-number-start", dest="line_number_start",
                        type=int, default=10,
                        help="first N number (default: 10)")
    parser.add_argument("--line-number-increment",
                        dest="line_number_increment", type=int, default=5,
                        help="N number increment (default: 5)")
    parser.add_argument("--precision", dest="precision", type=int,
                        default=None,
                        help="decimal places for linear axes "
                             "(default: 4 for inches, 3 for millimetres)")
    parser.add_argument("--home-x", dest="home_x", type=float, default=0.0,
                        help="X machine home used by --safe-retracts g53, "
                             "in MILLIMETRES whatever the output unit")
    parser.add_argument("--home-y", dest="home_y", type=float, default=0.0,
                        help="Y machine home used by --safe-retracts g53, "
                             "in MILLIMETRES whatever the output unit")
    parser.add_argument("--home-z", dest="home_z", type=float, default=0.0,
                        help="Z machine position --safe-retracts g53 "
                             "retracts to, in MILLIMETRES whatever the "
                             "output unit; 0 is machine zero, which on an "
                             "AVID is the top of Z after homing")
    parser.add_argument("--preamble", dest="preamble", default="",
                        help="blocks emitted after the header, ';' separated")
    parser.add_argument("--postamble", dest="postamble", default="",
                        help="blocks emitted before M30, ';' separated")
    return parser


PARSER = _build_parser()
TOOLTIP_ARGS = PARSER.format_help()


def process_arguments(argstring):
    """Parse ``argstring`` and return the namespace, or ``None`` on error.

    ``shlex`` raises on an unbalanced quote, which reaches FreeCAD as a
    traceback rather than as "your arguments are wrong"; both failures are
    reported the same way.
    """
    try:
        return PARSER.parse_args(shlex.split(argstring or ""))
    except (SystemExit, ValueError):
        return None


# --------------------------------------------------------------------------
# the post processor itself
# --------------------------------------------------------------------------
class AvidPost:
    """Translates a FreeCAD command stream into AVID/Mach4 g-code."""

    def __init__(self, args):
        self.args = args
        self.lines = []
        self.sequence_number = args.line_number_start
        self.separator = " " if args.spaces else ""

        inches = args.inches
        precision = args.precision
        if precision is None:
            precision = 4 if inches else 3
        scale = 1.0 / MM_PER_INCH if inches else 1.0
        feed_scale = scale * FEED_UNIT_SCALE[args.feed_units]

        self.xyz_format = Formatter(decimals=precision, scale=scale)
        self.feed_format = Formatter(decimals=1 if inches else 0,
                                     scale=feed_scale)
        self.abc_format = Formatter(decimals=3)
        self.rpm_format = Formatter(decimals=0, force_decimal=False)
        self.int_format = Formatter(decimals=0, force_decimal=False)
        self.sec_format = Formatter(decimals=3)
        self.milli_format = Formatter(decimals=0, force_decimal=False)
        self.n_format = Formatter(decimals=0, prefix="N", force_decimal=False)

        self.x_output = OutputVariable(Formatter(decimals=precision,
                                                 prefix="X", scale=scale))
        self.y_output = OutputVariable(Formatter(decimals=precision,
                                                 prefix="Y", scale=scale))
        self.z_output = OutputVariable(Formatter(decimals=precision,
                                                 prefix="Z", scale=scale))
        self.a_output = OutputVariable(Formatter(decimals=3, prefix="A"))
        self.b_output = OutputVariable(Formatter(decimals=3, prefix="B"))
        self.c_output = OutputVariable(Formatter(decimals=3, prefix="C"))
        self.feed_output = OutputVariable(
            Formatter(decimals=1 if inches else 0, prefix="F",
                      scale=feed_scale))
        self.r_output = OutputVariable(Formatter(decimals=precision,
                                                 prefix="R", scale=scale))
        self.q_output = OutputVariable(Formatter(decimals=precision,
                                                 prefix="Q", scale=scale))
        self.axis_outputs = {
            "X": self.x_output, "Y": self.y_output, "Z": self.z_output,
            "A": self.a_output, "B": self.b_output, "C": self.c_output,
        }

        self.motion_modal = Modal(Formatter(decimals=1, prefix="G",
                                            force_decimal=False))
        self.plane_modal = Modal(Formatter(decimals=1, prefix="G",
                                           force_decimal=False))
        self.abs_inc_modal = Modal(Formatter(decimals=1, prefix="G",
                                             force_decimal=False))
        self.feed_mode_modal = Modal(Formatter(decimals=1, prefix="G",
                                               force_decimal=False))
        self.unit_modal = Modal(Formatter(decimals=1, prefix="G",
                                          force_decimal=False))
        self.cycle_modal = Modal(Formatter(decimals=1, prefix="G",
                                           force_decimal=False))
        self.retract_modal = Modal(Formatter(decimals=1, prefix="G",
                                             force_decimal=False))

        # collected state
        self.position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.current_tool = None
        self.current_coolant = "None"
        self.current_work_offset = None
        # the offset the command stream last asked for.  A tool change
        # clears current_work_offset to force the word back out, but must
        # not lose which offset the job is actually running in.
        self.active_work_offset = None
        self.retracted = False
        self.first_section = True
        self.tool_changes = 0
        self.pending_tool_length_offset = False
        self.pending_coolant = None
        self.tool_numbers = []
        self.section_comment = None
        self.section_tool_number = None

    # -- low level output ---------------------------------------------------
    def write(self, text):
        self.lines.append(text)

    def write_block(self, *words):
        """Emit a block, skipping empty words and adding the N word."""
        flat = []
        for word in words:
            if isinstance(word, (list, tuple)):
                flat.extend(w for w in word if w)
            elif word:
                flat.append(word)
        if not flat:
            return
        text = self.separator.join(flat)
        if self.args.line_numbers:
            text = self.n_format.format(self.sequence_number % 100000) + \
                self.separator + text
            self.sequence_number += self.args.line_number_increment
        self.write(text)

    def write_comment(self, text):
        if self.args.comments:
            self.write(format_comment(text))

    def write_blank(self):
        self.write("")

    # -- retracts -----------------------------------------------------------
    def invalidate_axis_cache(self):
        """Forget every cached axis word.

        Modal suppression is only sound while a word means the same place
        it did last time it was written.  Anything that re-frames the
        coordinate system -- a different fixture, a switch between absolute
        and incremental, a tool length offset, a ``G92`` -- has to come
        through here, or the next move that happens to repeat a number is
        silently dropped and the tool cuts somewhere else.
        """
        for output in self.axis_outputs.values():
            output.reset()

    def write_absolute_mode(self, code):
        """Emit ``G90``/``G91``, if the control is not in that mode already.

        A real switch invalidates every cached axis word -- an absolute and
        an incremental value are not comparable, so suppressing "the same"
        word would drop a move.
        """
        text = self.abs_inc_modal.format(code)
        if text:
            self.invalidate_axis_cache()
        return text

    def write_retract(self, *axes):
        """Send the named axes to a safe position.

        With ``g28``/``g30`` the machine is sent home in incremental mode;
        with ``g53`` the axes move in machine coordinates, Z to
        ``--home-z``.  Z always moves in a block of its own: sending Z and
        XY home together would have the tool climb out of the work along a
        diagonal.
        """
        if self.args.safe_retracts == "none":
            # no retract of any kind, matching the AVID Fusion post with
            # useG28 off: the tool stays at whatever clearance height the
            # operation itself ended on
            return
        axes = [a.upper() for a in axes]
        use_g28 = self.args.safe_retracts in ("g28", "g30")

        if not use_g28 and "Z" in axes and ("X" in axes or "Y" in axes):
            raise ValueError("cannot move home in XY and Z in the same block")

        homes = {"X": self.args.home_x, "Y": self.args.home_y,
                 "Z": self.args.home_z}
        if use_g28:
            homes = {"X": 0.0, "Y": 0.0, "Z": 0.0}

        words = []
        for axis in axes:
            words.append(axis + self.xyz_format.format(homes[axis]))
            if axis == "Z":
                self.retracted = True
        if not words:
            return

        if use_g28:
            self.abs_inc_modal.reset()
            code = "G30" if self.args.safe_retracts == "g30" else "G28"
            self.write_block(code, self.write_absolute_mode(91), *words)
            self.write_block(self.write_absolute_mode(90))
        else:
            self.motion_modal.reset()
            self.write_block(self.write_absolute_mode(90), "G53",
                             self.motion_modal.format(0), *words)

        for axis in axes:
            self.axis_outputs[axis].reset()

    # -- header / preamble --------------------------------------------------
    def write_header(self, job, operations, now=None):
        if self.args.header and self.args.comments:
            now = now or datetime.datetime.now()
            self.write_comment("Exported by FreeCAD")
            self.write_comment("Post processor: avid_mach4_post")
            self.write_comment("Output time: " +
                               now.strftime("%Y-%m-%d %H:%M:%S"))
        if self.args.comments and job is not None:
            label = getattr(job, "Label", None)
            if label:
                self.write_comment(label)

        if self.args.write_machine and self.args.comments:
            self._write_machine(job)
        if self.args.write_tools and self.args.comments:
            self._write_tools(operations)

    def _write_machine(self, job):
        machine = getattr(job, "Machine", None) if job is not None else None
        vendor = getattr(machine, "Vendor", None) or ""
        model = getattr(machine, "Model", None) or ""
        description = getattr(machine, "Description", None) or ""
        if not (vendor or model or description):
            # the Fusion post writes no machine block when the job does not
            # name one, and inventing a vendor here would be a guess
            return
        self.write_comment("Machine")
        if vendor:
            self.write_comment("  vendor: " + str(vendor))
        if model:
            self.write_comment("  model: " + str(model))
        if description:
            self.write_comment("  description: " + str(description))
        self.write_comment("  control: Mach4")

    def _write_tools(self, operations):
        for tool in collect_tools(operations):
            comment = "T" + self.int_format.format(tool["number"]) + "  D=" + \
                self.xyz_format.format(tool["diameter"])
            if tool["corner_radius"] is not None:
                comment += " CR=" + \
                    self.xyz_format.format(tool["corner_radius"])
            if tool["zmin"] is not None:
                comment += " - ZMIN=" + self.xyz_format.format(tool["zmin"])
            if tool["name"]:
                comment += " - " + str(tool["name"])
            self.write_comment(comment)

    def write_preamble(self):
        self.write_block(self.abs_inc_modal.format(90),
                         self.feed_mode_modal.format(94),
                         "G91.1", "G40", "G49",
                         self.plane_modal.format(17))
        self.write_block(self.unit_modal.format(20 if self.args.inches
                                                else 21))
        global UNITS
        UNITS = "G20" if self.args.inches else "G21"
        if self.args.dust_collector:
            self.write_block("M7")
        for block in _split_blocks(self.args.preamble):
            self.write_block(block)

    def write_postamble(self):
        self.write_blank()
        if self.args.dust_collector:
            self.write_block("M9")
        elif self.current_coolant != "None":
            self.write_block("M9")
            self.current_coolant = "None"
        # unconditionally, even if the last operation ended parked: this is
        # the block that guarantees the tool is clear before the machine
        # traverses to home, and it costs one redundant line to not have to
        # trust the post's own position tracking here
        self.write_retract("Z")
        self.write_retract("X", "Y")
        for block in _split_blocks(self.args.postamble):
            self.write_block(block)
        self.write_block("M30")

    # -- sections -----------------------------------------------------------
    def begin_section(self, operation):
        """Emit the retract, blank line and operation comment for an op."""
        controller = _resolve_controller(operation)
        number = getattr(controller, "ToolNumber", None)
        self.section_tool_number = None if number is None else int(number)
        commands = list(iter_commands(operation))
        tool_change = any(_command_name(c) in ("M6", "M06") for c in commands)
        if (tool_change or self.first_section) and not self.retracted:
            self.write_retract("Z")
        self.write_blank()
        label = getattr(operation, "Label", None) or \
            getattr(operation, "Name", "")
        self.section_comment = format_comment(label) if label else None
        if label:
            self.write_comment(label)
        # every operation FreeCAD generates assumes it starts in absolute
        # mode; a previous Custom op may have left the stream in G91
        self.write_block(self.write_absolute_mode(90))
        if self.args.coolant and not self.args.dust_collector:
            self.pending_coolant = coolant_mode(operation)
        return commands

    def write_tool_change(self, tool_number):
        # begin_section retracts ahead of an operation that opens with M6;
        # an M6 that turns up part way through one has had no retract, and
        # the tool is wherever the last cut left it
        if not self.retracted:
            self.write_retract("Z")
        self.write_block("M5")
        if self.current_coolant != "None" and not self.args.dust_collector:
            self.write_block("M9")
            self.current_coolant = "None"
        if self.tool_changes and self.args.optional_stop:
            self.write_block("M1")
        self.tool_changes += 1

        tool_word = "T" + self.int_format.format(tool_number)
        if self.args.use_m6:
            self.write_block(tool_word, "M6")
        else:
            self.write_block(tool_word)

        if self.args.preload_tool and self.args.use_m6:
            nxt = self._next_tool(tool_number)
            if nxt is not None:
                self.write_block("T" + self.int_format.format(nxt))

        self.current_tool = tool_number
        # the work offset is written out again after a tool change
        self.current_work_offset = None
        if self.args.tool_length_offset:
            self.pending_tool_length_offset = True
        # a tool change invalidates every modal value on the control
        self.motion_modal.reset()
        self.feed_output.reset()
        for output in self.axis_outputs.values():
            output.reset()

    def _next_tool(self, tool_number):
        try:
            index = self.tool_numbers.index(tool_number)
        except ValueError:
            return None
        for candidate in self.tool_numbers[index + 1:]:
            if candidate != tool_number:
                return candidate
        first = self.tool_numbers[0] if self.tool_numbers else None
        return first if first != tool_number else None

    def write_work_offset(self, code=None, p=None):
        """Emit the work offset when the control does not already have it.

        Called with no arguments before the first motion of a section, to
        restate whatever offset the job is running in -- a tool change
        clears the control's copy.  ``G54`` is the fallback only when the
        command stream never named one; restating a hard-coded ``G54``
        there would silently move a G55 job onto the G54 fixture.
        """
        if code is None:
            if self.current_work_offset is not None:
                return
            key = self.active_work_offset or "G54"
        else:
            key = code if p is None else \
                f"{code} P{self.int_format.format(p)}"
            self.active_work_offset = key
        if key == self.current_work_offset:
            return
        self.invalidate_axis_cache()
        self.current_work_offset = key
        self.write_block(*key.split(" "))

    def flush_coolant(self):
        if self.pending_coolant is None:
            return
        mode = str(self.pending_coolant)
        self.pending_coolant = None
        if mode == self.current_coolant:
            return
        if mode == "None":
            # the previous operation left it running; this one asked for it
            # off, which used to be silently ignored
            self.write_block("M9")
            self.current_coolant = "None"
            return
        code = COOLANT_CODES.get(mode)
        if code is None:
            return
        self.write_block(code)
        self.current_coolant = mode

    # -- command translation ------------------------------------------------
    def parse_command(self, command):
        name = _command_name(command)
        if not name:
            return
        if name in UNIT_CODES:
            # the preamble already stated the unit this program is written
            # in, and every number is scaled to it
            return
        if name.startswith("("):
            # FreeCAD operations often repeat their own label as the first
            # comment of the path; the section header already carries it
            if format_comment(name.strip("()")) == self.section_comment:
                self.section_comment = None
                return
            self.write_comment(name.strip("()"))
            return

        params = _command_parameters(command)

        if name in ("M6", "M06"):
            tool = params.get("T")
            if tool is None:
                tool = self.section_tool_number
            if tool is None:
                tool = self.current_tool
            if tool is None:
                # no T anywhere: emitting "T0 M6" would order the machine to
                # unload the spindle, so leave the tool alone
                self.write_comment("tool change with no tool number")
                return
            self.write_tool_change(int(tool))
            return
        if name in ("M3", "M03", "M4", "M04"):
            speed = params.get("S")
            words = []
            if speed is not None:
                words.append("S" + self.rpm_format.format(speed))
            words.append("M3" if name in ("M3", "M03") else "M4")
            self.write_block(*words)
            self.flush_coolant()
            return
        if name in ("M7", "M07", "M8", "M08", "M9", "M09"):
            if not self.args.coolant or self.args.dust_collector:
                return
            self.pending_coolant = None
            self.write_block(name)
            self.current_coolant = {"M7": "Mist", "M07": "Mist",
                                    "M8": "Flood", "M08": "Flood"}.get(
                                        name, "None")
            return
        if name in ("G4", "G04", "G04.1"):
            self.write_dwell(params)
            return
        if name in ("G43", "G49"):
            # the Z frame moves under the program's feet; a cached Z word
            # is no longer the height it was
            self.pending_tool_length_offset = False
            self.z_output.reset()
            if name == "G49":
                self.write_block("G49")
            else:
                self.write_block("G43", self._h_word(params))
            return
        if name in WORK_OFFSET_CODES:
            self.write_work_offset(name, params.get("P"))
            return
        if name in MODAL_GROUPS:
            # the bookkeeping runs either way: --no-modal only decides
            # whether the word is repeated, not whether the post knows
            # which plane or coordinate mode the control is in
            if name in ("G90", "G91"):
                text = self.write_absolute_mode(float(name[1:]))
            else:
                modal = getattr(self, MODAL_GROUPS[name])
                text = modal.format(float(name[1:]))
            self.write_block(name if not self.args.modal else text)
            return
        if name in MOTION_CODES:
            self.write_work_offset()
            self.flush_coolant()
            self.write_motion(name, params)
            return
        if name in CYCLE_CODES:
            self.write_work_offset()
            self.flush_coolant()
            self.write_cycle(name, params)
            return
        if name in FRAME_CHANGING_CODES:
            self.invalidate_axis_cache()
            self.write_block(self._passthrough(name, params))
            return
        if name == "G80":
            self.cycle_modal.reset()
            # G80 leaves motion group 1 empty; the next move has to name
            # its own code or the control sees a bare axis word.  G98/G99
            # is a different group and is not cancelled here.
            self.motion_modal.reset()
            self.z_output.reset()
            self.r_output.reset()
            self.q_output.reset()
            self.write_block("G80")
            return

        if "Z" in params:
            self.apply_tool_length_offset()
        self.write_block(self._passthrough(name, params))

    def write_dwell(self, params):
        seconds = params.get("P", params.get("S", 0.0)) or 0.0
        if self.args.dwell_in_seconds:
            self.write_block("G4", "P" + self.sec_format.format(seconds))
        else:
            millis = min(max(seconds * 1000.0, 1.0), 99999999.0)
            self.write_block("G4", "P" + self.milli_format.format(millis))

    def write_motion(self, name, params):
        code = int(float(name[1:]))
        if code in (2, 3):
            self.write_arc(code, params)
            return
        self.write_linear(code, params)

    def write_linear(self, code, params):
        if self.pending_tool_length_offset and "Z" in params:
            # G43 rides the first Z bearing move after a tool change, once
            # XY has been positioned: "G0 X.. Y.." then "G43 Z0.2 H1".  It
            # is deliberately not restricted to a rapid -- an operation
            # whose first Z move is the plunge would otherwise cut the whole
            # pass with the previous tool's offset still in force.
            self.pending_tool_length_offset = False
            self.z_output.reset()
            other = self._coordinate_words(
                {k: v for k, v in params.items() if k != "Z"})
            feed = self._feed_word(params) if code != 0 else ""
            self.write_block(self._motion_word(code), "G43", other,
                             self.z_output.format(params["Z"]),
                             self._h_word({}), feed)
            self._update_position(params)
            if code == 0:
                self.feed_output.reset()
            return

        coords = self._coordinate_words(params)
        if not coords:
            # the block commands no movement, so nothing is emitted and
            # nothing about the control's state has changed.  Claiming
            # motion group 1 here would let the *next* block inherit a mode
            # the machine was never put into, and consuming the feed would
            # leave a stray "F220." block behind -- FreeCAD really does emit
            # bare "G0" commands with no axis words at all.
            return
        feed = self._feed_word(params) if code != 0 else ""
        words = [self._motion_word(code)] + coords
        if feed:
            words.append(feed)
        self.write_block(*words)
        self._update_position(params)
        if code == 0:
            self.feed_output.reset()

    def apply_tool_length_offset(self):
        """Establish a pending ``G43`` in a block of its own.

        :meth:`write_linear` hangs the offset off the Z word it is already
        writing, which is the tidier form.  Everything else that can move Z
        -- a helical arc, a canned cycle, a probe passed straight through --
        has to state it separately, or the move runs on the *previous*
        tool's offset and G43 lands afterwards with a Z jump.
        """
        if not self.pending_tool_length_offset:
            return
        self.pending_tool_length_offset = False
        self.write_block("G43", self._h_word({}))

    def _motion_word(self, code):
        """Claim motion group 1 for ``code``; a canned cycle no longer has it.

        Only ever called for a block that is actually going to be written.
        """
        # group 1 only: G98/G99 live in group 10 and survive a G0/G1
        self.cycle_modal.reset()
        if not self.args.modal:
            self.motion_modal.reset()
        return self.motion_modal.format(code)

    def write_arc(self, code, params):
        self.apply_tool_length_offset()
        start = dict(self.position)
        i = params.get("I", 0.0) or 0.0
        j = params.get("J", 0.0) or 0.0
        k = params.get("K")
        if self._is_incremental():
            # the X/Y in the block are deltas; comparing one against an
            # absolute start turned a half circle into a full one
            end_x = start["X"] + (params.get("X", 0.0) or 0.0)
            end_y = start["Y"] + (params.get("Y", 0.0) or 0.0)
        else:
            end_x = params.get("X", start["X"])
            end_y = params.get("Y", start["Y"])
        full_circle = (
            self.xyz_format.format(end_x) == self.xyz_format.format(start["X"])
            and self.xyz_format.format(end_y) ==
            self.xyz_format.format(start["Y"])
        )

        words = [self._plane_word()]
        words.append(self._motion_word(code))
        if full_circle:
            # a full circle needs no end point in the arc plane -- but a
            # helix still has to carry its Z, or the turn comes out flat
            # and every Z after it is measured from a height the machine
            # never reached
            words.extend(self._coordinate_words(
                {k2: v for k2, v in params.items() if k2 not in ("X", "Y")}))
        else:
            words.extend(self._coordinate_words(params))

        in_xy_plane = self.plane_modal.get_current() in (None, "G17")
        if "R" in params and "I" not in params and "J" not in params:
            # the path describes the arc by its radius; inventing I0. J0.
            # from the missing centre would make it a zero radius arc
            words.append("R" + self.xyz_format.format(params["R"]))
        elif self.args.radius_arcs and not full_circle and in_xy_plane:
            radius = math.hypot(i, j)
            sweep = _arc_sweep(start["X"], start["Y"], start["X"] + i,
                               start["Y"] + j, end_x, end_y, code == 2)
            if sweep > math.pi + 1e-9:
                radius = -radius
            words.append("R" + self.xyz_format.format(radius))
        elif in_xy_plane:
            words.append("I" + self.xyz_format.format(i))
            words.append("J" + self.xyz_format.format(j))
            if k is not None:
                words.append("K" + self.xyz_format.format(k))
        else:
            # outside G17 the centre is described by a different pair of
            # letters; pass through exactly what the path carries
            for letter in ("I", "J", "K"):
                if letter in params:
                    words.append(
                        letter + self.xyz_format.format(params[letter]))

        feed = self._feed_word(params)
        if feed:
            words.append(feed)
        self.write_block(*words)
        self._update_position(params)

    def _plane_word(self):
        """Return the arc plane word, without overriding an active plane."""
        if self.plane_modal.get_current() is None:
            return self.plane_modal.format(17)
        if not self.args.modal:
            return self.plane_modal.get_current()
        return ""

    def write_cycle(self, name, params):
        """Emit a canned cycle block.

        The first point of a cycle carries the full definition; the ones
        after it only carry what changed, which is how repeated holes are
        expected to read.
        """
        # nothing in a cycle carries a plain Z move for G43 to ride, so the
        # offset has to be established before the first hole
        self.apply_tool_length_offset()
        # a canned cycle takes over motion group 1 from G0/G1/G2/G3
        self.motion_modal.reset()
        if self.retract_modal.get_current() is None:
            retract_word = self.retract_modal.format(98)
        elif self.args.modal:
            retract_word = ""
        else:
            retract_word = self.retract_modal.get_current()
        cycle_word = self.cycle_modal.format(float(name[1:])) \
            if self.args.modal else name
        first_point = bool(cycle_word) or not self.args.modal

        # under G91 every hole is a delta from the last one, so two holes
        # that share a word are two holes, not one
        restate = first_point or self._is_incremental()
        words = [retract_word, cycle_word]
        for axis in ("X", "Y", "Z"):
            if axis not in params:
                continue
            value = params[axis]
            if restate:
                self.axis_outputs[axis].reset()
            words.append(self.axis_outputs[axis].format(value))
        for letter, output in (("R", self.r_output), ("Q", self.q_output)):
            if letter not in params:
                continue
            if restate:
                output.reset()
            words.append(output.format(params[letter]))
        if "P" in params and (first_point or params["P"]):
            words.append("P" + self.sec_format.format(params["P"]))
        if "L" in params:
            words.append("L" + self.int_format.format(params["L"]))
        feed = self._feed_word(params)
        if feed:
            words.append(feed)

        words = [w for w in words if w]
        if not words:
            return
        if not any(w[0] in "XYZ" for w in words):
            # a cycle line needs at least one axis word
            axis = "X" if "X" in params else "Y"
            if axis in params:
                self.axis_outputs[axis].reset()
                words.append(self.axis_outputs[axis].format(params[axis]))
        self.write_block(*words)
        self._update_position(params)

    # -- helpers ------------------------------------------------------------
    def _is_incremental(self):
        """True while the stream has put the control into ``G91``."""
        return self.abs_inc_modal.get_current() == "G91"

    def _coordinate_words(self, params):
        """Format the axis words of a block, dropping unchanged ones."""
        incremental = self._is_incremental()
        words = []
        for axis in ("X", "Y", "Z", "A", "B", "C"):
            if axis not in params:
                continue
            output = self.axis_outputs[axis]
            if not self.args.modal or incremental:
                # two identical incremental words are two separate moves,
                # so suppressing the second one loses one of them
                output.reset()
            words.append(output.format(params[axis]))
        return [w for w in words if w]

    def _feed_word(self, params):
        if "F" not in params:
            return ""
        if self.args.modal:
            return self.feed_output.format(params["F"])
        self.feed_output.reset()
        return self.feed_output.format(params["F"])

    def _h_word(self, params):
        value = params.get("H", self.current_tool)
        if value is None:
            return ""
        return "H" + self.int_format.format(value)

    def _passthrough(self, name, params):
        words = [name]
        for letter in PARAMETER_ORDER:
            if letter not in params:
                continue
            value = params[letter]
            if letter in "XYZIJKRQ":
                words.append(letter + self.xyz_format.format(value))
            elif letter in "ABC":
                words.append(letter + self.abc_format.format(value))
            elif letter == "F":
                words.append("F" + self.feed_format.format(value))
            elif letter == "S":
                words.append("S" + self.rpm_format.format(value))
            elif letter == "P":
                words.append("P" + self.sec_format.format(value))
            else:
                words.append(letter + self.int_format.format(value))
        self._update_position(params)
        return words

    def _update_position(self, params):
        if "Z" in params:
            # commanding Z means the tool is no longer parked at the
            # retract plane
            self.retracted = False
        incremental = self._is_incremental()
        for axis in ("X", "Y", "Z"):
            if axis in params and params[axis] is not None:
                if incremental:
                    self.position[axis] += float(params[axis])
                else:
                    self.position[axis] = float(params[axis])

    # -- driver -------------------------------------------------------------
    def build(self, objectslist, now=None):
        job = _find_job(objectslist)
        operations = [obj for obj in objectslist if _is_path_object(obj)
                      and getattr(obj, "Active", True) is not False
                      and obj is not job]
        self.tool_numbers = [t["number"] for t in collect_tools(operations)]

        self.write_header(job, operations, now=now)
        self.write_preamble()

        for operation in operations:
            commands = self.begin_section(operation)
            for command in commands:
                self.parse_command(command)
            self.first_section = False
            self.feed_output.reset()

        self.write_postamble()
        return "\n".join(self.lines) + "\n"


COOLANT_CODES = {
    "None": None,
    "Flood": "M8",
    "Mist": "M7",
}


def _split_blocks(text):
    return [b.strip() for b in str(text or "").split(";") if b.strip()]


def _arc_sweep(sx, sy, cx, cy, ex, ey, clockwise):
    """Return the swept angle of an arc in radians (0 .. 2*pi)."""
    start = math.atan2(sy - cy, sx - cx)
    end = math.atan2(ey - cy, ex - cx)
    sweep = (start - end) if clockwise else (end - start)
    while sweep <= 0:
        sweep += 2 * math.pi
    while sweep > 2 * math.pi:
        sweep -= 2 * math.pi
    return sweep


# --------------------------------------------------------------------------
# FreeCAD object helpers (duck typed so they work with test doubles)
# --------------------------------------------------------------------------
def _is_path_object(obj):
    return hasattr(obj, "Path") and hasattr(obj.Path, "Commands")


def _find_job(objectslist):
    for obj in objectslist:
        if getattr(obj, "Name", "").startswith("Job"):
            return obj
    for obj in objectslist:
        parent = getattr(obj, "Job", None)
        if parent is not None:
            return parent
    return None


def iter_commands(operation):
    """Return an operation's commands, with its ``Placement`` applied.

    Every post FreeCAD ships reads its paths through
    ``getPathWithPlacement`` rather than ``Path.Commands``: an operation
    carries a Placement, and reading the commands raw posts it at the
    wrong coordinates whenever that Placement is not the identity.  The
    import is lazy and optional so the module still loads without FreeCAD.
    """
    path = getattr(operation, "Path", None)
    if path is None:
        return []
    if getattr(operation, "Placement", None) is not None:
        placed = _apply_placement(operation)
        if placed is not None:
            path = placed
    return list(path.Commands)


def _apply_placement(operation):
    """Return ``operation``'s Path with its Placement applied, or None."""
    for module_name in ("PathScripts.PathUtils", "Path.Base.Util"):
        try:  # pragma: no cover - requires FreeCAD
            module = importlib.import_module(module_name)
            return module.getPathWithPlacement(operation)
        except Exception:
            continue
    return None


def _command_name(command):
    return str(getattr(command, "Name", "") or "").strip()


def _command_parameters(command):
    params = getattr(command, "Parameters", None) or {}
    return {str(k).upper(): v for k, v in params.items()}


def coolant_mode(operation):
    mode = getattr(operation, "CoolantMode", None)
    if mode is None:
        base = getattr(operation, "Base", None)
        mode = getattr(base, "CoolantMode", None)
    return str(mode) if mode is not None else "None"


def _quantity_value(value):
    """Return a plain float from either a number or a FreeCAD Quantity."""
    if value is None:
        return None
    for attr in ("Value",):
        if hasattr(value, attr):
            return float(getattr(value, attr))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_controller(operation):
    """Return the ToolController behind an object in ``objectslist``.

    A CAM operation carries one on ``.ToolController``; FreeCAD also hands
    the job's ToolController objects to the post directly, and those *are*
    the controller.
    """
    controller = getattr(operation, "ToolController", None)
    if controller is not None:
        return controller
    if hasattr(operation, "Tool") and hasattr(operation, "ToolNumber"):
        return operation
    return None


def collect_tools(operations):
    """Return the tool table used by ``operations``, in order of first use.

    Each entry is a dict with ``number``, ``diameter``, ``corner_radius``,
    ``name`` and ``zmin`` (the lowest Z the tool reaches, which is what the
    ``ZMIN=`` annotation in the header reports).

    The same tool number is often seen more than once -- from the
    ToolController object and again from each operation that uses it -- so
    entries are enriched as better information turns up rather than being
    fixed by whichever object happened to come first.
    """
    tools = []
    index = {}
    for operation in operations:
        controller = _resolve_controller(operation)
        tool = getattr(controller, "Tool", None)
        number = getattr(controller, "ToolNumber", None)
        commands = iter_commands(operation)
        if number is None:
            for command in commands:
                if _command_name(command) in ("M6", "M06"):
                    number = _command_parameters(command).get("T")
                    break
        if number is None:
            continue
        number = int(number)
        entry = index.get(number)
        if entry is None:
            entry = {"number": number, "diameter": None,
                     "corner_radius": None, "name": "", "zmin": None}
            index[number] = entry
            tools.append(entry)
        if tool is not None:
            if entry["diameter"] is None:
                entry["diameter"] = _quantity_value(
                    getattr(tool, "Diameter", None))
            if entry["corner_radius"] is None:
                entry["corner_radius"] = _quantity_value(
                    getattr(tool, "CornerRadius", None)) or 0.0
            if not entry["name"]:
                entry["name"] = getattr(tool, "Label", None) or \
                    getattr(tool, "Name", "") or ""
        for command in commands:
            z = _command_parameters(command).get("Z")
            if z is None:
                continue
            z = float(z)
            entry["zmin"] = z if entry["zmin"] is None else \
                min(entry["zmin"], z)
    for entry in tools:
        if entry["diameter"] is None:
            entry["diameter"] = 0.0
    return tools


# --------------------------------------------------------------------------
# FreeCAD entry point
# --------------------------------------------------------------------------
def export(objectslist, filename, argstring=""):
    """FreeCAD CAM entry point.

    ``filename`` of ``"-"`` returns the g-code without writing a file, which
    is also what the unit tests use.
    """
    args = process_arguments(argstring)
    if args is None:
        return None

    if not isinstance(objectslist, (list, tuple)):
        objectslist = [objectslist]

    post = AvidPost(args)
    gcode = post.build(objectslist)

    if args.show_editor:
        gcode = _maybe_show_editor(gcode)

    if filename and filename != "-":
        root, ext = os.path.splitext(filename)
        if not ext:
            filename = root + EXTENSION
        with open(filename, "w") as handle:
            handle.write(gcode)
    return gcode


def _maybe_show_editor(gcode):
    """Open the FreeCAD g-code editor when running inside FreeCAD."""
    try:  # pragma: no cover - requires a running FreeCAD GUI
        import FreeCAD

        if not FreeCAD.GuiUp:
            return gcode
        from Path.Post.Utils import editor

        result = editor(gcode)
        return result if result is not None else gcode
    except Exception:  # pragma: no cover - headless / no FreeCAD
        return gcode
