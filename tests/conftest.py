"""Lightweight stand-ins for the FreeCAD objects the post processor reads.

The post processor only ever duck types its way through FreeCAD objects
(``obj.Path.Commands``, ``cmd.Name``, ``cmd.Parameters``, ...), so the tests
can run under plain CPython without a FreeCAD installation.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeCommand:
    def __init__(self, name, parameters=None):
        self.Name = name
        self.Parameters = dict(parameters or {})


class FakePath:
    def __init__(self, commands):
        self.Commands = list(commands)


class FakeTool:
    def __init__(self, label="6mm Endmill", diameter=6.0, corner_radius=0.0):
        self.Label = label
        self.Diameter = diameter
        self.CornerRadius = corner_radius


class FakeToolController:
    def __init__(self, number=1, tool=None):
        self.ToolNumber = number
        self.Tool = tool if tool is not None else FakeTool()


class FakeOperation:
    """Stands in for a CAM operation object."""

    def __init__(self, label, commands, tool_controller=None,
                 coolant="None", active=True, name=None):
        self.Label = label
        self.Name = name or label.replace(" ", "")
        self.Path = FakePath(commands)
        self.ToolController = tool_controller
        self.CoolantMode = coolant
        self.Active = active


class FakeMachine:
    def __init__(self, vendor="Avid CNC", model="PRO4896", description=""):
        self.Vendor = vendor
        self.Model = model
        self.Description = description


class FakeJob:
    def __init__(self, label="Job", machine=None):
        self.Name = "Job"
        self.Label = label
        self.Machine = machine if machine is not None else FakeMachine()


def cmd(name, **parameters):
    """Shorthand for building a command: ``cmd("G1", X=1, Y=2, F=100)``."""
    return FakeCommand(name, parameters)


@pytest.fixture
def post():
    import avid_mach4_post

    return avid_mach4_post


@pytest.fixture
def run_post():
    """Return a helper that posts operations and yields the g-code lines."""
    import avid_mach4_post

    def _run(operations, argstring="--no-header"):
        if not isinstance(operations, (list, tuple)):
            operations = [operations]
        gcode = avid_mach4_post.export(list(operations), "-", argstring)
        return gcode.splitlines()

    return _run
