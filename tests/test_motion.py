"""Linear moves, arcs, dwells, canned cycles and unit conversion."""

import pytest
from conftest import FakeOperation, FakeToolController, cmd, mmpm


def motion_op(commands, label="Op"):
    return FakeOperation(label, [cmd("M6", T=1), cmd("M3", S=12000)] +
                         list(commands), FakeToolController(1))


def body(lines):
    """Return just the operation body, without header/preamble/footer.

    The footer starts at the blank line after the section, whose length
    depends on --safe-retracts, so it is found rather than counted.
    """
    start = lines.index("(OP)")
    return lines[start + 1:lines.index("", start)]


def run_body(run_post, commands, argstring="--no-header --no-write-tools"):
    return body(run_post(motion_op(commands), argstring))


class TestLinearMotion:
    def test_rapid_and_feed_moves(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=25.4, Y=0),
            cmd("G1", X=25.4, Y=25.4, F=mmpm(1016)),
        ])
        assert "G0 X1. Y0." in out
        assert "G1 Y1. F40." in out

    def test_motion_code_is_modal(self, run_post):
        out = run_body(run_post, [
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G1", X=50.8, F=mmpm(1016)),
        ])
        assert sum("G1 X1. F40." in line for line in out) == 1
        assert sum("X2." in line for line in out) == 1

    def test_unchanged_axes_are_suppressed(self, run_post):
        out = run_body(run_post, [
            cmd("G1", X=25.4, Y=25.4, Z=-1.0, F=mmpm(1016)),
            cmd("G1", X=25.4, Y=50.8, Z=-1.0, F=mmpm(1016)),
        ])
        assert sum("Y2." in line for line in out) == 1
        assert not any(line == "Y2. X1." for line in out)

    def test_no_modal_repeats_everything(self, run_post):
        out = run_body(run_post, [
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G1", X=50.8, F=mmpm(1016)),
        ], "--no-header --no-write-tools --no-modal")
        assert sum("F40." in line for line in out) == 2
        assert len([line for line in out if line.startswith("G1")]) == 2

    def test_a_move_with_no_change_is_dropped(self, run_post):
        out = run_body(run_post, [
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G1", X=25.4, F=mmpm(1016)),
        ])
        assert sum("X1." in line for line in out) == 1

    def test_rotary_axis_is_emitted_in_degrees(self, run_post):
        out = run_body(run_post, [cmd("G1", X=25.4, A=90.0, F=mmpm(1016))])
        assert "A90." in out[-1]


class TestUnits:
    def test_inches_convert_from_freecad_millimetres(self, run_post):
        out = run_body(run_post, [cmd("G1", X=25.4, Y=12.7, F=mmpm(1016))])
        assert "X1." in out[-1] and "Y0.5" in out[-1]

    def test_inch_feeds_use_one_decimal(self, run_post):
        out = run_body(run_post, [cmd("G1", X=25.4, F=mmpm(1000))])
        assert "F39.4" in out[-1]

    def test_metric_passes_millimetres_through(self, run_post):
        out = run_body(run_post, [cmd("G1", X=25.4, F=mmpm(1000))],
                       "--no-header --no-write-tools --metric")
        assert "X25.4" in out[-1]
        assert "F1000." in out[-1]

    def test_precision_is_configurable(self, run_post):
        out = run_body(run_post, [cmd("G1", X=1.0, F=mmpm(1000))],
                       "--no-header --no-write-tools --metric --precision 6")
        assert "X1." in out[-1]
        out = run_body(run_post, [cmd("G1", X=1.23456789, F=mmpm(1000))],
                       "--no-header --no-write-tools --metric --precision 6")
        assert "X1.234568" in out[-1]


class TestArcs:
    def test_ijk_arc_with_plane_and_direction(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
        ])
        assert "G2 X1. Y1. I1. J0. F40." in out

    def test_counter_clockwise_arc(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G3", X=25.4, Y=25.4, I=0, J=25.4, F=mmpm(1016)),
        ])
        assert "G3 X1. Y1. I0. J1. F40." in out

    def test_helical_arc_keeps_k(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=0, Z=-2.54, I=12.7, J=0, K=0, F=mmpm(1016)),
        ])
        assert "K0." in out[-1]
        assert "Z-0.1" in out[-1]

    def test_full_circle_is_defined_by_its_centre_alone(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=0, Y=0, I=25.4, J=0, F=mmpm(1016)),
        ])
        assert "G2 I1. J0. F40." in out

    def test_radius_arcs_for_a_quarter_circle(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
        ], "--no-header --no-write-tools --radius-arcs")
        assert "R1." in out[-1]
        assert "I1." not in out[-1]

    def test_radius_arcs_are_negative_beyond_180_degrees(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            # 270 degree clockwise arc around (25.4, 0)
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
            cmd("G3", X=0, Y=0, I=-25.4, J=-25.4, F=mmpm(1016)),
        ], "--no-header --no-write-tools --radius-arcs")
        assert "R-1." in out[-1]

    def test_full_circles_fall_back_to_ijk_in_radius_mode(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=0, Y=0, I=25.4, J=0, F=mmpm(1016)),
        ], "--no-header --no-write-tools --radius-arcs")
        assert "I1. J0." in out[-1]

    def test_arc_plane_is_emitted_once(self, run_post):
        lines = run_post(motion_op([
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
        ]), "--no-header --no-write-tools")
        # G17 already comes from the preamble
        assert len([line for line in lines if "G17" in line]) == 1


class TestDwell:
    def test_dwell_in_seconds_by_default(self, run_post):
        out = run_body(run_post, [cmd("G4", P=1.5)])
        assert "G4 P1.5" in out

    def test_dwell_in_milliseconds(self, run_post):
        out = run_body(run_post, [cmd("G4", P=1.5)],
                       "--no-header --no-write-tools --dwell-in-milliseconds")
        assert "G4 P1500" in out


class TestCannedCycles:
    def test_first_hole_carries_the_full_definition(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G81", X=25.4, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert "G98 G81 X1. Y0. Z-1. R0.1 F10." in out

    def test_following_holes_only_carry_what_changed(self, run_post):
        out = run_body(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G81", X=25.4, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G81", X=50.8, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert out[-1] == "X2."

    def test_peck_drilling_keeps_q(self, run_post):
        out = run_body(run_post, [
            cmd("G83", X=25.4, Y=0, Z=-25.4, R=2.54, Q=5.08, F=mmpm(254)),
        ])
        assert "G83" in out[-1] and "Q0.2" in out[-1]

    def test_g80_cancels_and_resets_modality(self, run_post):
        out = run_body(run_post, [
            cmd("G81", X=25.4, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G80"),
            cmd("G81", X=25.4, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert sum(line == "G80" for line in out) == 1
        assert len([line for line in out if "G81" in line]) == 2

    def test_boring_cycle_dwell_is_in_seconds(self, run_post):
        out = run_body(run_post, [
            cmd("G82", X=25.4, Y=0, Z=-25.4, R=2.54, P=0.5, F=mmpm(254)),
        ])
        assert "P0.5" in out[-1]


class TestPassthrough:
    def test_unknown_codes_are_formatted_but_preserved(self, run_post):
        out = run_body(run_post, [cmd("M64", P=1)])
        assert "M64 P1." in out

    def test_inline_comments_are_kept(self, run_post):
        out = run_body(run_post, [cmd("(ramp in)")])
        assert "(RAMP IN)" in out

    def test_duplicate_state_codes_are_collapsed(self, run_post):
        lines = run_post(motion_op([cmd("G90"), cmd("G21"), cmd("G90")]),
                         "--no-write-tools --metric")
        # the stream's own G90s change nothing and its unit word is dropped
        # outright; the two G90 lines both come from retracts restoring
        # absolute mode after their G91 block
        assert lines.count("G90") == 2
        assert lines.count("G21") == 1


class TestExport:
    def test_export_writes_a_file(self, tmp_path, post):
        target = tmp_path / "part.tap"
        post.export([motion_op([cmd("G0", X=0, Y=0)])], str(target),
                    "--no-header --no-show-editor")
        assert target.read_text().rstrip().endswith("M30")

    def test_missing_extension_defaults_to_tap(self, tmp_path, post):
        post.export([motion_op([cmd("G0", X=0, Y=0)])],
                    str(tmp_path / "part"), "--no-header --no-show-editor")
        assert (tmp_path / "part.tap").exists()

    def test_dash_filename_returns_the_gcode_without_writing(self, post):
        gcode = post.export([motion_op([cmd("G0", X=0, Y=0)])], "-",
                            "--no-header")
        assert gcode.endswith("M30\n")

    def test_a_single_object_is_accepted(self, post):
        gcode = post.export(motion_op([cmd("G0", X=0, Y=0)]), "-",
                            "--no-header")
        assert "(OP)" in gcode

    def test_bad_arguments_return_none(self, post):
        assert post.export([], "-", "--nope") is None

    def test_inactive_operations_are_skipped(self, post):
        operation = motion_op([cmd("G0", X=0, Y=0)], label="Skipped")
        operation.Active = False
        gcode = post.export([operation], "-", "--no-header")
        assert "(SKIPPED)" not in gcode

    def test_non_path_objects_are_ignored(self, post):
        class Stock:
            Name = "Stock"

        gcode = post.export([Stock(), motion_op([cmd("G0", X=0, Y=0)])], "-",
                            "--no-header")
        assert "M30" in gcode

    @pytest.mark.parametrize("argstring", [
        "", "--metric", "--line-numbers", "--no-spaces", "--radius-arcs",
        "--dust-collector", "--safe-retracts g30", "--safe-retracts g53",
        "--no-comments", "--no-modal", "--preload-tool",
    ])
    def test_every_option_produces_a_complete_program(self, post, argstring):
        gcode = post.export([motion_op([
            cmd("G0", X=0, Y=0),
            cmd("G1", Z=-1.0, F=mmpm(500)),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
            cmd("G81", X=25.4, Y=0, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G80"),
        ])], "-", argstring)
        assert gcode.rstrip().endswith("M30")
