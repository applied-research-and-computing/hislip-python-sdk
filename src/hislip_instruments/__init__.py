"""HiSLIP instrument framework.

Build simulated test instruments that speak HiSLIP (IVI-6.1) and can
be wired together through signal ports to simulate complete test setups.

Quick start:
    from hislip_instruments import InstrumentBase, HiSLIPProtocol, Signal

    class VirtualDMM(InstrumentBase):
        def __init__(self, **kwargs):
            super().__init__(manufacturer="KEYSIGHT", model="34461A", **kwargs)
            self.add_input("INPUT")

        def on_input_changed(self, port_name, signal):
            if port_name == "INPUT":
                self.engine.set_property("VOLT", f"{signal.value:.5E}")

    dmm = VirtualDMM(protocols=[HiSLIPProtocol()])
    dmm.start()
"""

from .engine import CommandEngine
from .instrument import InstrumentBase
from .ports import InputPort, OutputPort, Signal
from .protocol import HiSLIPProtocol, Protocol

__all__ = [
    "CommandEngine",
    "HiSLIPProtocol",
    "InstrumentBase",
    "InputPort",
    "OutputPort",
    "Protocol",
    "Signal",
]
