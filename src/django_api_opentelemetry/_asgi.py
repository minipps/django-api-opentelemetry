"""Optional ASGI lifespan wrapper for graceful telemetry shutdown."""

from __future__ import annotations

import asyncio

from asgiref.typing import (
    ASGI3Application,
    ASGIReceiveCallable,
    ASGISendCallable,
    LifespanShutdownCompleteEvent,
    LifespanStartupCompleteEvent,
    Scope,
)

from . import _runtime

__all__ = ["instrument_asgi"]


def instrument_asgi(application: ASGI3Application) -> ASGI3Application:
    """Wrap an ASGI application with minimal Django-compatible lifespan support.

    Native Django ASGI applications do not implement lifespan. This wrapper
    forwards every non-lifespan scope unchanged, acknowledges startup without
    initializing exporters, and performs package-owned shutdown when the server
    closes the lifespan. No Django class or signal is patched.
    """

    async def instrumented(
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        if scope["type"] != "lifespan":
            await application(scope, receive, send)
            return

        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                startup_complete: LifespanStartupCompleteEvent = {
                    "type": "lifespan.startup.complete"
                }
                await send(startup_complete)
            elif message["type"] == "lifespan.shutdown":
                await asyncio.to_thread(_runtime.shutdown)
                shutdown_complete: LifespanShutdownCompleteEvent = {
                    "type": "lifespan.shutdown.complete"
                }
                await send(shutdown_complete)
                return

    return instrumented
