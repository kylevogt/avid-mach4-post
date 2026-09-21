"""Argument parsing for the ``argstring`` FreeCAD hands to the post."""

from avid_mach4_post import process_arguments


class TestDefaults:
    def test_defaults_match_the_avid_fusion_post(self):
        args = process_arguments("")
        assert args.inches is True
        assert args.comments is True
        assert args.write_machine is True
        assert args.write_tools is True
        assert args.use_m6 is True
        assert args.optional_stop is True
        assert args.preload_tool is False
        assert args.line_numbers is False
        assert args.line_number_start == 10
        assert args.line_number_increment == 5
        assert args.radius_arcs is False
        assert args.dwell_in_seconds is True
        assert args.dust_collector is False
        assert args.safe_retracts == "g28"
        assert args.spaces is True

    def test_precision_defaults_are_resolved_per_unit(self):
        assert process_arguments("").precision is None


class TestOverrides:
    def test_negated_flags(self):
        args = process_arguments("--no-comments --no-optional-stop --no-use-m6")
        assert args.comments is False
        assert args.optional_stop is False
        assert args.use_m6 is False

    def test_metric_switch(self):
        assert process_arguments("--metric").inches is False

    def test_retract_choice(self):
        assert process_arguments("--safe-retracts g30").safe_retracts == "g30"

    def test_quoted_values(self):
        args = process_arguments('--preamble "G53 G0 Z0;M8"')
        assert args.preamble == "G53 G0 Z0;M8"

    def test_invalid_arguments_return_none(self):
        assert process_arguments("--safe-retracts nonsense") is None
        assert process_arguments("--not-a-real-flag") is None
