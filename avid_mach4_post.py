"""AVID CNC / Mach4 post processor for the FreeCAD CAM workbench.

This post reproduces the behaviour of the ``Avid CNC`` post processor that
AVID CNC ships for Autodesk Fusion 360 (``avid cnc.cps``), so that g-code
produced by FreeCAD runs on an AVID CNC machine driven by Mach4 without any
hand editing.

Behaviour that is deliberately copied from the Fusion post:

* ``(COMMENTS LIKE THIS)`` -- upper cased and filtered down to the character
  set Mach4 accepts inside a comment.
* Fusion style number formatting: trailing zeros trimmed, but a decimal point
  is always emitted (``X0.``, ``Z-1.25``, ``F60.``).
* Preamble ``G90 G94 G91.1 G40 G49 G17`` followed by ``G20``/``G21``.
* Tool changes emitted as ``M5`` / coolant off / ``M1`` / ``T<n> M6`` /
  ``S<rpm> M3`` / work offset / ``G0 G43 Z<z> H<n>``.
* Safe retracts through ``G28 G91 Z0.`` + ``G90`` (or ``G30``, or ``G53``).
* Arcs in incremental ``I``/``J``/``K`` form, optionally as ``R``.
* Optional dust collector support (``M7`` in the header, ``M9`` in the footer).
* Program end with ``M30``.

The module has no hard dependency on FreeCAD so that it can be unit tested
with plain CPython; the FreeCAD specific bits are imported lazily.
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import shlex

__all__ = ["export", "TOOLTIP", "TOOLTIP_ARGS", "UNITS"]

TOOLTIP = """
Post processor for AVID CNC machines running Mach4.  It mirrors the AVID
supplied Fusion 360 post: Fusion style number formatting, G28/G30/G53 safe
retracts, M6 tool changes with G43 tool length compensation, incremental arc
centres and an M30 program end.

Import it with:

    import avid_mach4_post
    avid_mach4_post.export(object, "/path/to/file.tap", "--inches")
"""

# Mach4 only accepts this subset inside a comment; anything else is dropped.
PERMITTED_COMMENT_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,=_-"

#: Default file extension used by the AVID/Mach4 tool chain.
EXTENSION = ".tap"

#: Set by :func:`export`; FreeCAD inspects this after a run.
UNITS = "G20"

MM_PER_INCH = 25.4

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
# routed through a modal group so the output stays as terse as Fusion's.
MODAL_GROUPS = {
    "G17": "plane_modal", "G18": "plane_modal", "G19": "plane_modal",
    "G20": "unit_modal", "G21": "unit_modal",
    "G90": "abs_inc_modal", "G91": "abs_inc_modal",
    "G93": "feed_mode_modal", "G94": "feed_mode_modal",
}

WORK_OFFSET_CODES = {"G54", "G55", "G56", "G57", "G58", "G59"}


# --------------------------------------------------------------------------
# number / word formatting
# --------------------------------------------------------------------------
class Formatter:
    """Reimplementation of Fusion's ``createFormat``.

    ``trim`` removes trailing zeros, ``force_decimal`` guarantees a decimal
    point is present, which together produce the ``X0.`` / ``Z-1.25`` style
    that the AVID post emits.
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
    """Reimplementation of Fusion's ``createVariable``.

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
    """Return ``text`` as a Mach4 safe parenthesised comment."""
    cleaned = "".join(
        c for c in str(text).upper() if c in PERMITTED_COMMENT_CHARS
    )
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
    flag("header", "header", True,
         "output the generated-by header (default)", "suppress the header")
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
        "--safe-retracts", dest="safe_retracts", default="g28",
        choices=["g28", "g30", "g53"],
        help="how to retract between operations and at program end "
             "(default: g28)")
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
                        help="X machine home used by --safe-retracts g53")
    parser.add_argument("--home-y", dest="home_y", type=float, default=0.0,
                        help="Y machine home used by --safe-retracts g53")
    parser.add_argument("--preamble", dest="preamble", default="",
                        help="blocks emitted after the header, ';' separated")
    parser.add_argument("--postamble", dest="postamble", default="",
                        help="blocks emitted before M30, ';' separated")
    return parser


PARSER = _build_parser()
TOOLTIP_ARGS = PARSER.format_help()


def process_arguments(argstring):
    """Parse ``argstring`` and return the namespace, or ``None`` on error."""
    try:
        return PARSER.parse_args(shlex.split(argstring or ""))
    except SystemExit:
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

        self.xyz_format = Formatter(decimals=precision, scale=scale)
        self.feed_format = Formatter(decimals=1 if inches else 0, scale=scale)
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
            Formatter(decimals=1 if inches else 0, prefix="F", scale=scale))
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
        self.retracted = False
        self.first_section = True
        self.pending_tool_length_offset = False
        self.pending_coolant = None
        self.tool_numbers = []
        self.section_comment = None

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
    def write_retract(self, *axes):
        """Reproduce the Fusion post's ``writeRetract``.

        With ``g28``/``g30`` the machine is sent home in incremental mode;
        with ``g53`` a Z only retract is skipped entirely (exactly as the
        AVID post does) and X/Y move in machine coordinates.
        """
        axes = [a.upper() for a in axes]
        use_g28 = self.args.safe_retracts in ("g28", "g30")

        if not use_g28 and "Z" in axes and ("X" in axes or "Y" in axes):
            raise ValueError("cannot move home in XY and Z in the same block")
        if "Z" in axes and not use_g28:
            return

        homes = {"X": self.args.home_x, "Y": self.args.home_y, "Z": 0.0}
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
            self.write_block(code, self.abs_inc_modal.format(91), *words)
            self.write_block(self.abs_inc_modal.format(90))
        else:
            self.motion_modal.reset()
            self.write_block(self.abs_inc_modal.format(90), "G53",
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
        vendor = getattr(machine, "Vendor", None) or "Avid CNC"
        model = getattr(machine, "Model", None) or ""
        description = getattr(machine, "Description", None) or ""
        self.write_comment("Machine")
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
            self.write(block)

    def write_postamble(self):
        self.write_blank()
        if self.args.dust_collector:
            self.write_block("M9")
        elif self.current_coolant != "None":
            self.write_block("M9")
            self.current_coolant = "None"
        self.write_retract("Z")
        self.write_retract("X", "Y")
        for block in _split_blocks(self.args.postamble):
            self.write(block)
        self.write_block("M30")

    # -- sections -----------------------------------------------------------
    def begin_section(self, operation):
        """Emit the retract, blank line and operation comment for an op."""
        commands = list(iter_commands(operation))
        tool_change = any(_command_name(c) in ("M6", "M06") for c in commands)
        if tool_change or self.first_section:
            self.write_retract("Z")
        self.write_blank()
        label = getattr(operation, "Label", None) or \
            getattr(operation, "Name", "")
        self.section_comment = format_comment(label) if label else None
        if label:
            self.write_comment(label)
        if self.args.coolant and not self.args.dust_collector:
            self.pending_coolant = coolant_mode(operation)
        return commands

    def write_tool_change(self, tool_number):
        self.write_block("M5")
        if self.current_coolant != "None" and not self.args.dust_collector:
            self.write_block("M9")
            self.current_coolant = "None"
        if not self.first_section and self.args.optional_stop:
            self.write_block("M1")

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
        # the AVID post forces the work offset back out after a tool change
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
        """Emit G54..G59 (optionally ``G59 P<n>``) when it changes.

        Called with no arguments before the first motion of a section: the
        AVID post defaults to G54 when the job never specified an offset.
        """
        if code is None:
            if self.current_work_offset is not None:
                return
            code = "G54"
        key = code if p is None else f"{code} P{self.int_format.format(p)}"
        if key == self.current_work_offset:
            return
        self.current_work_offset = key
        if p is None:
            self.write_block(code)
        else:
            self.write_block(code, "P" + self.int_format.format(p))

    def flush_coolant(self):
        if self.pending_coolant is None:
            return
        mode = self.pending_coolant
        self.pending_coolant = None
        code = COOLANT_CODES.get(str(mode))
        if code is None or mode == self.current_coolant:
            return
        self.write_block(code)
        self.current_coolant = mode

    # -- command translation ------------------------------------------------
    def parse_command(self, command):
        name = _command_name(command)
        if not name:
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
            tool = params.get("T", self.current_tool)
            self.write_tool_change(int(tool) if tool is not None else 0)
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
        if name in ("G43",):
            self.pending_tool_length_offset = False
            self.write_block("G43", self._h_word(params))
            return
        if name in WORK_OFFSET_CODES:
            self.write_work_offset(name, params.get("P"))
            return
        if name in MODAL_GROUPS:
            modal = getattr(self, MODAL_GROUPS[name])
            self.write_block(modal.format(float(name[1:]))
                             if self.args.modal else name)
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
        if name == "G80":
            self.cycle_modal.reset()
            self.retract_modal.reset()
            self.z_output.reset()
            self.r_output.reset()
            self.q_output.reset()
            self.write_block("G80")
            return

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

        applies_tlo = (self.pending_tool_length_offset and code == 0 and
                       "Z" in params)
        if applies_tlo:
            # the AVID post hangs G43 off the first Z rapid after a tool
            # change: "G0 G43 Z0.2 H1"
            self.pending_tool_length_offset = False
            self.z_output.reset()
            self.write_block(self.motion_modal.format(code), "G43",
                             self.z_output.format(params["Z"]),
                             self._h_word({}))
            self._update_position(params)
            self.feed_output.reset()
            return

        motion = self.motion_modal.format(code) if self.args.modal else \
            "G" + str(code)
        coords = self._coordinate_words(params)
        feed = self._feed_word(params) if code != 0 else ""
        if not coords and not feed:
            return
        words = [self.abs_inc_modal.format(90), motion] + coords
        if feed:
            words.append(feed)
        self.write_block(*words)
        self._update_position(params)
        if code == 0:
            self.feed_output.reset()

    def write_arc(self, code, params):
        start = dict(self.position)
        i = params.get("I", 0.0) or 0.0
        j = params.get("J", 0.0) or 0.0
        k = params.get("K")
        end_x = params.get("X", start["X"])
        end_y = params.get("Y", start["Y"])
        full_circle = (
            self.xyz_format.format(end_x) == self.xyz_format.format(start["X"])
            and self.xyz_format.format(end_y) ==
            self.xyz_format.format(start["Y"])
        )

        words = [self.plane_modal.format(17) if self.args.modal else "G17"]
        words.append(self.motion_modal.format(code) if self.args.modal
                     else "G" + str(code))
        if not full_circle:
            # a full circle is defined by its centre alone, exactly as the
            # AVID post emits it
            words.extend(self._coordinate_words(params))

        if self.args.radius_arcs and not full_circle:
            radius = math.hypot(i, j)
            sweep = _arc_sweep(start["X"], start["Y"], start["X"] + i,
                               start["Y"] + j, end_x, end_y, code == 2)
            if sweep > math.pi + 1e-9:
                radius = -radius
            words.append("R" + self.xyz_format.format(radius))
        else:
            words.append("I" + self.xyz_format.format(i))
            words.append("J" + self.xyz_format.format(j))
            if k is not None:
                words.append("K" + self.xyz_format.format(k))

        feed = self._feed_word(params)
        if feed:
            words.append(feed)
        self.write_block(*words)
        self._update_position(params)

    def write_cycle(self, name, params):
        """Emit a canned cycle block.

        The first point of a cycle carries the full definition; the ones
        after it only carry what changed, which is how the AVID post emits
        repeated holes.
        """
        retract_word = self.retract_modal.format(98) if self.args.modal \
            else "G98"
        cycle_word = self.cycle_modal.format(float(name[1:])) \
            if self.args.modal else name
        first_point = bool(cycle_word) or not self.args.modal

        words = [retract_word, cycle_word]
        for axis in ("X", "Y", "Z"):
            if axis not in params:
                continue
            value = params[axis]
            if first_point:
                self.axis_outputs[axis].reset()
            words.append(self.axis_outputs[axis].format(value))
        for letter, output in (("R", self.r_output), ("Q", self.q_output)):
            if letter not in params:
                continue
            if first_point:
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
    def _coordinate_words(self, params):
        """Format the axis words of a block, dropping unchanged ones."""
        words = []
        for axis in ("X", "Y", "Z", "A", "B", "C"):
            if axis not in params:
                continue
            output = self.axis_outputs[axis]
            if not self.args.modal:
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
        for axis in ("X", "Y", "Z"):
            if axis in params and params[axis] is not None:
                self.position[axis] = float(params[axis])

    # -- driver -------------------------------------------------------------
    def build(self, objectslist, now=None):
        operations = [obj for obj in objectslist if _is_path_object(obj)]
        job = _find_job(objectslist)
        self.tool_numbers = [t["number"] for t in collect_tools(operations)]

        self.write_header(job, operations, now=now)
        self.write_preamble()

        for operation in operations:
            if getattr(operation, "Active", True) is False:
                continue
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
    return list(operation.Path.Commands)


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


def collect_tools(operations):
    """Return the tool table used by ``operations``, in order of first use.

    Each entry is a dict with ``number``, ``diameter``, ``corner_radius``,
    ``name`` and ``zmin`` (the lowest Z the tool reaches, mirroring the
    ``ZMIN=`` annotation the AVID Fusion post writes).
    """
    tools = []
    index = {}
    for operation in operations:
        controller = getattr(operation, "ToolController", None)
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
            entry = {
                "number": number,
                "diameter": _quantity_value(getattr(tool, "Diameter", None))
                or 0.0,
                "corner_radius": _quantity_value(
                    getattr(tool, "CornerRadius", None)),
                "name": getattr(tool, "Label", None) or
                getattr(tool, "Name", "") or "",
                "zmin": None,
            }
            index[number] = entry
            tools.append(entry)
        for command in commands:
            z = _command_parameters(command).get("Z")
            if z is None:
                continue
            z = float(z)
            entry["zmin"] = z if entry["zmin"] is None else \
                min(entry["zmin"], z)
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
