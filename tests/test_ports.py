"""Tests for signal ports and inter-instrument wiring."""

import pytest
from hislip_instruments.ports import Signal, OutputPort, InputPort
from hislip_instruments.instrument import InstrumentBase


class TestSignal:
    """Signal dataclass behavior."""

    def test_default_signal(self):
        signal = Signal()
        assert signal.value == 0.0
        assert signal.unit == "V"
        assert signal.metadata == {}

    def test_signal_with_values(self):
        signal = Signal(value=3.3, unit="V", metadata={"frequency": 1e6})
        assert signal.value == 3.3
        assert signal.unit == "V"
        assert signal.metadata["frequency"] == 1e6

    def test_signal_metadata_independent(self):
        """Each Signal instance should have its own metadata dict."""
        sig1 = Signal()
        sig2 = Signal()
        sig1.metadata["key"] = "value"
        assert "key" not in sig2.metadata


class _TrackingInstrument(InstrumentBase):
    """Instrument that records on_input_changed calls for testing."""

    def __init__(self, **kwargs):
        super().__init__(ieee488=False, **kwargs)
        self.received_changes: list[tuple[str, Signal]] = []

    def on_input_changed(self, port_name, signal):
        self.received_changes.append((port_name, signal))


class TestOutputPort:
    """OutputPort emission and connection behavior."""

    def test_initial_signal(self):
        instrument = _TrackingInstrument()
        port = OutputPort("OUT", instrument)
        assert port.signal.value == 0.0

    def test_emit_updates_signal(self):
        instrument = _TrackingInstrument()
        port = OutputPort("OUT", instrument)
        new_signal = Signal(value=5.0, unit="V")
        port.emit(new_signal)
        assert port.signal.value == 5.0

    def test_emit_delivers_to_connected_input(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()
        output = OutputPort("OUT", source)
        input_port = target.add_input("IN")
        output.connect_to(input_port)

        # Clear the initial propagation from connect_to
        target.received_changes.clear()

        output.emit(Signal(value=3.3))
        assert len(target.received_changes) == 1
        assert target.received_changes[0][0] == "IN"
        assert target.received_changes[0][1].value == 3.3

    def test_emit_to_multiple_inputs(self):
        source = _TrackingInstrument()
        target1 = _TrackingInstrument()
        target2 = _TrackingInstrument()

        output = OutputPort("OUT", source)
        in1 = target1.add_input("IN")
        in2 = target2.add_input("IN")

        output.connect_to(in1)
        output.connect_to(in2)
        target1.received_changes.clear()
        target2.received_changes.clear()

        output.emit(Signal(value=7.0))
        assert len(target1.received_changes) == 1
        assert len(target2.received_changes) == 1
        assert target1.received_changes[0][1].value == 7.0
        assert target2.received_changes[0][1].value == 7.0

    def test_connect_pushes_initial_value(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()

        output = OutputPort("OUT", source)
        output.signal = Signal(value=9.0)

        input_port = target.add_input("IN")
        output.connect_to(input_port)

        # Should have received the initial value on connect
        assert len(target.received_changes) == 1
        assert target.received_changes[0][1].value == 9.0

    def test_duplicate_connect_ignored(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()

        output = OutputPort("OUT", source)
        input_port = target.add_input("IN")

        output.connect_to(input_port)
        output.connect_to(input_port)  # duplicate

        target.received_changes.clear()
        output.emit(Signal(value=1.0))

        # Should only receive once, not twice
        assert len(target.received_changes) == 1

    def test_disconnect(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()

        output = OutputPort("OUT", source)
        input_port = target.add_input("IN")
        output.connect_to(input_port)
        output.disconnect(input_port)

        target.received_changes.clear()
        output.emit(Signal(value=1.0))
        assert len(target.received_changes) == 0

    def test_disconnect_all(self):
        source = _TrackingInstrument()
        target1 = _TrackingInstrument()
        target2 = _TrackingInstrument()

        output = OutputPort("OUT", source)
        output.connect_to(target1.add_input("IN"))
        output.connect_to(target2.add_input("IN"))
        output.disconnect_all()

        target1.received_changes.clear()
        target2.received_changes.clear()
        output.emit(Signal(value=1.0))
        assert len(target1.received_changes) == 0
        assert len(target2.received_changes) == 0

    def test_connected_property(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()

        output = OutputPort("OUT", source)
        assert not output.connected

        input_port = target.add_input("IN")
        output.connect_to(input_port)
        assert output.connected

        output.disconnect(input_port)
        assert not output.connected


class TestInputPort:
    """InputPort receive behavior."""

    def test_initial_signal(self):
        instrument = _TrackingInstrument()
        port = InputPort("IN", instrument)
        assert port.signal.value == 0.0

    def test_receive_updates_signal(self):
        instrument = _TrackingInstrument()
        port = InputPort("IN", instrument)
        port.receive(Signal(value=2.5, unit="A"))
        assert port.signal.value == 2.5
        assert port.signal.unit == "A"

    def test_receive_triggers_callback(self):
        instrument = _TrackingInstrument()
        port = InputPort("IN", instrument)
        port.receive(Signal(value=1.0))
        assert len(instrument.received_changes) == 1
        assert instrument.received_changes[0] == ("IN", Signal(value=1.0))


class TestSignalChaining:
    """End-to-end signal flow through connected instruments."""

    def test_signal_chain_three_instruments(self):
        """Source -> Middle -> Sink signal propagation."""

        class Passthrough(InstrumentBase):
            def __init__(self, **kwargs):
                super().__init__(ieee488=False, **kwargs)
                self.add_input("IN")
                self.add_output("OUT")

            def on_input_changed(self, port_name, signal):
                if port_name == "IN":
                    # Double the value and pass it along
                    doubled = Signal(value=signal.value * 2, unit=signal.unit)
                    self.outputs["OUT"].emit(doubled)

        class Sink(_TrackingInstrument):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_input("IN")

        source = InstrumentBase(ieee488=False)
        source.add_output("OUT")

        middle = Passthrough()
        sink = Sink()

        source.connect("OUT", middle, "IN")
        middle.connect("OUT", sink, "IN")

        # Emit from source
        source.outputs["OUT"].emit(Signal(value=5.0))

        # Sink should receive doubled value
        # The last change should be 10.0 (5.0 * 2)
        last_change = sink.received_changes[-1]
        assert last_change[1].value == 10.0

    def test_signal_with_metadata_propagates(self):
        source = _TrackingInstrument()
        target = _TrackingInstrument()

        source.add_output("RF_OUT")
        target.add_input("RF_IN")
        source.connect("RF_OUT", target, "RF_IN")
        target.received_changes.clear()

        signal = Signal(
            value=1.0,
            unit="Vpp",
            metadata={"frequency": 1e9, "waveform": "sine", "offset": 0.5},
        )
        source.outputs["RF_OUT"].emit(signal)

        received = target.received_changes[0][1]
        assert received.value == 1.0
        assert received.metadata["frequency"] == 1e9
        assert received.metadata["waveform"] == "sine"
        assert received.metadata["offset"] == 0.5
