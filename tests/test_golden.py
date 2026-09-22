"""End-to-end check against a checked-in reference program.

``UPDATE_GOLDEN=1 pytest tests/test_golden.py`` rewrites the reference file
after an intentional change; review the diff before committing it.
"""

import datetime
import os
import pathlib

import pytest
from conftest import (
    FakeFixtureOp,
    FakeJob,
    FakeMachine,
    FakeOperation,
    FakeTool,
    FakeToolController,
    FakeToolControllerOp,
    cmd,
    mmpm,
)

from avid_mach4_post import AvidPost, process_arguments

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TIMESTAMP = datetime.datetime(2024, 3, 17, 9, 30, 0)


def sample_job():
    """A two operation job: a contoured pocket followed by drilling."""
    job = FakeJob("Sign Blank", FakeMachine("Avid CNC", "PRO4848", "Mach4"))

    contour = FakeOperation(
        "Profile Outside",
        [
            cmd("(Profile Outside)"),
            cmd("M6", T=1),
            cmd("M3", S=18000),
            cmd("G0", X=0.0, Y=0.0),
            cmd("G0", Z=5.08),
            cmd("G1", Z=-3.175, F=mmpm(762.0)),
            cmd("G1", X=101.6, Y=0.0, F=mmpm(2540.0)),
            cmd("G2", X=127.0, Y=25.4, I=0.0, J=25.4, K=0.0,
                F=mmpm(2540.0)),
            cmd("G1", X=127.0, Y=76.2, F=mmpm(2540.0)),
            cmd("G3", X=101.6, Y=101.6, I=-25.4, J=0.0, K=0.0,
                F=mmpm(2540.0)),
            cmd("G1", X=0.0, Y=101.6, F=mmpm(2540.0)),
            cmd("G0", Z=5.08),
        ],
        FakeToolController(1, FakeTool("Flat End Mill", 6.35, 0.0)),
        coolant="Mist",
    )

    drilling = FakeOperation(
        "Drill Mounting Holes",
        [
            cmd("(Drill Mounting Holes)"),
            cmd("M6", T=5),
            cmd("M3", S=9000),
            cmd("G0", X=12.7, Y=12.7),
            cmd("G0", Z=5.08),
            cmd("G83", X=12.7, Y=12.7, Z=-19.05, R=2.54, Q=3.175, F=mmpm(381.0)),
            cmd("G83", X=114.3, Y=12.7, Z=-19.05, R=2.54, Q=3.175, F=mmpm(381.0)),
            cmd("G83", X=114.3, Y=88.9, Z=-19.05, R=2.54, Q=3.175, F=mmpm(381.0)),
            cmd("G83", X=12.7, Y=88.9, Z=-19.05, R=2.54, Q=3.175, F=mmpm(381.0)),
            cmd("G80"),
            cmd("G4", P=0.5),
        ],
        FakeToolController(5, FakeTool("4mm Drill", 4.0, 0.0)),
        coolant="None",
    )

    return [job, contour, drilling]


def freecad_job_graph():
    """The shape FreeCAD really exports: Job, Fixture, ToolController, op.

    Keeping this as a golden makes the start-up retract, the optional stop
    and the tool table visible in a diff whenever they move.
    """
    tool = FakeTool("1/4 Flat", 6.35, 0.0)
    adaptive = FakeOperation("Adaptive", [
        cmd("(Adaptive)"),
        cmd("G0", Z=5.0),
        cmd("G0", X=76.2, Y=73.0),
        cmd("G0", Z=3.0),
        cmd("G1", Z=0.0, F=mmpm(2032.0)),
        cmd("G3", Y=79.4, Z=-0.79, I=0.0, J=3.17, K=0.0, F=mmpm(5080.0)),
        cmd("G1", Y=73.0, F=mmpm(5080.0)),
        cmd("G0", Z=5.0),
    ], FakeToolController(2, tool), coolant="None")
    return [FakeJob("Paths Test"), FakeFixtureOp("G54"),
            FakeToolControllerOp(2, tool, "TC: 1/4 Flat"), adaptive]


IN = 25.4


def avid_fusion_shape():
    """The FreeCAD equivalent of a real AVID/Fusion 360 export.

    Pinned against ``2D Adaptive1`` from a program posted by AVID's own
    Fusion post. Posted with ``--safe-retracts none`` and nothing else, so
    this is the file that says whether the rest still matches Fusion: the
    same preamble, the same header (program name, machine if the job names
    one, tools -- no timestamp), the same tool-change sequence, the same
    arcs, and a bare ``M30`` at the end.

    Two blocks deliberately do *not* match. Fusion writes the approach as
    ``G0 X.. Y..`` then ``G43 Z.. H1``, because it is handed the section's
    initial position and composes the move itself. This post is handed a
    flat command stream and passes FreeCAD's own order through, so those
    two come out the other way round. FreeCAD goes to clearance height
    first and traverses there, which is what the clearance plane is for.

    The job deliberately carries no machine information, which is the
    common FreeCAD case and what the reference file shows.
    """
    def i(value):
        return value * IN

    tool = FakeTool("Flat End Mill", i(0.25), 0.0)
    adaptive = FakeOperation("2D Adaptive1", [
        cmd("G0", Z=i(0.6)),
        cmd("G0", X=i(3.0938), Y=i(3.0829)),
        cmd("G1", Z=i(0.2), F=mmpm(100 * IN)),
        cmd("G1", Z=i(0.1)),
        cmd("G3", X=i(2.9101), Y=i(3.0871), Z=i(0.0636),
            I=i(-0.0938), J=i(-0.0829), F=mmpm(30 * IN)),
        cmd("G3", X=i(2.92), Y=i(2.9046), Z=i(0.0273),
            I=i(0.0894), J=i(-0.0867)),
        cmd("G3", X=i(2.8804), I=i(-0.1188), J=0.0),
        cmd("G3", X=i(3.1181), I=i(0.1188), J=0.0),
        cmd("G1", X=i(3.1182), Y=i(3.0088), F=mmpm(100 * IN)),
        cmd("G1", X=i(3.1244), Y=i(3.0444)),
        cmd("G0", Z=i(0.6)),
    ], FakeToolController(1, tool), coolant="None")
    return [FakeJob("FusionExample", FakeMachine("", "", "")),
            FakeFixtureOp("G54"),
            FakeToolControllerOp(1, tool, "TC: Flat End Mill", speed=20000),
            adaptive]


def generate(argstring):
    post = AvidPost(process_arguments(argstring))
    return post.build(sample_job(), now=TIMESTAMP)


@pytest.mark.parametrize("name,argstring", [
    ("imperial.tap", ""),
    ("metric.tap", "--metric"),
    ("header.tap", "--header"),
    ("line_numbers.tap", "--line-numbers --safe-retracts g53"),
])
def test_matches_reference_program(name, argstring):
    _assert_matches(generate(argstring), name)


def test_matches_reference_avid_fusion_shape():
    post = AvidPost(process_arguments("--safe-retracts none"))
    _assert_matches(post.build(avid_fusion_shape(), now=TIMESTAMP),
                    "avid_fusion_shape.tap")


def test_matches_reference_freecad_job_graph():
    post = AvidPost(process_arguments(""))
    _assert_matches(post.build(freecad_job_graph(), now=TIMESTAMP),
                    "freecad_job_graph.tap")


def _assert_matches(gcode, name):
    reference = FIXTURES / name
    if os.environ.get("UPDATE_GOLDEN"):
        reference.write_text(gcode)
    assert reference.exists(), (
        f"missing reference {reference}; run UPDATE_GOLDEN=1 pytest")
    assert gcode == reference.read_text()
