"""Start a virtual oscilloscope + signal generator wired together.

The signal generator OUTPUT is connected to the oscilloscope CH1 input.
The SG starts with output enabled at 1 kHz / 2 Vpp sine, so the
oscilloscope immediately has a live waveform on CH1.

Ports:
    Oscilloscope  HiSLIP  4880   TCPIP0::127.0.0.1::hislip0::INSTR
    Signal Gen    HiSLIP  4881   TCPIP0::127.0.0.1::hislip0::INSTR

Usage:
    python3 bench.py
"""

import signal
import threading

from hislip_instruments import HiSLIPProtocol

from oscilloscope import VirtualOscilloscope
from signal_generator import VirtualSignalGenerator

stop_event = threading.Event()


def main():
    osc = VirtualOscilloscope(
        protocols=[HiSLIPProtocol(port=4880)]
    )
    sg = VirtualSignalGenerator(
        protocols=[HiSLIPProtocol(port=4881)]
    )

    # Wire signal generator OUTPUT -> oscilloscope CH1
    sg.connect("OUTPUT", osc, "CH1")

    osc.start()
    sg.start()

    # Enable output so CH1 has a live waveform immediately
    sg.engine.process_command("SOUR:FREQ 1000")
    sg.engine.process_command("SOUR:VOLT 2")
    sg.engine.process_command("OUTP ON")

    print("Virtual bench running:")
    print(f"  Oscilloscope HiSLIP:  TCPIP0::127.0.0.1::hislip0::INSTR  (port 4880)")
    print(f"  Signal Gen   HiSLIP:  TCPIP0::127.0.0.1::hislip0::INSTR  (port 4881)")
    print(f"  Wiring:               SG OUTPUT -> OSC CH1  (1 kHz, 2 Vpp sine)")
    print()
    print("Press Ctrl-C to stop.")

    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    stop_event.wait()

    sg.stop()
    osc.stop()
    print("\nStopped.")


if __name__ == "__main__":
    main()
