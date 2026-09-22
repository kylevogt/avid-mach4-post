"""Header, preamble, footer and safe-retract behaviour."""

import datetime

import pytest
from conftest import (
    FakeJob,
    FakeMachine,
    FakeOperation,
    FakeTool,
    FakeToolController,
    cmd,
    mmpm,
)

from avid_mach4_post import AvidPost, process_arguments


def simple_op(label="Op", tool=1):
    return FakeOperation(label, [
        cmd("M6", T=tool),
        cmd("M3", S=12000),
        cmd("G0", X=0, Y=0),
        cmd("G0", Z=5.08),
        cmd("G1", Z=-1.0, F=mmpm(500)),
    ], FakeToolController(tool))


class TestPreamble:
    def test_preamble_is_the_expected_safe_state(self, run_post):
        lines = run_post(simple_op())
        assert "G90 G94 G91.1 G40 G49 G17" in lines
        assert "G20" in lines

    def test_metric_emits_g21(self, run_post):
        lines = run_post(simple_op(), "--no-header --metric")
        assert "G21" in lines
        assert "G20" not in lines

    def test_user_preamble_blocks_are_appended(self, run_post):
        lines = run_post(simple_op(),
                         '--no-header --preamble "G53 G0 Z0;M64 P1"')
        assert "G53 G0 Z0" in lines
        assert "M64 P1" in lines

    def test_user_postamble_runs_before_m30(self, run_post):
        lines = run_post(simple_op(), '--no-header --postamble "M65 P1"')
        assert lines.index("M65 P1") < lines.index("M30")


class TestHeader:
    def test_no_generated_by_header_by_default(self, run_post):
        # an AVID/Fusion program opens with the program name and the tool
        # table, and carries no timestamp
        lines = run_post([FakeJob(), simple_op()], "")
        assert "(EXPORTED BY FREECAD)" not in lines
        assert not any(line.startswith("(OUTPUT TIME") for line in lines)
        assert lines[0] == "(JOB)"

    def test_header_can_be_asked_for(self, run_post):
        lines = run_post([FakeJob(), simple_op()], "--header")
        assert "(EXPORTED BY FREECAD)" in lines
        assert any(line.startswith("(OUTPUT TIME") for line in lines)

    def test_header_can_be_suppressed(self, run_post):
        lines = run_post([FakeJob(), simple_op()], "--no-header")
        assert not any("EXPORTED BY FREECAD" in line for line in lines)

    def test_machine_description_is_written(self, run_post):
        job = FakeJob(machine=FakeMachine("Avid CNC", "PRO4824", "Mach4"))
        lines = run_post([job, simple_op()], "--no-header")
        assert "(MACHINE)" in lines
        assert "(  VENDOR AVID CNC)" in lines
        assert "(  MODEL PRO4824)" in lines
        assert "(  CONTROL MACH4)" in lines

    def test_machine_description_can_be_suppressed(self, run_post):
        lines = run_post([FakeJob(), simple_op()],
                         "--no-header --no-write-machine")
        assert "(MACHINE)" not in lines

    def test_tool_table_lists_diameter_and_zmin(self, run_post):
        op = FakeOperation("Contour", [
            cmd("M6", T=3),
            cmd("G0", Z=5.08),
            cmd("G1", Z=-6.35, F=mmpm(500)),
        ], FakeToolController(3, FakeTool("Flat End Mill", 6.35, 0.0)))
        lines = run_post([FakeJob(), op], "--no-header")
        tool_line = [line for line in lines if line.startswith("(T3")][0]
        assert "D=0.25" in tool_line
        assert "CR=0." in tool_line
        assert "ZMIN=-0.25" in tool_line
        assert "FLAT END MILL" in tool_line

    def test_tool_table_can_be_suppressed(self, run_post):
        lines = run_post([FakeJob(), simple_op()],
                         "--no-header --no-write-tools")
        assert not any(line.startswith("(T1") for line in lines)

    def test_no_comments_removes_every_comment(self, run_post):
        lines = run_post([FakeJob(), simple_op()], "--no-comments")
        assert not any(line.startswith("(") for line in lines)


class TestFooter:
    def test_program_ends_with_m30(self, run_post):
        assert run_post(simple_op())[-1] == "M30"

    def test_footer_is_just_m30_without_retracts(self, run_post):
        lines = run_post(simple_op(), "--safe-retracts none")
        assert lines[-2:] == ["", "M30"]

    def test_footer_retracts_z_and_leaves_xy_alone(self, run_post):
        # a traverse to machine home at the end of the program crosses the
        # whole table and can find work holding on the way
        lines = run_post(simple_op())
        end = lines.index("M30")
        assert lines[end - 2:end] == ["G28 G91 Z0.", "G90"]
        assert "G28 G91 X0. Y0." not in lines

    def test_footer_homes_xy_after_z_when_asked(self, run_post):
        lines = run_post(simple_op(), "--home-xy-at-end")
        end = lines.index("M30")
        assert lines[end - 4:end] == [
            "G28 G91 Z0.", "G90", "G28 G91 X0. Y0.", "G90"]


class TestSafeRetracts:
    def test_g28_is_the_default_and_restores_absolute(self, run_post):
        lines = run_post(simple_op())
        assert "G28 G91 Z0." in lines
        assert lines[lines.index("G28 G91 Z0.") + 1] == "G90"

    def test_g30_variant(self, run_post):
        lines = run_post(simple_op(), "--no-header --safe-retracts g30")
        assert "G30 G91 Z0." in lines
        assert "G28 G91 Z0." not in lines

    def test_g53_retracts_in_machine_coordinates(self, run_post):
        lines = run_post(simple_op(), "--no-header --safe-retracts g53")
        assert not any(line.startswith("G28") for line in lines)
        assert "G53 G0 Z0." in lines
        assert "G53 G0 X0. Y0." not in lines

    def test_g53_lifts_z_before_it_traverses_to_machine_home(self,
                                                             run_post):
        # the footer used to send the tool to machine home in XY at
        # whatever depth the last operation stopped at
        lines = run_post(simple_op(),
                         "--no-header --safe-retracts g53 --home-xy-at-end")
        cut = max(i for i, ln in enumerate(lines) if ln.startswith("G1 Z-"))
        assert lines.index("G53 G0 Z0.", cut) < \
            lines.index("G53 G0 X0. Y0.")

    def test_g53_honours_configured_home(self, run_post):
        lines = run_post(
            simple_op(),
            "--no-header --safe-retracts g53 --home-xy-at-end"
            " --home-x 25.4 --home-y 50.8 --home-z -25.4")
        assert "G53 G0 X1. Y2." in lines
        assert "G53 G0 Z-1." in lines

    def test_retracting_xy_and_z_together_in_g53_mode_is_rejected(self):
        post = AvidPost(process_arguments("--safe-retracts g53"))
        with pytest.raises(ValueError):
            post.write_retract("X", "Y", "Z")


class TestLineNumbers:
    def test_no_line_numbers_by_default(self, run_post):
        assert not any(line.startswith("N")
                       for line in run_post(simple_op()))

    def test_line_numbers_increment(self, run_post):
        lines = [line for line in
                 run_post(simple_op(), "--no-header --line-numbers")
                 if line.startswith("N")]
        assert lines[0].startswith("N10 ")
        assert lines[1].startswith("N15 ")
        assert lines[2].startswith("N20 ")

    def test_line_number_start_and_increment_are_configurable(self, run_post):
        lines = [line for line in run_post(
            simple_op(),
            "--no-header --line-numbers --line-number-start 100 "
            "--line-number-increment 10") if line.startswith("N")]
        assert lines[0].startswith("N100 ")
        assert lines[1].startswith("N110 ")


class TestWordSeparator:
    def test_words_are_space_separated_by_default(self, run_post):
        assert "G90 G94 G91.1 G40 G49 G17" in run_post(simple_op())

    def test_no_spaces_packs_the_block(self, run_post):
        lines = run_post(simple_op(), "--no-header --no-spaces")
        assert "G90G94G91.1G40G49G17" in lines


class TestBuildDeterminism:
    def test_timestamp_can_be_injected(self):
        post = AvidPost(process_arguments("--header"))
        gcode = post.build([FakeJob(), simple_op()],
                           now=datetime.datetime(2024, 1, 2, 3, 4, 5))
        assert "(OUTPUT TIME 2024-01-02 030405)" in gcode


class TestNoSafeRetracts:
    """``--safe-retracts none`` reproduces the AVID Fusion post with its
    ``useG28`` property off: no retract block anywhere, and each operation's
    own clearance-height move is what lifts the tool."""

    def two_ops(self):
        def op(label, tool):
            return FakeOperation(label, [
                cmd("M6", T=tool), cmd("M3", S=20000),
                cmd("G0", Z=15.24),
                cmd("G0", X=78.58, Y=78.31),
                cmd("G1", Z=5.08, F=mmpm(2540)),
                cmd("G0", Z=15.24),
            ], FakeToolController(tool, FakeTool(f"T{tool}")))
        return [op("Rough", 1), op("Finish", 2)]

    def test_no_retract_block_is_emitted(self, run_post):
        lines = run_post(self.two_ops(),
                         "--no-header --no-write-tools --safe-retracts none")
        assert not any(line.startswith(("G28", "G30", "G53"))
                       for line in lines)

    def test_the_program_still_ends_with_m30(self, run_post):
        lines = run_post(self.two_ops(),
                         "--no-header --no-write-tools --safe-retracts none")
        assert lines[-1] == "M30"
        assert lines[-2] == ""

    def test_freecads_approach_order_is_preserved(self, run_post):
        lines = run_post(self.two_ops(),
                         "--no-header --no-write-tools --safe-retracts none")
        tail = lines[lines.index("(FINISH)"):]
        assert tail[6:8] == ["G0 G43 Z0.6 H2", "X3.0937 Y3.0831"]

    def test_each_operations_own_clearance_move_lifts_the_tool(self,
                                                               run_post):
        # nothing retracts in this mode, so the tool leaves the cut on the
        # operation's own clearance rapid
        first = FakeOperation("A", [
            cmd("M6", T=1), cmd("M3", S=12000),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G1", Z=-12.7, F=mmpm(500)),
        ], FakeToolController(1))
        second = FakeOperation("B", [
            cmd("G0", Z=15.24),
            cmd("G0", X=76.2, Y=76.2),
            cmd("G1", Z=-12.7, F=mmpm(500)),
        ], FakeToolController(1))
        lines = run_post([first, second],
                         "--no-header --no-write-tools --safe-retracts none")
        tail = lines[lines.index("(B)"):]
        assert tail[1:3] == ["G0 Z0.6", "X3. Y3."]

    def test_a_mid_section_tool_change_is_not_retracted(self, run_post):
        operation = FakeOperation("Custom", [
            cmd("M6", T=1), cmd("M3", S=12000),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G1", Z=-76.2, F=mmpm(500)),
            cmd("M6", T=2), cmd("M3", S=12000),
            cmd("G0", Z=127.0),
            cmd("G0", X=101.6, Y=101.6),
        ], FakeToolController(1))
        lines = run_post(operation,
                         "--no-header --no-write-tools --safe-retracts none")
        assert not any(ln.startswith(("G28", "G30", "G53")) for ln in lines)
        tail = lines[lines.index("T2 M6"):]
        assert tail[3:5] == ["G0 G43 Z5. H2", "X4. Y4."]
