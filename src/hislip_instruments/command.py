"""Parsed SCPI command object passed to handlers."""

from __future__ import annotations


class SCPICommand:
    """Parsed SCPI command passed to handlers.

    When a handler is invoked, it receives an ``SCPICommand`` instead of
    a raw string.  The object exposes the full command text, the matched
    handler prefix, whether it's a query, and comma-separated arguments
    with optional type coercion.

    Attributes:
        raw: Full command string, e.g. ``"CONF:VOLT:DC 10,0.001"``.
        prefix: Matched handler prefix, e.g. ``"CONF:VOLT:DC"``.
        query: ``True`` if the command ends with ``?``.
        args: Comma-separated arguments after the prefix, whitespace-stripped.
    """

    def __init__(self, raw: str, prefix: str):
        self.raw = raw
        self.prefix = prefix
        self.query = raw.rstrip().endswith("?")

        # Parse args: everything after the prefix, split by comma
        remainder = raw[len(prefix):].strip()
        if remainder:
            self.args = [a.strip() for a in remainder.split(",")]
        else:
            self.args = []

    def arg(self, index: int, type: type = str, default=None):
        """Get argument by index with type coercion.

        Usage::

            cmd.arg(0, float)        # first arg as float, None if missing
            cmd.arg(1, int, 100)     # second arg as int, 100 if missing
        """
        if index >= len(self.args):
            return default
        try:
            return type(self.args[index])
        except (ValueError, TypeError):
            return default

    def __str__(self):
        return self.raw
