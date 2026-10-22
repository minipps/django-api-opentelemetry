"""Native sync/async Django middleware and streaming lifecycle instrumentation.

Request state lives on the request object. No ``MiddlewareMixin`` adapter is
used for normal async requests.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from asgiref import sync as asgiref_sync
from asgiref.sync import markcoroutinefunction
from django.conf import settings
from opentelemetry import context as otel_context
from opentelemetry import trace

from . import _telemetry
from ._config import Config, get_config
from ._telemetry import REQUEST_STATE_ATTRIBUTE, RequestTelemetry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
    from django.http.response import HttpResponseBase

__all__ = ["TelemetryMiddleware", "observe_django_exception"]

_logger = logging.getLogger(__name__)
_WARN_LOCK = threading.Lock()
_WARNED: set[tuple[str, type[BaseException]]] = set()

# The standard OpenTelemetry Django integration has used both a module-level
# middleware and the ``otel_middleware`` module. Both live below this package
# prefix. Detecting the configured path avoids importing the optional package.
_EXTERNAL_HTTP_MIDDLEWARE_PREFIX = "opentelemetry.instrumentation.django.middleware"
_EXTERNAL_HTTP_MIDDLEWARE_NAMES = frozenset(
    {
        "DjangoInstrumentationMiddleware",
        "_DjangoInstrumentationMiddleware",
        "_DjangoMiddleware",
    }
)
_CONTEXT_BOUND_ATTRIBUTE = "_django_ninja_otel_context_bound"
_ASYNC_CLOSE_TIMEOUT = 5.0

# asgiref delegates to inspect on Python 3.12+, but resolving it through the
# module keeps the supported detection behavior without a deprecated stdlib
# alias in type checking.
_iscoroutinefunction = getattr(  # noqa: B009 - avoid a deprecated type stub
    asgiref_sync, "iscoroutinefunction"
)


class TelemetryMiddleware:
    """Create and complete one request telemetry record per Django request."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        self._is_async = _iscoroutinefunction(get_response)
        config = get_config()
        if config.http_instrumentation and _external_http_instrumentation():
            # Another integration owns the HTTP root span and metrics. Keep
            # tracing/logging enabled for Ninja operation spans and hooks.
            config = replace(config, http_instrumentation=False)
        self.config: Config = config

        # Choose a native view hook for the handler's execution mode. Django
        # adapts a sync hook for ASGI and an async hook for WSGI, instead of
        # adapting every request/middleware call through MiddlewareMixin.
        if self._is_async:
            self.process_view = self._process_view_async
            markcoroutinefunction(cast("Any", self))
        else:
            self.process_view = self._process_view_sync

    def __call__(self, request: HttpRequest) -> Any:
        if self._is_async:
            return self._async_call(request)
        return self._sync_call(request)

    def _sync_call(self, request: HttpRequest) -> HttpResponse:
        state = _telemetry.begin_request(request, self.config)
        try:
            with _telemetry.activate(state):
                response = self.get_response(request)
        except BaseException as error:
            _safe_finish(state, None, error)
            raise
        return _handle_response(request, state, response)

    async def _async_call(self, request: HttpRequest) -> HttpResponse:
        state = _telemetry.begin_request(request, self.config)
        try:
            with _telemetry.activate(state):
                response = await self.get_response(request)
        except BaseException as error:
            _safe_finish(state, None, error)
            raise
        return _handle_response(request, state, response)

    def _process_view_sync(
        self,
        request: HttpRequest,
        view_func: Any,
        view_args: Any,
        view_kwargs: Any,
    ) -> None:
        _bind_request_context(request)
        _set_route(request)

    async def _process_view_async(
        self,
        request: HttpRequest,
        view_func: Any,
        view_args: Any,
        view_kwargs: Any,
    ) -> None:
        _bind_request_context(request)
        _set_route(request)


def _handle_response(
    request: HttpRequest,
    state: RequestTelemetry | None,
    response: HttpResponse,
) -> HttpResponse:
    _set_route(request)
    status_code = _response_status(response)

    if not getattr(response, "streaming", False):
        _safe_finish(state, status_code)
        return response

    try:
        _instrument_stream(state, cast("StreamingHttpResponse", response))
    except Exception as error:  # noqa: BLE001 - instrumentation must fail open
        # Never turn a valid application response into an error because
        # telemetry setup failed. Finish now so active counters stay balanced;
        # a partially installed wrapper remains idempotent.
        _warn("stream instrument", error)
        _safe_finish(state, status_code)
    return response


def _instrument_stream(
    state: RequestTelemetry | None,
    response: StreamingHttpResponse,
) -> None:
    if state is None:
        return

    original_close = response.close
    iterator = getattr(response, "_iterator")  # noqa: B009 - Django private API

    # Wrap Django's actual iterator, not the ``streaming_content`` getter. The
    # getter creates a new map/async generator whose close does not propagate
    # to the user's generator. Replacing the iterator also preserves the
    # property's byte conversion, FileResponse.file_to_stream and Django's own
    # resource-closer bookkeeping.
    if not getattr(response, "is_async", False):

        def close() -> None:
            try:
                with _telemetry.activate(state):
                    original_close()
            finally:
                _safe_finish(state, _response_status(response))

        setattr(response, "close", close)  # noqa: B010 - per-instance override
        setattr(  # noqa: B010 - Django intentionally keeps this private
            response,
            "_iterator",
            _wrap_sync(cast("Iterator[Any]", iterator), state, response),
        )
        return

    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        loop_thread: int | None = threading.get_ident()
    except RuntimeError:
        loop = loop_thread = None

    original_aclose = getattr(response, "aclose", None)
    closed = [False]
    wrapped = _wrap_async(
        cast("AsyncIterator[Any]", iterator),
        state,
        response,
        lambda: closed.__setitem__(0, True),
    )
    setattr(response, "_iterator", wrapped)  # noqa: B010 - private iterator

    async def cleanup() -> None:
        cleanup_error: BaseException | None = None
        with _telemetry.activate(state):
            try:
                await cast("Any", wrapped).aclose()
            except BaseException as error:
                cleanup_error = error
            if not closed[0]:
                try:
                    aclose_iterator = getattr(iterator, "aclose", None)
                    if aclose_iterator is not None:
                        await aclose_iterator()
                    closed[0] = True
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
            if callable(original_aclose):
                try:
                    await original_aclose()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

    async def aclose() -> None:
        try:
            await cleanup()
        finally:
            _safe_finish(state, _response_status(response))

    def close() -> None:
        # Django 5.2/6.1 call the synchronous response.close from sync_to_async
        # after a disconnect, even for async streams. Bridge the required
        # aclose onto the captured serving loop.
        coroutine = cleanup()
        try:
            _bridge_async_cleanup(coroutine, loop, loop_thread)
        finally:
            try:
                with _telemetry.activate(state):
                    original_close()
            finally:
                _safe_finish(state, _response_status(response))

    setattr(response, "aclose", aclose)  # noqa: B010 - per-instance override
    setattr(response, "close", close)  # noqa: B010 - per-instance override


def _bridge_async_cleanup(
    coroutine: Any,
    loop: asyncio.AbstractEventLoop | None,
    loop_thread: int | None,
) -> None:
    if loop is None:
        try:
            asyncio.run(coroutine)
        except BaseException as error:
            _warn("stream cleanup", error)
        return

    if loop.is_closed() or not loop.is_running():
        coroutine.close()
        _warn("stream cleanup", RuntimeError("event loop is not running"))
        return

    if threading.get_ident() == loop_thread:
        loop.create_task(coroutine)
        return

    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        future.result(timeout=_ASYNC_CLOSE_TIMEOUT)
    except BaseException as error:
        future.cancel()
        _warn("stream cleanup", error)


def _wrap_sync(
    iterator: Iterator[Any],
    state: RequestTelemetry,
    response: StreamingHttpResponse,
) -> Iterator[Any]:
    failure: BaseException | None = None
    try:
        while True:
            with _telemetry.activate(state):
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                except BaseException as error:
                    failure = error
                    raise
            # The request context is detached before the consumer sees a
            # chunk; a paused generator never keeps it attached.
            yield chunk
    finally:
        # Django already registered the original iterator's close (when it has
        # one), so response.close owns that call. Closing it here as well would
        # invoke custom iterators twice.
        _safe_finish(state, _response_status(response), failure)


async def _wrap_async(
    iterator: AsyncIterator[Any],
    state: RequestTelemetry,
    response: StreamingHttpResponse,
    on_closed: Callable[[], None],
) -> AsyncIterator[Any]:
    failure: BaseException | None = None
    closing = False
    try:
        while True:
            with _telemetry.activate(state):
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                except BaseException as error:
                    failure = error
                    raise
            yield chunk
    except GeneratorExit:
        closing = True
        raise
    finally:
        cleanup_error: BaseException | None = None
        with _telemetry.activate(state):
            try:
                aclose = getattr(iterator, "aclose", None)
                if aclose is None:
                    on_closed()
                else:
                    await aclose()
                    on_closed()
            except BaseException as error:
                cleanup_error = error
        _safe_finish(state, _response_status(response), failure)
        if cleanup_error is not None and failure is None and not closing:
            raise cleanup_error


def _bind_request_context(request: HttpRequest) -> None:
    state = _request_state(request)
    if state is not None:
        _bind_borrowed_context(state)


def _bind_borrowed_context(state: RequestTelemetry) -> None:
    # If we run outside contrib, begin_request may see an empty context before
    # contrib opens its span. Adopt it once; response handling must not
    # overwrite it after contrib detaches.
    if state.span is not None or state.config.http_instrumentation:
        return
    if getattr(state, _CONTEXT_BOUND_ATTRIBUTE, False):
        return

    current = otel_context.get_current()
    span_context = trace.get_current_span(current).get_span_context()
    if not span_context.is_valid or span_context.is_remote:
        return

    state.context = current
    setattr(state, _CONTEXT_BOUND_ATTRIBUTE, True)  # noqa: B010 - private marker


def _set_route(request: HttpRequest) -> None:
    state = _request_state(request)
    if state is None:
        return
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    if isinstance(route, str) and route:
        state.route = route


def _request_state(request: HttpRequest) -> RequestTelemetry | None:
    state = getattr(request, REQUEST_STATE_ATTRIBUTE, None)
    return state if isinstance(state, RequestTelemetry) else None


def observe_django_exception(
    sender: Any,
    request: HttpRequest,
    **kwargs: Any,
) -> None:
    # AppConfig owns registration. Django sends this from inside the exception
    # handler; record_exception deduplicates by object identity.
    state = _request_state(request)
    if state is None or state.finished:
        return
    _bind_borrowed_context(state)

    error = sys.exception()
    if error is None:
        error = sys.exc_info()[1]
    if not isinstance(error, Exception):
        return

    try:
        _telemetry.record_exception(state, error)
    except Exception as failure:  # noqa: BLE001 - a signal receiver must not fail
        _warn("exception signal", failure)


def _external_http_instrumentation() -> bool:
    middleware = getattr(settings, "MIDDLEWARE", ()) or ()
    for entry in middleware:
        if not isinstance(entry, str):
            continue
        if not entry.startswith(_EXTERNAL_HTTP_MIDDLEWARE_PREFIX):
            continue
        # SQLCommenter and other optional middleware share the package prefix
        # but do not own HTTP spans or metrics.
        if entry.rsplit(".", 1)[-1] in _EXTERNAL_HTTP_MIDDLEWARE_NAMES:
            return True
    return False


def _response_status(response: HttpResponseBase) -> int | None:
    status_code = getattr(response, "status_code", None)
    try:
        return None if status_code is None else int(status_code)
    except (TypeError, ValueError):
        return None


def _safe_finish(
    state: RequestTelemetry | None,
    status_code: int | None,
    error: BaseException | None = None,
) -> None:
    try:
        _telemetry.finish_request(state, status_code, error)
    except Exception as failure:  # noqa: BLE001 - telemetry must fail open
        _warn("request finish", failure)


def _warn(operation: str, error: BaseException) -> None:
    key = (operation, type(error))
    with _WARN_LOCK:
        if key in _WARNED or len(_WARNED) >= 16:
            return
        _WARNED.add(key)
    _logger.warning("OpenTelemetry %s failed (%s)", operation, key[1].__name__)
