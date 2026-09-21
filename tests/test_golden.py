"""End-to-end check against a checked-in reference program.

``UPDATE_GOLDEN=1 pytest tests/test_golden.py`` rewrites the reference file
after an intentional change; review the diff before committing it.
"""

import datetime
import os
import pathlib

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
            cmd("G1", Z=-3.175, F=762.0),
            cmd("G1", X=101.6, Y=0.0, F=2540.0),
            cmd("G2", X=127.0, Y=25.4, I=0.0, J=25.4, F=2540.0),
            cmd("G1", X=127.0, Y=76.2, F=2540.0),
            cmd("G3", X=101.6, Y=101.6, I=-25.4, J=0.0, F=2540.0),
            cmd("G1", X=0.0, Y=101.6, F=2540.0),
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
            cmd("G83", X=12.7, Y=12.7, Z=-19.05, R=2.54, Q=3.175, F=381.0),
            cmd("G83", X=114.3, Y=12.7, Z=-19.05, R=2.54, Q=3.175, F=381.0),
            cmd("G83", X=114.3, Y=88.9, Z=-19.05, R=2.54, Q=3.175, F=381.0),
            cmd("G83", X=12.7, Y=88.9, Z=-19.05, R=2.54, Q=3.175, F=381.0),
            cmd("G80"),
            cmd("G4", P=0.5),
        ],
        FakeToolController(5, FakeTool("4mm Drill", 4.0, 0.0)),
        coolant="None",
    )

    return [job, contour, drilling]


def generate(argstring):
    post = AvidPost(process_arguments(argstring))
    return post.build(sample_job(), now=TIMESTAMP)


@pytest.mark.parametrize("name,argstring", [
    ("imperial.tap", ""),
    ("metric.tap", "--metric"),
    ("line_numbers.tap", "--line-numbers --safe-retracts g53"),
])
def test_matches_reference_program(name, argstring):
    gcode = generate(argstring)
    reference = FIXTURES / name
    if os.environ.get("UPDATE_GOLDEN"):
        reference.write_text(gcode)
    assert reference.exists(), (
        f"missing reference {reference}; run UPDATE_GOLDEN=1 pytest")
    assert gcode == reference.read_text()
