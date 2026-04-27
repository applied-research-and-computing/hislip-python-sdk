"""Inter-instrument signal routing.

Virtual instruments connect to each other through ports. An OutputPort
on one instrument can be wired to an InputPort on another, so when
the source instrument emits a signal, the receiving instrument sees it
and can react (e.g., update a measurement value).

Signals are flexible — a simple voltage, a complex RF signal with
frequency/power/modulation metadata, or a bare trigger pulse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .instrument import InstrumentBase


@dataclass
class Signal:
    """A value that flows between instrument ports.

    Simple use: just set `value` and `unit`.
    Complex use: put additional data in `metadata` (frequency,
    waveform shape, impedance, modulation, etc.).
    """

    value: float = 0.0
    unit: str = "V"
    metadata: dict[str, Any] = field(default_factory=dict)


class OutputPort:
    """An output on a virtual instrument that can drive other instruments."""

    def __init__(self, name: str, instrument: InstrumentBase):
        self.name = name
        self.instrument = instrument
        self._connections: list[InputPort] = []
        self.signal = Signal()

    def emit(self, signal: Signal):
        """Push a new signal value to all connected inputs."""
        self.signal = signal
        for port in self._connections:
            port.receive(signal)

    def connect_to(self, input_port: InputPort):
        """Wire this output to an input port on another instrument."""
        if input_port not in self._connections:
            self._connections.append(input_port)
        # Immediately push current value so the receiver has initial state
        input_port.receive(self.signal)

    def disconnect(self, input_port: InputPort):
        """Remove a connection to an input port."""
        self._connections = [
            conn for conn in self._connections if conn is not input_port
        ]

    def disconnect_all(self):
        """Remove all connections from this output."""
        self._connections.clear()

    @property
    def connected(self) -> bool:
        return len(self._connections) > 0


class InputPort:
    """An input on a virtual instrument that receives signals from other instruments."""

    def __init__(self, name: str, instrument: InstrumentBase):
        self.name = name
        self.instrument = instrument
        self.signal = Signal()

    def receive(self, signal: Signal):
        """Called when a connected output emits a new signal."""
        self.signal = signal
        self.instrument.on_input_changed(self.name, signal)
