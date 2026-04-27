"""Tests for CommandEngine — the core command processor."""

import pytest
from hislip_instruments.engine import CommandEngine


class TestBasicProcessing:
    """Basic command dispatch and response handling."""

    def test_empty_command_returns_none(self):
        engine = CommandEngine()
        assert engine.process_command("") is None
        assert engine.process_command("   ") is None

    def test_single_query_returns_response(self):
        engine = CommandEngine()
        result = engine.process_command("*IDN?")
        assert result is not None
        assert "MOCK" in result

    def test_non_query_returns_none(self):
        engine = CommandEngine()
        assert engine.process_command("*RST") is None

    def test_semicolon_separated_commands(self):
        engine = CommandEngine()
        result = engine.process_command("*IDN?;*OPC?")
        parts = result.split(";")
        assert len(parts) == 2
        assert "MOCK" in parts[0]
        assert parts[1] == "1"

    def test_semicolon_mix_query_and_write(self):
        engine = CommandEngine()
        result = engine.process_command("*RST;*IDN?")
        assert result is not None
        assert "MOCK" in result

    def test_all_writes_returns_none(self):
        engine = CommandEngine()
        assert engine.process_command("*RST;*CLS") is None

    def test_custom_separator(self):
        engine = CommandEngine(ieee488=False, command_separator="|")
        engine.register_handler("A?", lambda _: "hello")
        engine.register_handler("B?", lambda _: "world")
        result = engine.process_command("A?|B?")
        assert result == "hello;world"

    def test_no_separator(self):
        engine = CommandEngine(ieee488=False, command_separator="")
        engine.register_handler("HELLO?", lambda _: "yes")
        result = engine.process_command("HELLO?")
        assert result == "yes"


class TestIEEE488Commands:
    """IEEE 488.2 common command handling."""

    def test_idn_query(self):
        engine = CommandEngine(
            manufacturer="ACME",
            model="Widget",
            serial="X123",
            firmware="2.0",
        )
        result = engine.process_command("*IDN?")
        assert result == "ACME,Widget,X123,2.0"

    def test_rst_resets_state(self):
        engine = CommandEngine()
        engine.set_property("VOLT", "99.9")
        engine.process_command("*RST")
        # After reset, defaults are restored
        assert engine.get_property("VOLT") == "1.00000E+00"

    def test_cls_clears_status(self):
        engine = CommandEngine()
        engine.generate_srq()
        stb_before = engine.read_stb()
        assert stb_before & 0x40  # SRQ was set

        engine.process_command("*CLS")
        # After CLS, reading STB should show cleared state
        result = engine.process_command("*STB?")
        assert result == "0"

    def test_stb_query(self):
        engine = CommandEngine()
        result = engine.process_command("*STB?")
        assert result == "0"

    def test_esr_query_clears_on_read(self):
        engine = CommandEngine()
        engine._esr = 42
        result = engine.process_command("*ESR?")
        assert result == "42"
        # Second read should be 0 (cleared)
        assert engine.process_command("*ESR?") == "0"

    def test_ese_set_and_query(self):
        engine = CommandEngine()
        engine.process_command("*ESE 32")
        assert engine.process_command("*ESE?") == "32"

    def test_ese_masks_to_byte(self):
        engine = CommandEngine()
        engine.process_command("*ESE 256")
        assert engine.process_command("*ESE?") == "0"

    def test_sre_set_and_query(self):
        engine = CommandEngine()
        engine.process_command("*SRE 16")
        assert engine.process_command("*SRE?") == "16"

    def test_opc_query_returns_one(self):
        engine = CommandEngine()
        assert engine.process_command("*OPC?") == "1"

    def test_opc_set(self):
        engine = CommandEngine()
        engine.process_command("*OPC")
        assert engine._opc is True

    def test_tst_query_returns_zero(self):
        engine = CommandEngine()
        assert engine.process_command("*TST?") == "0"


class TestSCPICommands:
    """SCPI system commands (only when ieee488=True)."""

    def test_system_error_query(self):
        engine = CommandEngine()
        assert engine.process_command("SYST:ERR?") == '0,"No error"'
        assert engine.process_command("SYSTEM:ERROR?") == '0,"No error"'

    def test_system_version_query(self):
        engine = CommandEngine()
        assert engine.process_command("SYST:VERS?") == "1999.0"
        assert engine.process_command("SYSTEM:VERSION?") == "1999.0"

    def test_measure_voltage(self):
        engine = CommandEngine()
        result = engine.process_command("MEAS:VOLT?")
        assert result == "1.00000E+00"

    def test_measure_current(self):
        engine = CommandEngine()
        result = engine.process_command("MEAS:CURR?")
        assert result == "5.00000E-03"

    def test_measure_long_form(self):
        engine = CommandEngine()
        result = engine.process_command("MEASURE:FREQ?")
        assert result == "1.00000E+03"

    def test_measure_unknown_returns_nan(self):
        engine = CommandEngine()
        result = engine.process_command("MEAS:POWER?")
        assert result == "9.90000E+37"

    def test_default_properties(self):
        engine = CommandEngine()
        assert engine.get_property("VOLT") == "1.00000E+00"
        assert engine.get_property("CURR") == "5.00000E-03"
        assert engine.get_property("FREQ") == "1.00000E+03"
        assert engine.get_property("RES") == "1.00000E+04"


class TestIEEE488Disabled:
    """Behavior when ieee488=False."""

    def test_no_ieee488_commands(self):
        engine = CommandEngine(ieee488=False)
        result = engine.process_command("*IDN?")
        # Should not be handled — falls through to property handler
        # which returns None for unknown property
        assert result is None

    def test_no_scpi_commands(self):
        engine = CommandEngine(ieee488=False)
        result = engine.process_command("SYST:ERR?")
        assert result is None

    def test_custom_handlers_still_work(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("STATUS", lambda _: "OK")
        assert engine.process_command("STATUS") == "OK"


class TestCustomHandlers:
    """Custom command handler registration and dispatch."""

    def test_register_and_call(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("HELLO", lambda cmd: "WORLD")
        assert engine.process_command("HELLO") == "WORLD"

    def test_handler_receives_full_command(self):
        engine = CommandEngine(ieee488=False)
        received = []
        engine.register_handler("SET", lambda cmd: (received.append(cmd), None)[1])
        engine.process_command("SET VALUE 42")
        assert received[0] == "SET VALUE 42"

    def test_prefix_matching(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("MEAS", lambda _: "42")
        assert engine.process_command("MEAS:VOLT?") == "42"
        assert engine.process_command("MEASURE") == "42"

    def test_case_insensitive_matching(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("HELLO", lambda _: "yes")
        assert engine.process_command("hello") == "yes"
        assert engine.process_command("Hello") == "yes"
        assert engine.process_command("HELLO") == "yes"

    def test_longest_prefix_wins(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("AB", lambda _: "short")
        engine.register_handler("ABC", lambda _: "long")
        # Longer prefix "ABC" beats shorter "AB" for command "ABCD"
        assert engine.process_command("ABCD") == "long"
        # But "ABX" only matches "AB"
        assert engine.process_command("ABX") == "short"

    def test_handler_returning_none(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("SET", lambda _: None)
        assert engine.process_command("SET X 5") is None

    def test_multiple_handlers(self):
        engine = CommandEngine(ieee488=False)
        engine.register_handler("READ", lambda _: "100")
        engine.register_handler("WRITE", lambda _: None)
        engine.register_handler("STATUS", lambda _: "OK")
        assert engine.process_command("READ") == "100"
        assert engine.process_command("WRITE DATA") is None
        assert engine.process_command("STATUS") == "OK"


class TestProperties:
    """Property get/set via command processing and direct API."""

    def test_set_and_query_property(self):
        engine = CommandEngine(ieee488=False)
        engine.process_command("POWER 50")
        result = engine.process_command("POWER?")
        assert result == "50"

    def test_case_insensitive_property(self):
        engine = CommandEngine(ieee488=False)
        engine.process_command("voltage 3.14")
        assert engine.process_command("VOLTAGE?") == "3.14"

    def test_unknown_property_query_returns_none(self):
        engine = CommandEngine(ieee488=False)
        assert engine.process_command("NOEXIST?") is None

    def test_direct_get_set(self):
        engine = CommandEngine()
        engine.set_property("TEMP", "25.0")
        assert engine.get_property("TEMP") == "25.0"
        assert engine.get_property("temp") == "25.0"

    def test_direct_get_missing(self):
        engine = CommandEngine()
        assert engine.get_property("NOEXIST") is None

    def test_property_with_colons(self):
        engine = CommandEngine(ieee488=False)
        engine.set_property("SOUR:VOLT", "5.0")
        # Query with colon
        assert engine.get_property("SOUR:VOLT") == "5.0"


class TestStatusRegisters:
    """Status byte, SRQ, and register operations."""

    def test_initial_stb_is_zero(self):
        engine = CommandEngine()
        assert engine.read_stb() == 0

    def test_generate_srq_sets_bit6(self):
        engine = CommandEngine()
        engine.generate_srq()
        stb = engine.read_stb()
        assert stb & 0x40

    def test_read_stb_clears_srq(self):
        engine = CommandEngine()
        engine.generate_srq()
        engine.read_stb()
        stb = engine.read_stb()
        assert not (stb & 0x40)

    def test_cls_clears_srq(self):
        engine = CommandEngine()
        engine.generate_srq()
        engine.process_command("*CLS")
        assert engine.read_stb() == 0

    def test_rst_clears_device_state_but_preserves_enable_registers(self):
        """Per IEEE 488.2 section 10.32, *RST must NOT clear ESE or SRE."""
        engine = CommandEngine()
        engine.generate_srq()
        engine.process_command("*ESE 255")
        engine.process_command("*SRE 255")
        engine.process_command("*RST")
        # Enable registers survive *RST
        assert engine.process_command("*ESE?") == "255"
        assert engine.process_command("*SRE?") == "255"
        # But STB and SRQ are cleared
        assert engine.read_stb() == 0
