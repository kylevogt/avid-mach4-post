"""Tool changes, spindle, work offsets and coolant."""

from conftest import (
    FakeOperation,
    FakeTool,
    FakeToolController,
    cmd,
    mmpm,
)


def op(label, tool, coolant="None", extra=()):
    commands = [cmd("M6", T=tool), cmd("M3", S=12000),
                cmd("G0", X=0, Y=0), cmd("G0", Z=5.08)]
    commands.extend(extra)
    return FakeOperation(label, commands,
                         FakeToolController(tool, FakeTool(f"T{tool}")),
                         coolant=coolant)


class TestToolChange:
    def test_change_sequence_matches_the_expected_shape(self, run_post):
        lines = run_post(op("Contour", 1), "--no-header --no-write-tools")
        start = lines.index("(CONTOUR)")
        assert lines[start + 1:start + 6] == [
            "M5", "T1 M6", "S12000 M3", "G54", "G0 X0. Y0."]

    def test_retract_precedes_every_tool_change(self, run_post):
        # the spindle is parked at the top of Z when the tool is swapped
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-write-tools")
        second = lines.index("(POCKET)")
        assert lines[second - 3:second - 1] == ["G28 G91 Z0.", "G90"]

    def test_safe_retracts_none_changes_tools_where_it_stands(self,
                                                              run_post):
        # what the AVID Fusion post does with useG28 off: the change
        # happens at the operation's own clearance height
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-write-tools --safe-retracts none")
        assert not any(line.startswith("G28") for line in lines)

    def test_optional_stop_is_skipped_on_the_first_tool_change(
            self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-header --no-write-tools")
        first = lines.index("(CONTOUR)")
        second = lines.index("(POCKET)")
        assert "M1" not in lines[first:second]
        assert "M1" in lines[second:]

    def test_optional_stop_can_be_disabled(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-header --no-write-tools --no-optional-stop")
        assert "M1" not in lines

    def test_spindle_is_stopped_before_the_change(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-header --no-write-tools")
        second = lines.index("(POCKET)")
        assert lines[second + 1] == "M5"

    def test_no_use_m6_emits_the_bare_t_word(self, run_post):
        lines = run_post(op("Contour", 1),
                         "--no-header --no-write-tools --no-use-m6")
        assert "T1" in lines
        assert "T1 M6" not in lines

    def test_counter_clockwise_spindle(self, run_post):
        operation = FakeOperation("Contour", [
            cmd("M6", T=1), cmd("M4", S=9000), cmd("G0", X=0, Y=0)],
            FakeToolController(1))
        assert "S9000 M4" in run_post(operation,
                                      "--no-header --no-write-tools")


class TestToolLengthOffset:
    def test_g43_rides_on_the_first_z_move_after_the_traverse(self,
                                                              run_post):
        # the control is already in G0 from the XY traverse, so the block
        # carries no motion word of its own
        lines = run_post(op("Contour", 2), "--no-header --no-write-tools")
        assert lines[lines.index("G0 X0. Y0.") + 1] == "G43 Z0.2 H2"

    def test_g43_rides_a_plunge_when_there_is_no_z_rapid(self, run_post):
        # an operation whose first Z move is the cut used to run the whole
        # pass on the previous tool's offset and only pick up G43 on the
        # retract afterwards
        operation = FakeOperation("Contour", [
            cmd("M6", T=3), cmd("M3", S=12000),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G1", Z=-3.175, F=mmpm(500)),
            cmd("G1", X=50.8, F=mmpm(1000)),
            cmd("G0", Z=5.08),
        ], FakeToolController(3))
        lines = run_post(operation, "--no-header --no-write-tools")
        g43 = [line for line in lines if "G43" in line]
        assert g43 == ["G1 G43 Z-0.125 H3 F19.7"]
        assert lines.index(g43[0]) < lines.index("X2. F39.4")

    def test_g43_precedes_a_cycle_that_has_no_plain_z_move(self, run_post):
        operation = FakeOperation("Drill", [
            cmd("M6", T=3), cmd("M3", S=12000),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G81", X=25.4, Y=25.4, Z=-25.4, R=2.54, F=mmpm(254)),
            cmd("G80"),
        ], FakeToolController(3))
        lines = run_post(operation, "--no-header --no-write-tools")
        assert "G43 H3" in lines
        assert lines.index("G43 H3") < \
            next(i for i, ln in enumerate(lines) if "G81" in ln)

    def test_g43_is_emitted_once_per_tool_change(self, run_post):
        lines = run_post(op("Contour", 1, extra=[cmd("G0", Z=10.16)]),
                         "--no-header --no-write-tools")
        assert len([line for line in lines if "G43" in line]) == 1

    def test_g43_can_be_disabled(self, run_post):
        lines = run_post(
            op("Contour", 1),
            "--no-header --no-write-tools --no-tool-length-offset")
        assert not any("G43" in line for line in lines)

    def test_explicit_g43_in_the_stream_is_passed_through(self, run_post):
        operation = FakeOperation("Contour", [
            cmd("M6", T=4), cmd("M3", S=12000), cmd("G43", H=4),
            cmd("G0", X=0, Y=0)], FakeToolController(4))
        lines = run_post(operation, "--no-header --no-write-tools")
        assert lines.count("G43 H4") == 1


class TestPreloadTool:
    def test_next_tool_is_preloaded(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 7)],
                         "--no-header --no-write-tools --preload-tool")
        assert lines[lines.index("T1 M6") + 1] == "T7"

    def test_last_change_preloads_the_first_tool(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 7)],
                         "--no-header --no-write-tools --preload-tool")
        assert lines[lines.index("T7 M6") + 1] == "T1"

    def test_preloading_is_off_by_default(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 7)],
                         "--no-header --no-write-tools")
        assert lines[lines.index("T1 M6") + 1] != "T7"


class TestWorkOffset:
    def test_g54_is_assumed_when_the_job_specifies_nothing(self, run_post):
        assert "G54" in run_post(op("Contour", 1),
                                 "--no-header --no-write-tools")

    def test_fixture_from_the_command_stream_wins(self, run_post):
        operation = FakeOperation("Contour", [
            cmd("M6", T=1), cmd("M3", S=12000), cmd("G55"),
            cmd("G0", X=0, Y=0)], FakeToolController(1))
        lines = run_post(operation, "--no-header --no-write-tools")
        assert "G55" in lines
        assert "G54" not in lines

    def test_work_offset_is_not_repeated(self, run_post):
        operation = FakeOperation("Contour", [
            cmd("M6", T=1), cmd("M3", S=12000), cmd("G54"),
            cmd("G0", X=0, Y=0), cmd("G54"), cmd("G0", X=10, Y=10)],
            FakeToolController(1))
        lines = run_post(operation, "--no-header --no-write-tools")
        assert lines.count("G54") == 1

    def test_work_offset_is_forced_again_after_a_tool_change(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-header --no-write-tools")
        assert lines.count("G54") == 2


class TestCoolant:
    def test_flood_becomes_m8_after_the_spindle_starts(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools")
        assert lines[lines.index("S12000 M3") + 1] == "M8"

    def test_mist_becomes_m7(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Mist"),
                         "--no-header --no-write-tools")
        assert "M7" in lines

    def test_no_coolant_emits_nothing(self, run_post):
        lines = run_post(op("Contour", 1), "--no-header --no-write-tools")
        assert "M8" not in lines
        assert "M7" not in lines

    def test_coolant_is_turned_off_at_the_next_tool_change(self, run_post):
        lines = run_post([op("Contour", 1, coolant="Flood"),
                          op("Pocket", 2)],
                         "--no-header --no-write-tools")
        second = lines.index("(POCKET)")
        assert lines[second + 1:second + 3] == ["M5", "M9"]

    def test_coolant_is_turned_off_at_program_end(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-write-tools")
        assert lines[lines.index("M30") - 5] == "M9"

    def test_coolant_translation_can_be_disabled(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools --no-coolant")
        assert "M8" not in lines


class TestDustCollector:
    def test_m7_in_the_header_and_m9_in_the_footer(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-write-tools --dust-collector")
        assert lines.index("M7") < lines.index("(CONTOUR)")
        assert lines[lines.index("M30") - 5] == "M9"

    def test_operation_coolant_is_ignored(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools --dust-collector")
        assert "M8" not in lines
        assert lines.count("M7") == 1


def loaded_op(label, tool, coolant="None"):
    """An operation on an already loaded tool -- no M6 to mask anything."""
    return FakeOperation(label, [
        cmd("G0", X=76.2, Y=76.2), cmd("G0", Z=5.08),
        cmd("G1", Z=-1.0, F=mmpm(500))],
        FakeToolController(tool, FakeTool(f"T{tool}")), coolant=coolant)


class TestCoolantTransitions:
    """Changes of coolant mode between operations on the same tool.

    A tool change already forces M9, so these all run without one -- that
    is the path where "next operation wants it off" was ignored.
    """

    def test_coolant_is_turned_off_when_the_next_operation_wants_it_off(
            self, run_post):
        # Flood then None used to emit M8 and then nothing at all, leaving
        # it running until the footer
        lines = run_post([op("Rough", 1, coolant="Flood"),
                          loaded_op("Finish", 1, coolant="None")],
                         "--no-header --no-write-tools")
        tail = lines[lines.index("(FINISH)"):]
        assert "M9" in tail
        assert tail.index("M9") < tail.index("X3. Y3.")

    def test_an_unchanged_mode_is_not_repeated(self, run_post):
        lines = run_post([op("Rough", 1, coolant="Flood"),
                          loaded_op("Finish", 1, coolant="Flood")],
                         "--no-header --no-write-tools")
        tail = lines[lines.index("(FINISH)"):]
        assert "M8" not in tail
        assert "M9" not in tail[:tail.index("X3. Y3.")]

    def test_switching_mist_to_flood_does_not_leave_both_on(self, run_post):
        lines = run_post([op("Rough", 1, coolant="Mist"),
                          loaded_op("Finish", 1, coolant="Flood")],
                         "--no-header --no-write-tools")
        tail = lines[lines.index("(FINISH)"):]
        assert "M8" in tail


class TestToolTable:
    def test_an_inactive_operation_is_not_in_the_tool_table(self, run_post):
        ghost = op("Ghost", 9)
        ghost.Active = False
        lines = run_post([op("Contour", 1), ghost], "--no-header")
        assert not any("T9" in line for line in lines)

    def test_an_inactive_operation_is_not_preloaded(self, run_post):
        ghost = op("Ghost", 9)
        ghost.Active = False
        lines = run_post([op("Contour", 1), ghost],
                         "--no-header --no-write-tools --preload-tool")
        assert "T9" not in lines


class TestToolNumberResolution:
    def test_an_m6_without_a_t_word_uses_the_tool_controller(self, run_post):
        operation = FakeOperation("Contour", [
            cmd("M6"), cmd("M3", S=12000),
            cmd("G0", X=0, Y=0), cmd("G0", Z=5.08),
        ], FakeToolController(6))
        lines = run_post(operation, "--no-header --no-write-tools")
        assert "T6 M6" in lines
        assert "G43 Z0.2 H6" in lines

    def test_an_m6_with_no_tool_anywhere_does_not_unload_the_spindle(
            self, run_post):
        # "T0 M6" orders the machine to put the tool away
        operation = FakeOperation("Contour", [
            cmd("M6"), cmd("M3", S=12000), cmd("G0", X=0, Y=0)], None)
        lines = run_post(operation, "--no-header --no-write-tools")
        assert not any(line.startswith("T0") for line in lines)
        assert "T0 M6" not in lines
