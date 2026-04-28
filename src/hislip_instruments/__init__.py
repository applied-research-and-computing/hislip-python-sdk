"""Python SDK for building HiSLIP (IVI-6.1) instrument servers.

Build SCPI instrument servers that speak HiSLIP over TCP. Bring your
own command handlers — the SDK handles the wire protocol.

Quick start::

    from hislip_instruments import HiSLIPServer

    server = HiSLIPServer(manufacturer="ACME", model="DMM100", port=4880)

    @server.command("READ?")
    def read_measurement(cmd):
        return str(get_reading())

    server.run()
"""

from .command import SCPICommand
from .engine import CommandEngine
from .instrument import InstrumentBase
from .ports import InputPort, OutputPort, Signal
from .protocol import HiSLIPProtocol, HiSLIPServer, Protocol

__all__ = [
    "CommandEngine",
    "HiSLIPProtocol",
    "HiSLIPServer",
    "InstrumentBase",
    "InputPort",
    "OutputPort",
    "Protocol",
    "SCPICommand",
    "Signal",
]
