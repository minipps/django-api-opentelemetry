"""OpenTelemetry instrumentation for Django and Django Ninja."""

from ._asgi import instrument_asgi
from ._runtime import shutdown

__version__ = "0.1.0"

__all__ = ["__version__", "instrument_asgi", "shutdown"]
