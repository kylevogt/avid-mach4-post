"""Tool changes, spindle, work offsets and coolant."""

from conftest import FakeOperation, FakeTool, FakeToolController, cmd


def op(label, tool, coolant="None", extra=()):
    commands = [cmd("M6", T=tool), cmd("M3", S=12000),
                cmd("G0", X=0, Y=0), cmd("G0", Z=5.08)]
    commands.extend(extra)
    return FakeOperation(label, commands,
                         FakeToolController(tool, FakeTool(f"T{tool}")),
                         coolant=coolant)


class TestToolChange:
    def test_change_sequence_matches_the_avid_post(self, run_post):
        lines = run_post(op("Contour", 1), "--no-header --no-write-tools")
        start = lines.index("(CONTOUR)")
        assert lines[start + 1:start + 6] == [
            "M5", "T1 M6", "S12000 M3", "G54", "G0 X0. Y0."]

    def test_retract_precedes_every_tool_change(self, run_post):
        lines = run_post([op("Contour", 1), op("Pocket", 2)],
                         "--no-header --no-write-tools")
        second = lines.index("(POCKET)")
        assert lines[second - 3:second - 1] == ["G28 G91 Z0.", "G90"]

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
    def test_g43_rides_on_the_first_z_rapid(self, run_post):
        lines = run_post(op("Contour", 2), "--no-header --no-write-tools")
        assert "G43 Z0.2 H2" in lines

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
                         "--no-header --no-write-tools")
        assert lines[lines.index("M30") - 5] == "M9"

    def test_coolant_translation_can_be_disabled(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools --no-coolant")
        assert "M8" not in lines


class TestDustCollector:
    def test_m7_in_the_header_and_m9_in_the_footer(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools --dust-collector")
        assert lines.index("M7") < lines.index("(CONTOUR)")
        assert lines[lines.index("M30") - 5] == "M9"

    def test_operation_coolant_is_ignored(self, run_post):
        lines = run_post(op("Contour", 1, coolant="Flood"),
                         "--no-header --no-write-tools --dust-collector")
        assert "M8" not in lines
        assert lines.count("M7") == 1
