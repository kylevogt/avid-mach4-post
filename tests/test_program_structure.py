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
)

from avid_mach4_post import AvidPost, process_arguments


def simple_op(label="Op", tool=1):
    return FakeOperation(label, [
        cmd("M6", T=tool),
        cmd("M3", S=12000),
        cmd("G0", X=0, Y=0),
        cmd("G0", Z=5.08),
        cmd("G1", Z=-1.0, F=500),
    ], FakeToolController(tool))


class TestPreamble:
    def test_preamble_matches_the_avid_post(self, run_post):
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
    def test_header_is_written_by_default(self, run_post):
        lines = run_post([FakeJob(), simple_op()], "")
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
            cmd("G1", Z=-6.35, F=500),
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

    def test_footer_retracts_z_then_xy(self, run_post):
        lines = run_post(simple_op())
        end = lines.index("M30")
        assert lines[end - 4:end] == [
            "G28 G91 Z0.", "G90", "G28 G91 X0. Y0.", "G90"]


class TestSafeRetracts:
    def test_g28_is_the_default(self, run_post):
        lines = run_post(simple_op())
        assert "G28 G91 Z0." in lines
        assert lines[lines.index("G28 G91 Z0.") + 1] == "G90"

    def test_g30_variant(self, run_post):
        lines = run_post(simple_op(), "--no-header --safe-retracts g30")
        assert "G30 G91 Z0." in lines
        assert "G28 G91 Z0." not in lines

    def test_g53_skips_the_z_retract_and_homes_xy_in_machine_coords(
            self, run_post):
        lines = run_post(simple_op(), "--no-header --safe-retracts g53")
        assert not any(line.startswith("G28") for line in lines)
        assert "G53 G0 X0. Y0." in lines

    def test_g53_honours_configured_home(self, run_post):
        lines = run_post(
            simple_op(),
            "--no-header --safe-retracts g53 --home-x 25.4 --home-y 50.8")
        assert "G53 G0 X1. Y2." in lines

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
        post = AvidPost(process_arguments(""))
        gcode = post.build([FakeJob(), simple_op()],
                           now=datetime.datetime(2024, 1, 2, 3, 4, 5))
        assert "(OUTPUT TIME 2024-01-02 030405)" in gcode
