"""Number, word and comment formatting.

The AVID post inherits Fusion's ``createFormat``/``createVariable``
behaviour; these tests pin the parts that are visible in the g-code.
"""

import pytest

from avid_mach4_post import (
    Formatter,
    Modal,
    OutputVariable,
    format_comment,
)


class TestFormatter:
    @pytest.mark.parametrize("value,expected", [
        (0, "0."),
        (1, "1."),
        (1.5, "1.5"),
        (-1.25, "-1.25"),
        (1.23456789, "1.2346"),
        (-0.00001, "0."),  # never emit "-0."
    ])
    def test_trailing_zeros_trimmed_decimal_point_kept(self, value, expected):
        assert Formatter(decimals=4).format(value) == expected

    def test_prefix_is_applied(self):
        assert Formatter(decimals=3, prefix="X").format(2) == "X2."

    def test_scale_converts_millimetres_to_inches(self):
        inches = Formatter(decimals=4, scale=1.0 / 25.4)
        assert inches.format(25.4) == "1."
        assert inches.format(12.7) == "0.5"

    def test_integers_without_forced_decimal(self):
        assert Formatter(decimals=0, force_decimal=False).format(12000) == \
            "12000"

    def test_none_formats_to_empty_string(self):
        assert Formatter().format(None) == ""

    def test_are_different_compares_formatted_values(self):
        fmt = Formatter(decimals=3)
        assert not fmt.are_different(1.00001, 1.00002)
        assert fmt.are_different(1.0, 1.01)


class TestOutputVariable:
    def test_repeated_value_is_suppressed(self):
        var = OutputVariable(Formatter(decimals=4, prefix="X"))
        assert var.format(1.0) == "X1."
        assert var.format(1.0) == ""
        assert var.format(2.0) == "X2."

    def test_reset_forces_the_next_output(self):
        var = OutputVariable(Formatter(decimals=4, prefix="Z"))
        var.format(1.0)
        var.reset()
        assert var.format(1.0) == "Z1."

    def test_force_always_outputs(self):
        var = OutputVariable(Formatter(decimals=4, prefix="I"), force=True)
        assert var.format(0.0) == "I0."
        assert var.format(0.0) == "I0."

    def test_disabled_variable_is_silent(self):
        var = OutputVariable(Formatter(decimals=3, prefix="A"))
        var.disable()
        assert var.format(45.0) == ""


class TestModal:
    def test_only_changes_are_emitted(self):
        modal = Modal(Formatter(decimals=1, prefix="G", force_decimal=False))
        assert modal.format(0) == "G0"
        assert modal.format(0) == ""
        assert modal.format(1) == "G1"

    def test_reset_re_emits(self):
        modal = Modal(Formatter(decimals=1, prefix="G", force_decimal=False))
        modal.format(90)
        modal.reset()
        assert modal.format(90) == "G90"

    def test_fractional_codes_keep_their_decimal(self):
        modal = Modal(Formatter(decimals=1, prefix="G", force_decimal=False))
        assert modal.format(91.1) == "G91.1"


class TestComments:
    def test_comment_is_upper_cased_and_wrapped(self):
        assert format_comment("Face the stock") == "(FACE THE STOCK)"

    def test_unsupported_characters_are_dropped(self):
        # Mach4 chokes on nested parens, colons and slashes
        assert format_comment("Pocket (2): 1/2\" tool") == "(POCKET 2 12 TOOL)"

    def test_permitted_punctuation_survives(self):
        assert format_comment("D=0.25, CR=0.0_A-B") == "(D=0.25, CR=0.0_A-B)"
