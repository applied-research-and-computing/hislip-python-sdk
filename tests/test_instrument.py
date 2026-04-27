"""Tests for InstrumentBase — the main class users extend."""

import pytest
from hislip_instruments.instrument import InstrumentBase
from hislip_instruments.ports import Signal
from hislip_instruments.protocol import Protocol


class _DummyProtocol(Protocol):
    """Minimal protocol for testing InstrumentBase lifecycle."""

    def __init__(self):
        super().__init__()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def resource_string(self) -> str:
        return "DUMMY::resource"


class TestConstruction:
    """InstrumentBase construction and engine setup."""

    def test_default_construction(self):
        instrument = InstrumentBase()
        assert instrument.engine is not None
        result = instrument.engine.process_command("*IDN?")
        assert "MOCK,Instrument,SN001,1.0.0" == result

    def test_custom_identity(self):
        instrument = InstrumentBase(
            manufacturer="KEYSIGHT",
            model="34461A",
            serial="MY12345",
            firmware="3.0",
        )
        result = instrument.engine.process_command("*IDN?")
        assert result == "KEYSIGHT,34461A,MY12345,3.0"

    def test_ieee488_disabled(self):
        instrument = InstrumentBase(ieee488=False)
        assert instrument.engine.process_command("*IDN?") is None

    def test_custom_separator(self):
        instrument = InstrumentBase(command_separator="|")
        result = instrument.engine.process_command("*IDN?|*OPC?")
        assert "MOCK" in result
        assert "1" in result

    def test_protocols_attached_on_construction(self):
        proto = _DummyProtocol()
        instrument = InstrumentBase(protocols=[proto])
        assert proto in instrument.protocols
        assert proto._engine is instrument.engine

    def test_empty_ports(self):
        instrument = InstrumentBase()
        assert instrument.inputs == {}
        assert instrument.outputs == {}


class TestProtocolManagement:
    """Adding, starting, and stopping protocols."""

    def test_add_protocol(self):
        instrument = InstrumentBase()
        proto = _DummyProtocol()
        instrument.add_protocol(proto)
        assert proto in instrument.protocols

    def test_add_protocol_attaches_engine(self):
        instrument = InstrumentBase()
        proto = _DummyProtocol()
        instrument.add_protocol(proto)
        assert proto._engine is instrument.engine

    def test_start_starts_all_protocols(self):
        proto1 = _DummyProtocol()
        proto2 = _DummyProtocol()
        instrument = InstrumentBase(protocols=[proto1, proto2])
        instrument.start()
        assert proto1.started
        assert proto2.started

    def test_stop_stops_all_protocols(self):
        proto1 = _DummyProtocol()
        proto2 = _DummyProtocol()
        instrument = InstrumentBase(protocols=[proto1, proto2])
        instrument.start()
        instrument.stop()
        assert proto1.stopped
        assert proto2.stopped

    def test_resource_strings(self):
        proto1 = _DummyProtocol()
        proto2 = _DummyProtocol()
        instrument = InstrumentBase(protocols=[proto1, proto2])
        assert instrument.resource_strings == ["DUMMY::resource", "DUMMY::resource"]

    def test_protocols_returns_copy(self):
        instrument = InstrumentBase(protocols=[_DummyProtocol()])
        protos = instrument.protocols
        protos.append(_DummyProtocol())
        assert len(instrument.protocols) == 1  # original unchanged


class TestPortManagement:
    """Adding and connecting ports."""

    def test_add_input(self):
        instrument = InstrumentBase()
        port = instrument.add_input("SENSOR")
        assert "SENSOR" in instrument.inputs
        assert instrument.inputs["SENSOR"] is port

    def test_add_output(self):
        instrument = InstrumentBase()
        port = instrument.add_output("DRIVE")
        assert "DRIVE" in instrument.outputs
        assert instrument.outputs["DRIVE"] is port

    def test_connect_instruments(self):
        source = InstrumentBase()
        target = InstrumentBase()
        source.add_output("OUT")
        target.add_input("IN")

        source.connect("OUT", target, "IN")
        assert source.outputs["OUT"].connected

    def test_connect_bad_output_name(self):
        source = InstrumentBase()
        target = InstrumentBase()
        target.add_input("IN")

        with pytest.raises(KeyError, match="no output port"):
            source.connect("NOEXIST", target, "IN")

    def test_connect_bad_input_name(self):
        source = InstrumentBase()
        target = InstrumentBase()
        source.add_output("OUT")

        with pytest.raises(KeyError, match="no input port"):
            source.connect("OUT", target, "NOEXIST")


class TestSubclassing:
    """Extending InstrumentBase with custom instruments."""

    def test_setup_commands_called_on_start(self):
        class CustomInstrument(InstrumentBase):
            def __init__(self):
                super().__init__()
                self.setup_called = False

            def setup_commands(self):
                self.setup_called = True
                self.engine.register_handler("CUSTOM?", lambda _: "42")

        instrument = CustomInstrument()
        instrument.start()
        assert instrument.setup_called
        assert instrument.engine.process_command("CUSTOM?") == "42"

    def test_on_input_changed_receives_signal(self):
        class Receiver(InstrumentBase):
            def __init__(self):
                super().__init__(ieee488=False)
                self.add_input("IN")
                self.last_signal = None

            def on_input_changed(self, port_name, signal):
                self.last_signal = signal

        source = InstrumentBase(ieee488=False)
        source.add_output("OUT")
        receiver = Receiver()
        source.connect("OUT", receiver, "IN")

        source.outputs["OUT"].emit(Signal(value=42.0))
        assert receiver.last_signal is not None
        assert receiver.last_signal.value == 42.0

    def test_input_drives_measurement(self):
        """Instrument updates its measurement based on input signal."""

        class VirtualDMM(InstrumentBase):
            def __init__(self):
                super().__init__(
                    manufacturer="TEST", model="DMM", ieee488=True
                )
                self.add_input("INPUT")

            def setup_commands(self):
                self.engine.register_handler(
                    "MEAS:VOLT?",
                    lambda _: self.engine.get_property("VOLT"),
                )

            def on_input_changed(self, port_name, signal):
                if port_name == "INPUT":
                    self.engine.set_property("VOLT", f"{signal.value:.5E}")

        class VirtualPSU(InstrumentBase):
            def __init__(self):
                super().__init__(
                    manufacturer="TEST", model="PSU", ieee488=False
                )
                self.add_output("CH1")

        psu = VirtualPSU()
        dmm = VirtualDMM()
        psu.connect("CH1", dmm, "INPUT")

        dmm.start()
        psu.outputs["CH1"].emit(Signal(value=3.3))

        result = dmm.engine.process_command("MEAS:VOLT?")
        assert result == "3.30000E+00"

    def test_non_scpi_instrument(self):
        """Instrument with proprietary command set (no IEEE 488.2)."""

        class WeirdLaser(InstrumentBase):
            def __init__(self):
                super().__init__(ieee488=False, command_separator="\r")
                self._power = 0.0

            def setup_commands(self):
                self.engine.register_handler("PWR?", self._query_power)
                self.engine.register_handler("PWR", self._set_power)
                self.engine.register_handler("STAT", lambda _: "OK")

            def _query_power(self, _cmd):
                return f"{self._power:.1f}W"

            def _set_power(self, cmd):
                parts = cmd.split()
                if len(parts) == 2:
                    self._power = float(parts[1])
                return None

        laser = WeirdLaser()
        laser.start()

        assert laser.engine.process_command("STAT") == "OK"
        laser.engine.process_command("PWR 50.5")
        assert laser.engine.process_command("PWR?") == "50.5W"

    def test_multi_protocol_instrument(self):
        """Same instrument reachable via multiple protocols."""
        proto1 = _DummyProtocol()
        proto2 = _DummyProtocol()

        instrument = InstrumentBase(
            manufacturer="MULTI", model="PROTO", protocols=[proto1, proto2]
        )
        instrument.start()

        # Both protocols share the same engine
        assert proto1._engine is proto2._engine
        assert proto1.started
        assert proto2.started
        assert len(instrument.resource_strings) == 2
