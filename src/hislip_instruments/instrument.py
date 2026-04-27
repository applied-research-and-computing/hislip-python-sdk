"""Base class for virtual instruments.

InstrumentBase is the main class you extend to build virtual test
instruments. It composes a CommandEngine for command processing,
protocols for transport, and ports for inter-instrument wiring.

Usage:
    from hislip_instruments import InstrumentBase, HiSLIPProtocol

    class VirtualDMM(InstrumentBase):
        def __init__(self, **kwargs):
            super().__init__(
                manufacturer="KEYSIGHT",
                model="34461A",
                serial="MY12345678",
                firmware="2.0.0",
                **kwargs,
            )
            self.add_input("INPUT")
            self.add_input("TRIGGER_IN")
            self.add_output("TRIGGER_OUT")

        def setup_commands(self):
            self.engine.register_handler("CONF:VOLT", self.configure_voltage)

        def on_input_changed(self, port_name, signal):
            if port_name == "INPUT":
                self.engine.set_property("VOLT", f"{signal.value:.5E}")

    dmm = VirtualDMM(protocols=[HiSLIPProtocol(port=4880)])
    dmm.start()
"""

from __future__ import annotations

from .engine import CommandEngine
from .ports import InputPort, OutputPort, Signal
from .protocol import Protocol


class InstrumentBase:
    """Base class for all virtual instruments.

    Extend this to build specific instrument simulations. Override
    setup_commands() to register command handlers, and on_input_changed()
    to react when a connected instrument pushes a signal to one of
    your input ports.
    """

    def __init__(
        self,
        *,
        manufacturer: str = "MOCK",
        model: str = "Instrument",
        serial: str = "SN001",
        firmware: str = "1.0.0",
        ieee488: bool = True,
        command_separator: str = ";",
        protocols: list[Protocol] | None = None,
    ):
        self.engine = CommandEngine(
            manufacturer=manufacturer,
            model=model,
            serial=serial,
            firmware=firmware,
            ieee488=ieee488,
            command_separator=command_separator,
        )

        self.inputs: dict[str, InputPort] = {}
        self.outputs: dict[str, OutputPort] = {}
        self._protocols: list[Protocol] = []
        self._started = False

        if protocols:
            for protocol in protocols:
                self.add_protocol(protocol)

    # -- Protocol management -------------------------------------------------

    def add_protocol(self, protocol: Protocol):
        """Attach a protocol transport to this instrument."""
        protocol.attach(self.engine)
        self._protocols.append(protocol)

    @property
    def protocols(self) -> list[Protocol]:
        return list(self._protocols)

    @property
    def resource_strings(self) -> list[str]:
        """VISA resource strings for all attached protocols."""
        return [proto.resource_string for proto in self._protocols]

    # -- Port management -----------------------------------------------------

    def add_input(self, name: str) -> InputPort:
        """Add a named input port to this instrument."""
        port = InputPort(name, self)
        self.inputs[name] = port
        return port

    def add_output(self, name: str) -> OutputPort:
        """Add a named output port to this instrument."""
        port = OutputPort(name, self)
        self.outputs[name] = port
        return port

    def connect(self, output_name: str, target: InstrumentBase, input_name: str):
        """Wire an output port on this instrument to an input on another.

        After connecting, signals emitted from the output will be
        delivered to the target's input, triggering on_input_changed().
        """
        if output_name not in self.outputs:
            raise KeyError(
                f"{self.__class__.__name__} has no output port '{output_name}'. "
                f"Available: {list(self.outputs.keys())}"
            )
        if input_name not in target.inputs:
            raise KeyError(
                f"{target.__class__.__name__} has no input port '{input_name}'. "
                f"Available: {list(target.inputs.keys())}"
            )
        self.outputs[output_name].connect_to(target.inputs[input_name])

    # -- Lifecycle -----------------------------------------------------------

    def start(self):
        """Start the instrument: register commands, then start all protocols.

        Safe to call multiple times — handlers and hooks are only
        registered on the first call.
        """
        if not self._started:
            self.setup_commands()
            self._started = True
        for protocol in self._protocols:
            protocol.start()

    def stop(self):
        """Stop all protocol servers."""
        for protocol in self._protocols:
            protocol.stop()

    # -- Hooks for subclasses ------------------------------------------------

    def setup_commands(self):
        """Override to register custom command handlers on self.engine.

        Called once during start(), before protocols begin accepting
        connections.
        """
        pass

    def on_input_changed(self, port_name: str, signal: Signal):
        """Override to react when an input port receives a new signal.

        Called whenever a connected output port emits a value. Use this
        to update internal state, properties, or trigger outputs.
        """
        pass
