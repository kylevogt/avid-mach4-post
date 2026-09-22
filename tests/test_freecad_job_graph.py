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
        assert "1-4 FLAT" in tool_line  # "/" is not comment safe, "-" is

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
        lines = run_post(real_job(), "")
        assert lines.count("G28 G91 Z0.") == 2  # program start + program end

    def test_no_retract_at_all_under_safe_retracts_none(self, run_post):
        lines = run_post(real_job(), "--safe-retracts none")
        assert not any(line.startswith(("G28", "G30", "G53"))
                       for line in lines)

    def test_a_tool_change_after_cutting_still_retracts(self, run_post):
        job = real_job()
        job.append(FakeToolControllerOp(7, FakeTool("V Bit", 12.7),
                                        "TC: V Bit"))
        job.append(adaptive_op("Engrave", 7))
        lines = run_post(job, "")
        assert lines.count("G28 G91 Z0.") == 3
        assert lines.index("G28 G91 Z0.", lines.index("T2 M6")) < \
            lines.index("T7 M6")


class TestStructure:
    def test_the_work_offset_survives_the_fixture_operation(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert "G54" in lines

    def test_tool_length_offset_is_applied_after_the_change(self, run_post):
        lines = run_post(real_job(), "--no-header")
        assert "G43 Z0.1969 H2" in lines

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


class TestXYBeforeZ:
    """The tool must traverse to XY before it drops towards the work.

    A real FreeCAD operation opens with ``G0 Z<clearance>`` and only then
    rapids in XY. Emitted verbatim after a ``G28`` retract that means the
    spindle plunges to a few millimetres above the table *wherever it
    happens to be parked* -- over a clamp, the vice, or the previous cut --
    and only then traverses across the work at that height.
    """

    def _body(self, lines, label="(ADAPTIVE)"):
        """Return a section's blocks, starting at its first motion."""
        body = lines[lines.index(label) + 1:]
        first = next(i for i, ln in enumerate(body) if ln.startswith("G0 "))
        return body[first:]

    def test_xy_is_positioned_before_the_first_z_descent(self, run_post):
        body = self._body(run_post(real_job(), "--no-header"))
        first_xy = next(i for i, ln in enumerate(body) if "X3." in ln)
        first_z = next(i for i, ln in enumerate(body) if "Z0.1969" in ln)
        assert first_xy < first_z

    def test_the_descent_carries_the_tool_length_offset(self, run_post):
        body = self._body(run_post(real_job(), "--no-header"))
        assert body[0] == "G0 X3. Y2.874"
        assert body[1] == "G43 Z0.1969 H2"
        assert body[2] == "Z0.1181"

    def test_no_z_word_precedes_the_traverse(self, run_post):
        body = self._body(run_post(real_job(), "--no-header"))
        before = body[:next(i for i, ln in enumerate(body) if "X3." in ln)]
        assert not any("Z" in ln for ln in before)

    def test_the_held_back_rapid_is_still_emitted(self, run_post):
        # reordered, never dropped: both clearance heights survive
        body = self._body(run_post(real_job(), "--no-header"))
        assert any("Z0.1969" in ln for ln in body)
        assert any("Z0.1181" in ln for ln in body)

    def test_a_mid_operation_z_rapid_is_not_reordered(self, run_post):
        # the retract at the end of the cut has to stay where it is, or the
        # tool would drag through the work on the way to the next position
        job = real_job()
        job[-1].Path.Commands.extend([
            cmd("G0", X=100.0, Y=100.0),
            cmd("G1", Z=-1.0, F=ipm(80)),
        ])
        body = self._body(run_post(job, "--no-header"))
        retract = next(i for i, ln in enumerate(body) if ln == "G0 Z0.1969")
        traverse = next(i for i, ln in enumerate(body) if "X3.937" in ln)
        assert retract < traverse

    def test_a_combined_rapid_keeps_its_xy(self, run_post):
        # G43 used to be hung off the Z word of a combined X/Y/Z rapid and
        # the X and Y words were dropped on the floor
        job = real_job()
        job[-1].Path.Commands[:2] = [cmd("G0", X=76.2, Y=73.0, Z=5.0)]
        body = self._body(run_post(job, "--no-header"))
        assert body[0] == "G0 X3. Y2.874"
        assert body[1] == "G43 Z0.1969 H2"

    def test_an_xy_first_operation_is_left_alone(self, run_post):
        job = real_job()
        job[-1].Path.Commands[:2] = [cmd("G0", X=76.2, Y=73.0),
                                     cmd("G0", Z=5.0)]
        body = self._body(run_post(job, "--no-header"))
        assert body[0] == "G0 X3. Y2.874"
        assert body[1] == "G43 Z0.1969 H2"

    def test_ordering_holds_for_the_second_tool(self, run_post):
        job = real_job()
        job.append(FakeToolControllerOp(7, FakeTool("V Bit", 12.7),
                                        "TC: V Bit"))
        job.append(adaptive_op("Engrave", 7))
        body = self._body(run_post(job, "--no-header"), "(ENGRAVE)")
        assert body[0] == "G0 X3. Y2.874"
        assert body[1] == "G43 Z0.1969 H7"


class TestZLiftIsNeverDeferred:
    """The reorder only applies when Z is known to be at the retract plane.

    A Z rapid issued while the tool is still down in the work is the move
    that *lifts* it clear; holding that back would drag the cutter sideways
    through the material.
    """

    def test_a_mid_section_tool_change_retracts_first(self, run_post):
        # begin_section only retracts ahead of an operation that *opens*
        # with M6; an M6 part way through one used to change tools with the
        # collet still down in the part
        operation = FakeOperation("Custom", [
            cmd("M6", T=1), cmd("M3", S=12000),
            cmd("G0", X=25.4, Y=25.4),
            cmd("G1", Z=-76.2, F=ipm(20)),
            cmd("M6", T=2), cmd("M3", S=12000),
            cmd("G0", Z=127.0),
            cmd("G0", X=101.6, Y=101.6),
        ], FakeToolController(1))
        lines = run_post(operation, "--no-write-tools")
        cut = lines.index("G1 G43 Z-3. H1 F20.")
        assert lines.index("G28 G91 Z0.", cut) < lines.index("T2 M6")
        # and with Z genuinely parked the traverse leads again
        after = lines[lines.index("T2 M6"):]
        assert after[3:5] == ["G0 X4. Y4.", "G43 Z5. H2"]

    def test_g53_mode_lifts_before_it_traverses(self, run_post):
        # --safe-retracts g53 used to emit no Z retract at all, so the tool
        # stayed at cutting depth through the tool change and the traverse
        def op(label, tool):
            return FakeOperation(label, [
                cmd("M6", T=tool), cmd("M3", S=12000),
                cmd("G0", Z=127.0),
                cmd("G0", X=101.6, Y=101.6),
                cmd("G1", Z=-25.4, F=ipm(20)),
            ], FakeToolController(tool))

        lines = run_post([op("A", 1), op("B", 2)],
                         "--no-header --no-write-tools --safe-retracts g53")
        cut = lines.index("G1 Z-1. F20.")
        assert lines.index("G53 G0 Z0.", cut) < lines.index("T2 M6")
        # and with Z genuinely parked the traverse may lead again
        after = lines[lines.index("T2 M6"):]
        assert after[3:5] == ["G0 X4. Y4.", "G43 Z5. H2"]


class TestNothingIsLost:
    def test_every_commanded_z_height_reaches_the_program(self, run_post):
        job = real_job()
        lines = run_post(job, "--no-header --no-write-tools")
        wanted = ["Z0.1969", "Z0.1181", "Z0.", "Z-0.0311"]
        for word in wanted:
            assert any(word in line.split(" ")[-1] or
                       (" " + word + " ") in " " + line + " "
                       for line in lines), word

    def test_a_section_whose_last_move_is_a_held_back_z(self, run_post):
        # nothing follows the deferred rapid, so the flush at the end of the
        # section has to emit it
        job = real_job()
        job[-1].Path.Commands = [cmd("G0", Z=5.0)]
        lines = run_post(job, "--no-header --no-write-tools")
        assert "G0 G43 Z0.1969 H2" in lines

    def test_a_section_with_no_xy_move_at_all(self, run_post):
        job = real_job()
        job[-1].Path.Commands = [cmd("G0", Z=5.0), cmd("G1", Z=0.0,
                                                       F=ipm(80))]
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 G43 Z0.1969 H2") < lines.index("G1 Z0. F80.")

    def test_a_comment_between_the_z_and_the_xy_does_not_flush(self,
                                                               run_post):
        job = real_job()
        job[-1].Path.Commands.insert(1, cmd("(ramp in)"))
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 X3. Y2.874") < lines.index("G43 Z0.1969 H2")

    def test_the_order_holds_without_modal_suppression(self, run_post):
        lines = run_post(real_job(), "--no-header --no-write-tools "
                                     "--no-modal")
        traverse = next(i for i, ln in enumerate(lines) if "X3." in ln)
        descent = next(i for i, ln in enumerate(lines) if "G43" in ln)
        assert traverse < descent
        assert lines[descent].startswith("G0 G43 ")


class TestReorderSurvivesStateWords:
    """State words between the Z rapid and the XY rapid must not defeat it.

    FreeCAD's Drilling op in particular repeats ``G90``/``G98`` inside the
    path, and a flush on every one of them made the reorder silently do
    nothing for exactly the operations that plunge.
    """

    def test_a_plane_or_retract_mode_word_passes_through(self, run_post):
        job = real_job()
        job[-1].Path.Commands[1:1] = [cmd("G17"), cmd("G98")]
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 X3. Y2.874") < lines.index("G43 Z0.1969 H2")

    def test_the_spindle_word_passes_through(self, run_post):
        job = real_job()
        job[-1].Path.Commands[1:1] = [cmd("M3", S=18000)]
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 X3. Y2.874") < lines.index("G43 Z0.1969 H2")

    def test_a_work_offset_change_still_flushes(self, run_post):
        # G55 moves the frame the held back Z would be measured in
        job = real_job()
        job[-1].Path.Commands[1:1] = [cmd("G55")]
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 G43 Z0.1969 H2") < lines.index("G55")

    def test_a_coordinate_mode_change_still_flushes(self, run_post):
        job = real_job()
        job[-1].Path.Commands[1:1] = [cmd("G91")]
        lines = run_post(job, "--no-header --no-write-tools")
        assert lines.index("G0 G43 Z0.1969 H2") < lines.index("G91")


class TestRealDrillingStream:
    """The shape FreeCAD 1.0's Drilling operation actually emits.

    ``G0 Z<clearance>``, then ``G90``, then ``G98``/``G99``, then a
    ``G0 X Y`` per hole. The restated ``G90`` used to flush the held back Z
    rapid, so the one operation that always follows a tool change was also
    the one the reorder never helped.
    """

    def drilling_job(self, retract="G98"):
        job = real_job()
        job[-1] = FakeOperation("Drilling", [
            cmd("G0", Z=15.0),
            cmd("G90"),
            cmd(retract),
            cmd("G0", X=76.2, Y=73.0),
            cmd("G81", X=76.2, Y=73.0, Z=-10.0, R=2.0, F=ipm(8)),
            cmd("G0", X=101.6, Y=73.0),
            cmd("G81", X=101.6, Y=73.0, Z=-10.0, R=2.0, F=ipm(8)),
            cmd("G80"),
            cmd("G0", Z=15.0),
        ], FakeToolController(2, FakeTool("1/4 Flat", 6.35, 0.0)))
        return job

    def test_xy_is_positioned_before_the_first_hole_descent(self, run_post):
        lines = run_post(self.drilling_job(), "--no-header --no-write-tools")
        body = lines[lines.index("(DRILLING)"):]
        assert body.index("G0 X3. Y2.874") < \
            next(i for i, ln in enumerate(body) if "Z0.5906" in ln)

    def test_the_clearance_rapid_carries_the_tool_length_offset(self,
                                                                run_post):
        lines = run_post(self.drilling_job(), "--no-header --no-write-tools")
        assert "G43 Z0.5906 H2" in lines

    def test_a_g99_retract_mode_is_preserved(self, run_post):
        lines = run_post(self.drilling_job("G99"),
                         "--no-header --no-write-tools")
        assert "G99" in lines
        assert not any("G98" in line for line in lines)

    def test_both_holes_are_drilled(self, run_post):
        lines = run_post(self.drilling_job(), "--no-header --no-write-tools")
        body = lines[lines.index("(DRILLING)"):lines.index("G80")]
        assert "G98" in body  # from the stream, not restated by the cycle
        assert "G81 X3. Y2.874 Z-0.3937 R0.0787 F8." in body
        assert "G0 X4." in body
