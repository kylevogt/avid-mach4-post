"""Regressions from a real FreeCAD 1.0 export.

FreeCAD does not hand the post a tidy list of operations. A job arrives as
the Job object, a ``Fixture`` pseudo operation holding just the work offset,
one ``ToolController`` object per tool holding the M6/M3 blocks, and only
then the operations that actually cut. Everything here is pinned against
output produced by a real export that got it wrong.
"""

from conftest import (
    FakeFixtureOp,
    FakeJob,
    FakeOperation,
    FakeTool,
    FakeToolController,
    FakeToolControllerOp,
    cmd,
    ipm,
)


def adaptive_op(label="Adaptive", tool_number=2, tool=None):
    """An operation as FreeCAD emits it: no M6, it is already loaded."""
    return FakeOperation(label, [
        cmd("G0", Z=5.0),
        cmd("G0", X=76.2, Y=73.0),
        cmd("G0", Z=3.0),
        cmd("G1", Z=0.0, F=ipm(80)),
        cmd("G3", Y=79.4, Z=-0.79, I=0.0, J=3.17, F=ipm(200)),
        cmd("G1", Y=73.0, F=ipm(200)),
        cmd("G0", Z=5.0),
    ], FakeToolController(tool_number,
                          tool if tool is not None else FakeTool()))


def real_job(tool=None):
    tool = tool if tool is not None else FakeTool("1/4 Flat", 6.35, 0.0)
    return [
        FakeJob(),
        FakeFixtureOp("G54"),
        FakeToolControllerOp(2, tool, "TC: 1/4 Flat"),
        adaptive_op(tool_number=2, tool=tool),
    ]


class TestOptionalStop:
    def test_no_m1_before_the_first_tool_change(self, run_post):
        # The Fixture pseudo operation used to consume "first section", so
        # the program opened with an unwanted optional stop.
        lines = run_post(real_job(), "--no-header")
        assert lines[lines.index("T2 M6") - 1] == "M5"
        assert "M1" not in lines

    def test_m1_returns_for_the_second_tool_change(self, run_post):
        job = real_job()
        job.append(FakeToolControllerOp(7, FakeTool("V Bit", 12.7),
                                        "TC: V Bit"))
        job.append(adaptive_op("Engrave", 7))
        lines = run_post(job, "--no-header")
        assert "M1" in lines
        assert lines.index("M1") > lines.index("T2 M6")
        assert lines.index("M1") < lines.index("T7 M6")
        assert lines.count("M1") == 1


class TestToolTable:
    def test_diameter_and_name_come_from_the_tool_controller(self, run_post):
        # The ToolController object owns the tool, but it has no
        # .ToolController attribute of its own, so the tool used to be lost
        # and the header read "(T2  D=0. - ZMIN=...)".
        lines = run_post(real_job(), "--no-header")
        tool_line = [line for line in lines if line.startswith("(T2")][0]
        assert "D=0.25" in tool_line
        assert "CR=0." in tool_line
        assert "14 FLAT" in tool_line  # the "/" is not comment safe

    def test_zmin_spans_every_operation_using_the_tool(self, run_post):
        job = real_job()
        job.append(adaptive_op("Deeper", 2))
        job[-1].Path.Commands.append(cmd("G1", Z=-19.05, F=ipm(80)))
        lines = run_post(job, "--no-header")
        tool_line = [line for line in lines if line.startswith("(T2")][0]
        assert "ZMIN=-0.75" in tool_line

    def test_a_tool_number_is_listed_once(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert len([line for line in lines if line.startswith("(T2")]) == 1


class TestRetracts:
    def test_startup_retract_is_not_repeated(self, run_post):
        # Fixture and ToolController both opened a section before anything
        # moved, which produced two G28 blocks back to back.
        lines = run_post(real_job(), "--no-header")
        assert lines.count("G28 G91 Z0.") == 2  # program start + program end

    def test_a_tool_change_after_cutting_still_retracts(self, run_post):
        job = real_job()
        job.append(FakeToolControllerOp(7, FakeTool("V Bit", 12.7),
                                        "TC: V Bit"))
        job.append(adaptive_op("Engrave", 7))
        lines = run_post(job, "--no-header")
        assert lines.count("G28 G91 Z0.") == 3
        assert lines.index("G28 G91 Z0.", lines.index("T2 M6")) < \
            lines.index("T7 M6")


class TestStructure:
    def test_the_work_offset_survives_the_fixture_operation(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert "G54" in lines

    def test_tool_length_offset_is_applied_after_the_change(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert "G0 G43 Z0.1969 H2" in lines

    def test_only_one_tool_change_block(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert lines.count("T2 M6") == 1
        assert lines.count("M5") == 1

    def test_program_still_ends_cleanly(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert lines[-1] == "M30"


class TestFeedUnits:
    def test_freecad_velocities_are_mm_per_second(self, run_post):
        # A 200 in/min horizontal feed is stored as 84.667 mm/s; emitting it
        # as mm/min produced F3.3 and a 60x slow program.
        lines = run_post(real_job(), "--no-header")
        assert "F200." in " ".join(lines)
        assert "F80." in " ".join(lines)
        assert "F3.3" not in " ".join(lines)

    def test_metric_output_is_mm_per_minute(self, run_post):
        lines = run_post(real_job(), "--no-header --metric")
        assert "F5080." in " ".join(lines)

    def test_override_for_paths_already_in_mm_per_minute(self, run_post):
        lines = run_post(real_job(),
                         "--no-header --metric --feed-units mm-per-minute")
        assert "F85." in " ".join(lines)
