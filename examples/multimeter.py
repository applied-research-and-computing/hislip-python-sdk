"""Virtual digital multimeter (DMM).

Simulates a generic 6.5-digit DMM with standard SCPI measurement
commands. Supports voltage, current, resistance, and frequency
measurements with configurable ranges.

When an INPUT port is connected, measurement values track the
incoming signal. Otherwise, returns default/configured values.
"""

from __future__ import annotations

import random

from hislip_instruments import InstrumentBase, Signal


class VirtualDMM(InstrumentBase):
    """Virtual digital multimeter.

    SCPI command tree:
        CONFigure:VOLTage[:DC] [<range>]
        CONFigure:CURRent[:DC] [<range>]
        CONFigure:RESistance [<range>]
        CONFigure:FREQuency [<range>]
        MEASure:VOLTage[:DC]?
        MEASure:CURRent[:DC]?
        MEASure:RESistance?
        MEASure:FREQuency?
        READ?
        INPut:IMPedance:AUTO <bool>
        SENSe:VOLTage:DC:NPLC <value>
        SENSe:VOLTage:DC:RANGe <value>
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("manufacturer", "CARBON")
        kwargs.setdefault("model", "DMM6500")
        kwargs.setdefault("serial", "DMM001")
        kwargs.setdefault("firmware", "1.0.0")
        super().__init__(**kwargs)

        self._function = "VOLT"
        self._range = "AUTO"
        self._nplc = 10.0
        self._impedance_auto = True

        self.add_input("INPUT")
        self.add_input("TRIGGER_IN")
        self.add_output("TRIGGER_OUT")

    def _reset(self):
        """Reset instrument state to power-on defaults."""
        self._function = "VOLT"
        self._range = "AUTO"
        self._nplc = 10.0
        self._impedance_auto = True

    def setup_commands(self):
        engine = self.engine
        engine._reset_hooks.append(self._reset)

        # Configure function
        engine.register_handler("CONF:VOLT", self._configure_voltage)
        engine.register_handler("CONFIGURE:VOLTAGE", self._configure_voltage)
        engine.register_handler("CONF:CURR", self._configure_current)
        engine.register_handler("CONFIGURE:CURRENT", self._configure_current)
        engine.register_handler("CONF:RES", self._configure_resistance)
        engine.register_handler("CONFIGURE:RESISTANCE", self._configure_resistance)
        engine.register_handler("CONF:FREQ", self._configure_frequency)
        engine.register_handler("CONFIGURE:FREQUENCY", self._configure_frequency)

        # Read measurement
        engine.register_handler("READ?", self._handle_read)

        # Input impedance
        engine.register_handler("INP:IMP:AUTO", self._handle_impedance_auto)
        engine.register_handler("INPUT:IMPEDANCE:AUTO", self._handle_impedance_auto)

        # Sense subsystem
        engine.register_handler("SENS:VOLT:DC:NPLC", self._handle_nplc)
        engine.register_handler("SENSE:VOLTAGE:DC:NPLC", self._handle_nplc)
        engine.register_handler("SENS:VOLT:DC:RANG", self._handle_range)
        engine.register_handler("SENSE:VOLTAGE:DC:RANGE", self._handle_range)

        # Function query
        engine.register_handler("FUNC?", self._handle_func_query)
        engine.register_handler("FUNCTION?", self._handle_func_query)

    def on_input_changed(self, port_name: str, signal: Signal):
        if port_name == "INPUT":
            if self._function == "VOLT" and signal.unit in ("V", "Vpp"):
                self.engine.set_property("VOLT", f"{signal.value:.6E}")
            elif self._function == "CURR" and signal.unit == "A":
                self.engine.set_property("CURR", f"{signal.value:.6E}")
            elif self._function == "RES" and signal.unit in ("\u03a9", "ohm", "OHM"):
                self.engine.set_property("RES", f"{signal.value:.6E}")
            elif self._function == "FREQ":
                freq = signal.metadata.get("frequency", signal.value)
                self.engine.set_property("FREQ", f"{freq:.6E}")

    def _reading(self) -> str:
        """Get current measurement value with realistic noise."""
        prop = self.engine.get_property(self._function)
        if prop is None:
            return "9.90000E+37"
        try:
            value = float(prop)
            noise = random.gauss(0, abs(value) * 1e-6) if value != 0 else 0
            return f"{value + noise:.6E}"
        except ValueError:
            return prop

    def _handle_read(self, _cmd) -> str:
        return self._reading()

    def _configure_voltage(self, cmd) -> str | None:
        if cmd.query:
            return f"VOLT {self._range}"
        self._function = "VOLT"
        rng = cmd.arg(0)
        if rng is not None:
            self._range = rng.upper()
        return None

    def _configure_current(self, cmd) -> str | None:
        if cmd.query:
            return f"CURR {self._range}"
        self._function = "CURR"
        rng = cmd.arg(0)
        if rng is not None:
            self._range = rng.upper()
        return None

    def _configure_resistance(self, cmd) -> str | None:
        if cmd.query:
            return f"RES {self._range}"
        self._function = "RES"
        rng = cmd.arg(0)
        if rng is not None:
            self._range = rng.upper()
        return None

    def _configure_frequency(self, cmd) -> str | None:
        if cmd.query:
            return f"FREQ {self._range}"
        self._function = "FREQ"
        rng = cmd.arg(0)
        if rng is not None:
            self._range = rng.upper()
        return None

    def _handle_impedance_auto(self, cmd) -> str | None:
        if cmd.query:
            return "1" if self._impedance_auto else "0"
        value = cmd.arg(0, str, "")
        self._impedance_auto = value.upper() in ("ON", "1")
        return None

    def _handle_nplc(self, cmd) -> str | None:
        if cmd.query:
            return str(self._nplc)
        val = cmd.arg(0, float)
        if val is not None:
            self._nplc = val
        return None

    def _handle_range(self, cmd) -> str | None:
        if cmd.query:
            return self._range
        rng = cmd.arg(0)
        if rng is not None:
            self._range = rng.upper()
        return None

    def _handle_func_query(self, _cmd) -> str:
        return self._function


if __name__ == "__main__":
    from hislip_instruments import HiSLIPProtocol

    dmm = VirtualDMM(protocols=[HiSLIPProtocol(port=4880)])
    dmm.start()
    print(f"DMM running: {dmm.resource_strings[0]}")
    print("Press Ctrl-C to stop.")

    import signal, threading
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    dmm.stop()
