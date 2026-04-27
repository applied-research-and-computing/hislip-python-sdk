"""Minimal HiSLIP instrument in ~15 lines."""

import signal, threading
from hislip_instruments import InstrumentBase, HiSLIPProtocol

class HelloInstrument(InstrumentBase):
    def setup_commands(self):
        self.engine.register_handler("HELLO?", lambda _: "World!")

instrument = HelloInstrument(
    manufacturer="DEMO", model="HELLO", protocols=[HiSLIPProtocol(port=4880)]
)
instrument.start()
print(f"Running: {instrument.resource_strings[0]}")
print("Send '*IDN?' or 'HELLO?' via any VISA client. Ctrl-C to stop.")

stop = threading.Event()
signal.signal(signal.SIGINT, lambda *_: stop.set())
stop.wait()
instrument.stop()
