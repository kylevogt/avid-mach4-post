"""Modal state the control is left in between blocks.

Every one of these is a case where the emitted g-code parsed fine but put
Mach4 into a state the next block did not expect: a bare axis word with no
motion mode, an arc forced into the wrong plane, an incremental move
executed absolute. They are reachable from a Custom operation or from a
path generator that does not split its arcs.
"""

from conftest import FakeOperation, FakeToolController, cmd, mmpm


def op(commands, label="Op", tool=1):
    return FakeOperation(label, [cmd("M6", T=tool), cmd("M3", S=12000)] +
                         list(commands), FakeToolController(tool))


def run(run_post, commands, argstring="--no-header --no-write-tools"):
    """Return one operation's blocks, without the header or the footer."""
    lines = run_post(op(commands), argstring)
    start = lines.index("(OP)")
    return lines[start:lines.index("", start)]


class TestCannedCycleModalGroup:
    def test_a_move_after_g80_names_its_motion_code(self, run_post):
        # "G80" then a bare "Z0.1969" left the control in G80 with an axis
        # word, which RS274 makes an error
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G80"),
            cmd("G0", Z=5.08),
            cmd("G0", X=101.6, Y=101.6),
        ])
        after = out[out.index("G80") + 1:]
        assert after[0] == "G0 Z0.2"
        assert after[1] == "X4. Y4."

    def test_a_rapid_between_holes_cancels_the_cycle_explicitly(self,
                                                               run_post):
        # without a motion word the control reads "X4. Y4." as one more
        # hole at that position
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G0", X=101.6, Y=101.6),
        ])
        assert out[-1] == "G0 X4. Y4."

    def test_the_cycle_is_restated_after_a_plain_move(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G0", X=101.6, Y=101.6),
            cmd("G81", X=101.6, Y=101.6, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert len([line for line in out if "G81" in line]) == 2

    def test_an_explicit_g99_is_not_overridden_by_g98(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G99"),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert "G99" in out
        assert not any("G98" in line for line in out)


class TestArcPlane:
    def test_an_xy_arc_still_gets_g17_from_the_preamble(self, run_post):
        lines = run_post(op([
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
        ]), "--no-header --no-write-tools")
        assert len([line for line in lines if "G17" in line]) == 1

    def test_an_active_plane_is_not_forced_back_to_g17(self, run_post):
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G18"),
            cmd("G2", X=25.4, Z=-12.7, I=12.7, K=0, F=mmpm(1016)),
        ])
        assert "G18" in out
        assert not any("G17" in line for line in out)

    def test_outside_g17_only_the_given_centre_words_are_emitted(self,
                                                                run_post):
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G18"),
            cmd("G2", X=25.4, Z=-12.7, I=12.7, K=0, F=mmpm(1016)),
        ])
        assert out[-1] == "G2 X1. Z-0.5 I0.5 K0. F40."


class TestHelicalFullCircle:
    def test_a_full_circle_keeps_its_z(self, run_post):
        # dropping Z turned the helix into a flat circle, and every Z after
        # it was measured from a height the machine never reached
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G1", Z=0.0, F=mmpm(500)),
            cmd("G3", X=25.4, Y=25.4, Z=-3.175, I=-5.08, J=0, K=0,
                F=mmpm(1016)),
        ])
        assert out[-1] == "G3 Z-0.125 I-0.2 J0. K0. F40."

    def test_a_flat_full_circle_is_still_its_centre_alone(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G2", X=25.4, Y=25.4, I=-5.08, J=0, F=mmpm(1016)),
        ])
        assert out[-1] == "G2 I-0.2 J0. F40."


class TestIncrementalMode:
    def test_g91_is_not_overridden_by_a_forced_g90(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G91"),
            cmd("G1", X=25.4, F=mmpm(1016)),
        ])
        assert out[out.index("G91") + 1] == "G1 X1. F40."

    def test_repeated_incremental_words_are_not_suppressed(self, run_post):
        # two identical incremental words are two separate moves
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G91"),
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G1", X=25.4, F=mmpm(1016)),
        ])
        assert out[out.index("G91") + 1:] == ["G1 X1. F40.", "X1."]

    def test_returning_to_g90_restates_the_axis_words(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G91"),
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G90"),
            cmd("G1", X=50.8, F=mmpm(1016)),
        ])
        # back in absolute the cached "X1." must not swallow the move
        assert out[-1] == "X2."


class TestSuppressedBlocks:
    def test_a_dropped_block_does_not_claim_the_motion_mode(self, run_post):
        # G0 X1 / G1 X1 (no move, no F) / G1 X2: the middle block is dropped,
        # and used to leave the post believing the control was in G1. The
        # last block then went out as a bare "X2." while the machine was
        # still in G0 -- a rapid through the work at cutting depth.
        out = run(run_post, [
            cmd("G1", X=25.4, F=mmpm(1016)),
            cmd("G0", X=50.8),
            cmd("G1", X=50.8),
            cmd("G1", X=76.2),
        ])
        assert out[-3:] == ["G1 X1. F40.", "G0 X2.", "G1 X3."]

    def test_a_dropped_block_does_not_cancel_a_cycle(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=50.8, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        # the G0 moves nowhere and is dropped, so the cycle is still live
        # and the next hole only carries what changed
        assert out[-1] == "X2."


class TestIncrementalArcs:
    def test_a_g91_half_circle_is_not_mistaken_for_a_full_one(self,
                                                              run_post):
        # the endpoint is a delta under G91; comparing it against an
        # absolute start dropped the X/Y words and closed the arc
        out = run(run_post, [
            cmd("G0", X=25.4, Y=0),
            cmd("G91"),
            cmd("G2", X=25.4, Y=0, I=12.7, J=0, F=mmpm(1000)),
        ])
        assert out[-1] == "G2 X1. Y0. I0.5 J0. F39.4"

    def test_a_g91_arc_radius_keeps_its_sign(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G91"),
            cmd("G2", X=4.41, Y=-50.41, I=0, J=-25.4, F=mmpm(1000)),
        ], "--no-header --no-write-tools --radius-arcs")
        assert "R1." in out[-1]
        assert "R-1." not in out[-1]

    def test_a_g91_full_circle_is_still_recognised(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G91"),
            cmd("G2", X=0, Y=0, I=-5.08, J=0, F=mmpm(1016)),
        ])
        assert out[-1] == "G2 I-0.2 J0. F40."


class TestIncrementalLeaks:
    def test_a_new_section_starts_absolute(self, run_post):
        # a Custom op that ends in G91 used to leave the next operation
        # issuing incremental moves it believed were absolute
        first = op([cmd("G0", X=25.4, Y=25.4), cmd("G91"),
                    cmd("G1", X=25.4, F=mmpm(1000))], "A", 1)
        second = FakeOperation("B", [
            cmd("G0", X=0, Y=0), cmd("G1", Z=-25.4, F=mmpm(500))],
            FakeToolController(1))
        lines = run_post([first, second], "--no-header --no-write-tools")
        tail = lines[lines.index("(B)"):]
        assert tail[1] == "G90"
        assert tail[2] == "G0 X0. Y0."


class TestNoModalBookkeeping:
    def test_an_active_plane_is_tracked_even_with_modal_off(self, run_post):
        # --no-modal repeats every word, but the post still has to know
        # which plane the control is in or it forces the arc back to G17
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G18"),
            cmd("G2", X=25.4, Z=-12.7, I=12.7, K=0, F=mmpm(1016)),
        ], "--no-header --no-write-tools --no-modal")
        assert out[-1] == "G18 G2 X1. Z-0.5 I0.5 K0. F40."

    def test_incremental_mode_is_tracked_even_with_modal_off(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=0),
            cmd("G91"),
            cmd("G2", X=25.4, Y=0, I=12.7, J=0, F=mmpm(1000)),
        ], "--no-header --no-write-tools --no-modal")
        assert "X1." in out[-1] and "Y0." in out[-1]


class TestRetractModeGroup:
    def test_g99_survives_a_move_between_holes(self, run_post):
        # G98/G99 is group 10; a G0 does not cancel it, so re-emitting G98
        # silently raised every hole after the first
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G99"),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G0", X=101.6, Y=101.6),
            cmd("G81", X=101.6, Y=101.6, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert not any("G98" in line for line in out)
        assert out.count("G99") == 1

    def test_g99_survives_g80(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G99"),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G80"),
            cmd("G81", X=101.6, Y=101.6, Z=-25.4, R=2.54, F=mmpm(254)),
        ])
        assert not any("G98" in line for line in out)


class TestToolLengthOffsetOnEveryZMove:
    def test_a_helical_entry_is_not_cut_on_the_previous_offset(self,
                                                               run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G3", X=25.4, Y=25.4, Z=-3.175, I=-5.08, J=0, K=0,
                F=mmpm(1016)),
            cmd("G1", X=50.8, F=mmpm(1016)),
        ], "--no-header --no-write-tools")
        assert out[out.index("G0 X1. Y1.") + 1] == "G43 H1"
        assert out[-2].startswith("G3 ")

    def test_a_passthrough_z_move_is_not_cut_on_the_previous_offset(
            self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G38.2", Z=-25.4, F=mmpm(254)),
        ], "--no-header --no-write-tools")
        assert out[out.index("G0 X1. Y1.") + 1] == "G43 H1"


class TestAbsoluteModeSwitch:
    def test_a_cached_axis_word_does_not_survive_the_switch_back(self,
                                                                 run_post):
        # op A leaks G91; op B on the same tool (so no tool change to reset
        # anything) asks for the same X it "reached" incrementally, and the
        # word used to be suppressed -- the tool plunged where it stood
        first = op([cmd("G0", X=25.4, Y=25.4), cmd("G91"),
                    cmd("G1", X=25.4, F=mmpm(1000))], "A", 1)
        second = FakeOperation("B", [
            cmd("G0", X=25.4, Y=25.4), cmd("G1", Z=-5.0, F=mmpm(500))],
            FakeToolController(1))
        lines = run_post([first, second], "--no-header --no-write-tools")
        tail = lines[lines.index("(B)"):]
        assert tail[1:3] == ["G90", "G0 X1. Y1."]

    def test_a_redundant_g90_changes_nothing(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4),
            cmd("G90"),
            cmd("G1", X=50.8, F=mmpm(1000)),
        ])
        assert "G90" not in out
        assert out[-1] == "G1 X2. F39.4"


class TestStreamUnitWords:
    def test_a_stream_g21_does_not_rescale_an_inch_program(self, run_post):
        # every number is already scaled to the unit in the preamble; a G21
        # in an inch program has Mach4 read "X1." as one millimetre
        out = run(run_post, [cmd("G21"), cmd("G0", X=25.4, Y=25.4)])
        assert "G21" not in out
        assert out[-1] == "G0 X1. Y1."

    def test_a_stream_g20_does_not_rescale_a_metric_program(self, run_post):
        out = run(run_post, [cmd("G20"), cmd("G0", X=25.4, Y=25.4)],
                  "--no-header --no-write-tools --metric")
        assert "G20" not in out
        assert out[-1] == "G0 X25.4 Y25.4"

    def test_the_preamble_still_states_the_unit(self, run_post):
        lines = run_post(op([cmd("G21")], "A", 1), "--no-header")
        assert "G20" in lines


class TestIncrementalCycles:
    def test_g91_holes_are_not_collapsed_into_one(self, run_post):
        # three identical deltas are three holes, not one
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G91"),
            cmd("G81", X=10.0, Y=0, Z=-5.0, R=-3.0, F=mmpm(200)),
            cmd("G81", X=10.0, Y=0, Z=-5.0, R=-3.0, F=mmpm(200)),
            cmd("G81", X=10.0, Y=0, Z=-5.0, R=-3.0, F=mmpm(200)),
            cmd("G80"),
        ])
        holes = [line for line in out if "X0.3937" in line]
        assert len(holes) == 3


class TestRadiusArcsOutsideG17:
    def test_an_arc_outside_the_xy_plane_falls_back_to_ijk(self, run_post):
        # hypot(I, J) is not the radius of a G18 arc; the R that came out
        # was shorter than half the chord
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G18"),
            cmd("G2", X=25.4, Z=-25.4, I=12.7, K=-12.7, F=mmpm(1016)),
        ], "--no-header --no-write-tools --radius-arcs")
        assert out[-1] == "G2 X1. Z-1. I0.5 K-0.5 F40."


class TestRadiusArcsFromTheStream:
    def test_an_r_arc_is_not_turned_into_a_zero_radius_arc(self, run_post):
        # I/J defaulted to 0 and R was ignored, so an arc described by its
        # radius came out as G2 X.. Y.. I0. J0.
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, R=25.4, F=mmpm(1016)),
        ])
        assert out[-1] == "G2 X1. Y1. R1. F40."

    def test_an_ijk_arc_is_still_ijk(self, run_post):
        out = run(run_post, [
            cmd("G0", X=0, Y=0),
            cmd("G2", X=25.4, Y=25.4, I=25.4, J=0, F=mmpm(1016)),
        ])
        assert out[-1] == "G2 X1. Y1. I1. J0. F40."


class TestToolLengthOffsetFromTheStream:
    def test_an_explicit_g43_reframes_z(self, run_post):
        # G43 H9 moves the Z frame, so the identical Z that follows is a
        # real move and must not be suppressed
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4), cmd("G0", Z=5.0),
            cmd("G43", H=9), cmd("G0", Z=5.0),
        ])
        assert out[-2:] == ["G43 H9", "Z0.1969"]

    def test_g49_reframes_z_too(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4), cmd("G0", Z=5.0),
            cmd("G49"), cmd("G0", Z=5.0),
        ])
        assert out[-2:] == ["G49", "Z0.1969"]


class TestFrameChanges:
    """Anything that re-frames the coordinates invalidates the axis cache.

    Modal suppression is only sound while a word means the same physical
    place. Each of these used to let a real move be dropped because it
    happened to repeat a number.
    """

    def test_g92_does_not_swallow_the_move_after_it(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4), cmd("G0", Z=5.0),
            cmd("G92", X=0.0, Y=0.0),
            cmd("G0", X=25.4, Y=25.4),
        ])
        assert out[-2:] == ["G92 X0. Y0.", "X1. Y1."]

    def test_g10_does_not_swallow_the_move_after_it(self, run_post):
        out = run(run_post, [
            cmd("G0", X=25.4, Y=25.4), cmd("G0", Z=5.0),
            cmd("G10", L=2, P=1, X=0.0, Y=0.0),
            cmd("G0", X=25.4, Y=25.4),
        ])
        assert out[-1] == "X1. Y1."
