"""Tests for SCPICommand — parsed command object."""

from hislip_instruments.command import SCPICommand


class TestSCPICommandParsing:
    """Basic parsing of raw command strings."""

    def test_basic_parse(self):
        cmd = SCPICommand("CONF:VOLT:DC 10,0.001", "CONF:VOLT:DC")
        assert cmd.raw == "CONF:VOLT:DC 10,0.001"
        assert cmd.prefix == "CONF:VOLT:DC"
        assert cmd.args == ["10", "0.001"]
        assert cmd.query is False

    def test_no_args(self):
        cmd = SCPICommand("*RST", "*RST")
        assert cmd.raw == "*RST"
        assert cmd.prefix == "*RST"
        assert cmd.args == []
        assert cmd.query is False

    def test_query_detection(self):
        cmd = SCPICommand("*IDN?", "*IDN?")
        assert cmd.query is True

    def test_query_with_trailing_space(self):
        cmd = SCPICommand("*IDN? ", "*IDN?")
        assert cmd.query is True

    def test_non_query(self):
        cmd = SCPICommand("*ESE 32", "*ESE")
        assert cmd.query is False

    def test_comma_separated_args(self):
        cmd = SCPICommand("CONF:VOLT 10,0.001", "CONF:VOLT")
        assert cmd.args == ["10", "0.001"]

    def test_single_arg(self):
        cmd = SCPICommand("*ESE 32", "*ESE")
        assert cmd.args == ["32"]

    def test_args_whitespace_stripped(self):
        cmd = SCPICommand("CONF:VOLT 10 , 0.001 , AUTO", "CONF:VOLT")
        assert cmd.args == ["10", "0.001", "AUTO"]

    def test_str_returns_raw(self):
        cmd = SCPICommand("MEAS:VOLT?", "MEAS:")
        assert str(cmd) == "MEAS:VOLT?"


class TestSCPICommandArgCoercion:
    """Type coercion via cmd.arg()."""

    def test_arg_as_float(self):
        cmd = SCPICommand("CONF:VOLT:DC 10,0.001", "CONF:VOLT:DC")
        assert cmd.arg(0, float) == 10.0
        assert cmd.arg(1, float) == 0.001

    def test_arg_as_int(self):
        cmd = SCPICommand("*ESE 32", "*ESE")
        assert cmd.arg(0, int) == 32

    def test_arg_default_type_is_str(self):
        cmd = SCPICommand("CONF:VOLT:DC 10", "CONF:VOLT:DC")
        assert cmd.arg(0) == "10"
        assert isinstance(cmd.arg(0), str)

    def test_arg_out_of_range_returns_default(self):
        cmd = SCPICommand("*RST", "*RST")
        assert cmd.arg(0) is None
        assert cmd.arg(0, float, 42.0) == 42.0

    def test_arg_invalid_type_returns_default(self):
        cmd = SCPICommand("CONF:VOLT:DC AUTO", "CONF:VOLT:DC")
        assert cmd.arg(0, float) is None
        assert cmd.arg(0, float, -1.0) == -1.0

    def test_arg_with_custom_default(self):
        cmd = SCPICommand("CONF:VOLT:DC 10", "CONF:VOLT:DC")
        assert cmd.arg(1, int, 100) == 100
