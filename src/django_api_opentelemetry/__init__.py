"""OpenTelemetry instrumentation for Django, Django Ninja and Django REST framework."""

from ._asgi import instrument_asgi
from ._runtime import shutdown

__version__ = "0.2.0"

__all__ = ["__version__", "instrument_asgi", "shutdown"]
