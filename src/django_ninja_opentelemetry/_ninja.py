"""Guarded Django Ninja 1.7 exception and operation instrumentation.

The module is importable without Ninja installed.  Ninja imports and every
patch happen lazily inside :func:`install`, and only after the version and all
expected hook signatures have been verified.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from django.http import Http404
from opentelemetry.trace import Status, StatusCode

from ._telemetry import (
    REQUEST_STATE_ATTRIBUTE,
    RequestTelemetry,
    operation,
    record_exception,
    record_validation,
)

__all__ = ["install"]

_logger = logging.getLogger(__name__)

_SUPPORTED_MAJOR = 1
_SUPPORTED_MINOR = 7
_VERSION_PATTERN = re.compile(r"^\s*(\d+)\.(\d+)")
_DISTRIBUTION = "django-ninja"

_PATCH_MARK = "_django_ninja_otel_patched"
_WRAPPER_MARK = "_django_ninja_otel_wrapper"
_WRAPPER_ATTRIBUTE = "_django_ninja_otel_endpoint"

_state_lock = threading.Lock()
_install_attempted = False

_warning_lock = threading.Lock()
_warned: set[str] = set()
_wrap_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _Hooks:
    """Verified Ninja classes and exception types used by the patches."""

    api_class: Any
    operation_class: Any
    path_view_class: Any
    validation_error: type[BaseException]
    http_error: type[BaseException]


def install() -> None:
    """Install the Ninja 1.7 hooks once for this process.

    Missing Ninja is a silent no-op.  An unsupported Ninja version or an
    unexpected hook shape produces one warning and leaves every hook untouched.
    Repeated calls are idempotent.
    """

    global _install_attempted

    with _state_lock:
        if _install_attempted:
            return
        _install_attempted = True

        installed_version = _distribution_version()
        if installed_version is None:
            return
        if not _version_supported(installed_version):
            _warn_once(
                "version",
                "Django Ninja instrumentation disabled: unsupported version "
                f"{installed_version!r} (expected >=1.7,<1.8)",
            )
            return

        hooks = _verified_hooks()
        if hooks is None:
            return
        _patch_hooks(hooks)


def _distribution_version() -> str | None:
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError:
        return None
    except Exception as error:  # noqa: BLE001 - metadata must not break startup
        _warn_once(
            "metadata",
            "Django Ninja instrumentation disabled: distribution metadata "
            f"could not be read ({type(error).__name__})",
        )
        return None


def _version_supported(installed_version: str) -> bool:
    match = _VERSION_PATTERN.match(installed_version)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) == (
        _SUPPORTED_MAJOR,
        _SUPPORTED_MINOR,
    )


def _verified_hooks() -> _Hooks | None:
    try:
        from ninja.errors import HttpError, ValidationError
        from ninja.main import NinjaAPI
        from ninja.operation import AsyncOperation, Operation, PathView
    except Exception as error:  # noqa: BLE001 - fail open without Ninja hooks
        _warn_once(
            "import",
            "Django Ninja instrumentation disabled: expected 1.7 hooks "
            f"could not be imported ({type(error).__name__})",
        )
        return None

    get_values = getattr(Operation, "_get_values", None)
    result_to_response = getattr(Operation, "_result_to_response", None)
    get_view = getattr(PathView, "get_view", None)
    on_exception = getattr(NinjaAPI, "on_exception", None)
    expected = (
        (get_values, ("self", "request", "path_params", "temporal_response")),
        (
            result_to_response,
            ("self", "request", "result", "temporal_response"),
        ),
        (get_view, ("self",)),
        (on_exception, ("self", "request", "exc")),
    )
    for function, parameter_names in expected:
        if function is None:
            _warn_once(
                "missing-hook",
                "Django Ninja instrumentation disabled: an expected 1.7 hook "
                "is missing",
            )
            return None
        if not _signature_matches(function, parameter_names):
            _warn_once(
                "signature",
                "Django Ninja instrumentation disabled: unexpected 1.7 hook "
                f"signature for {_function_name(function)}",
            )
            return None
        if getattr(function, _PATCH_MARK, False):
            _warn_once(
                "already-patched",
                "Django Ninja instrumentation disabled: hooks are already patched",
            )
            return None

    async_get_values = getattr(AsyncOperation, "_get_values", None)
    async_result_to_response = getattr(AsyncOperation, "_result_to_response", None)
    if (
        async_get_values is not get_values
        or async_result_to_response is not result_to_response
    ):
        _warn_once(
            "async-hooks",
            "Django Ninja instrumentation disabled: async operation hooks are "
            "not inherited from Operation",
        )
        return None

    if not (
        isinstance(ValidationError, type)
        and issubclass(ValidationError, Exception)
        and isinstance(HttpError, type)
        and issubclass(HttpError, Exception)
    ):
        _warn_once(
            "exception-types",
            "Django Ninja instrumentation disabled: expected exception types "
            "are unavailable",
        )
        return None

    return _Hooks(
        api_class=NinjaAPI,
        operation_class=Operation,
        path_view_class=PathView,
        validation_error=ValidationError,
        http_error=HttpError,
    )


def _signature_matches(function: Any, names: tuple[str, ...]) -> bool:
    try:
        parameters = tuple(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return False
    if len(parameters) != len(names):
        return False
    return all(
        parameter.name == name
        and parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter, name in zip(parameters, names, strict=True)
    )


def _function_name(function: Any) -> str:
    module = getattr(function, "__module__", type(function).__module__)
    qualname = getattr(function, "__qualname__", type(function).__qualname__)
    return f"{module}.{qualname}"


def _patch_hooks(hooks: _Hooks) -> bool:
    original_on_exception = hooks.api_class.on_exception
    original_get_values = hooks.operation_class._get_values
    original_result_to_response = hooks.operation_class._result_to_response
    original_get_view = hooks.path_view_class.get_view

    try:
        new_on_exception = _make_on_exception(hooks, original_on_exception)
        new_get_values = _make_get_values(hooks, original_get_values)
        new_result_to_response = _make_result_to_response(
            hooks, original_result_to_response
        )
        new_get_view = _make_get_view(hooks, original_get_view)
    except Exception as error:  # noqa: BLE001 - fail open
        _warn_once(
            "wrap",
            "Django Ninja instrumentation disabled: hook wrapping failed "
            f"({type(error).__name__})",
        )
        return False

    replacements = (
        (hooks.api_class, "on_exception", original_on_exception, new_on_exception),
        (hooks.operation_class, "_get_values", original_get_values, new_get_values),
        (
            hooks.operation_class,
            "_result_to_response",
            original_result_to_response,
            new_result_to_response,
        ),
        (hooks.path_view_class, "get_view", original_get_view, new_get_view),
    )

    installed: list[tuple[Any, str, Any]] = []
    try:
        for target, name, original, replacement in replacements:
            setattr(target, name, replacement)
            installed.append((target, name, original))
    except Exception as error:  # noqa: BLE001 - restore an all-or-nothing patch
        for target, name, original in reversed(installed):
            try:
                setattr(target, name, original)
            except Exception:
                pass
        _warn_once(
            "patch",
            "Django Ninja instrumentation disabled: hook installation failed "
            f"({type(error).__name__})",
        )
        return False

    for _, _, _, replacement in replacements:
        try:
            setattr(replacement, _PATCH_MARK, True)
        except Exception:
            pass
    return True


def _make_on_exception(
    hooks: _Hooks, original: Callable[..., Any]
) -> Callable[..., Any]:
    expected = (hooks.validation_error, hooks.http_error, Http404)

    @wraps(original)
    def on_exception(self: Any, request: Any, exc: BaseException) -> Any:
        state = _request_state(request)
        if state is None:
            return original(self, request, exc)

        validation = isinstance(exc, hooks.validation_error)
        if validation:
            _safe(lambda: record_validation(state, _validation_count(exc)))

        try:
            response = original(self, request, exc)
        except BaseException as escaping:
            if isinstance(escaping, Exception) and not isinstance(escaping, expected):
                _safe(lambda exc=escaping: record_exception(state, exc))
            raise

        if validation or isinstance(exc, (hooks.http_error, Http404)):
            return response

        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code >= 500:
            _safe(lambda: record_exception(state, exc))
        return response

    return on_exception


def _expected_operation_exceptions(
    hooks: _Hooks,
) -> tuple[type[BaseException], ...]:
    return (hooks.validation_error, hooks.http_error, Http404)


def _make_get_values(hooks: _Hooks, original: Callable[..., Any]) -> Callable[..., Any]:
    expected = _expected_operation_exceptions(hooks)

    @wraps(original)
    def get_values(
        self: Any, request: Any, path_params: Any, temporal_response: Any
    ) -> Any:
        state = _request_state(request)
        with operation(state, "django_ninja.input_resolution", self.view_func) as span:
            try:
                return original(self, request, path_params, temporal_response)
            except BaseException as exc:
                _mark_operation_error(span, exc, expected)
                raise

    return get_values


def _make_result_to_response(
    hooks: _Hooks, original: Callable[..., Any]
) -> Callable[..., Any]:
    expected = _expected_operation_exceptions(hooks)

    @wraps(original)
    def result_to_response(
        self: Any, request: Any, result: Any, temporal_response: Any
    ) -> Any:
        state = _request_state(request)
        with operation(state, "django_ninja.serialization", self.view_func) as span:
            try:
                return original(self, request, result, temporal_response)
            except BaseException as exc:
                _mark_operation_error(span, exc, expected)
                raise

    return result_to_response


def _make_get_view(hooks: _Hooks, original: Callable[..., Any]) -> Callable[..., Any]:
    expected = _expected_operation_exceptions(hooks)

    @wraps(original)
    def get_view(self: Any) -> Any:
        _instrument_path_view(self, expected)
        return original(self)

    return get_view


def _instrument_path_view(
    path_view: Any, expected: tuple[type[BaseException], ...]
) -> None:
    for entry in getattr(path_view, "operations", ()):
        view_func = getattr(entry, "view_func", None)
        if not callable(view_func):
            continue
        try:
            entry.view_func = _endpoint_wrapper(view_func, expected)
        except Exception as error:  # noqa: BLE001 - never break URL generation
            _warn_once(
                "endpoint-wrap",
                "Django Ninja endpoint instrumentation skipped for one operation "
                f"({type(error).__name__})",
            )


def _endpoint_wrapper(
    function: Callable[..., Any],
    expected: tuple[type[BaseException], ...],
) -> Callable[..., Any]:
    if getattr(function, _WRAPPER_MARK, False):
        return function

    try:
        cached = getattr(function, _WRAPPER_ATTRIBUTE)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    with _wrap_lock:
        try:
            cached = getattr(function, _WRAPPER_ATTRIBUTE)
        except Exception:
            cached = None
        if cached is not None:
            return cached

        if inspect.iscoroutinefunction(function):
            wrapper = _async_endpoint_wrapper(function, expected)
        else:
            # Sync generators and async generators are intentionally handled by
            # a sync wrapper.  Ninja passes the returned generator to its own
            # streaming response code; awaiting it would be incorrect and
            # would leave the endpoint span active across yields.
            wrapper = _sync_endpoint_wrapper(function, expected)

        try:
            setattr(wrapper, _WRAPPER_MARK, True)
            setattr(function, _WRAPPER_ATTRIBUTE, wrapper)
        except Exception:
            # Callables that reject attributes still get a marked wrapper, so
            # cloned operations cannot wrap the wrapper again.
            pass
        return wrapper


def _sync_endpoint_wrapper(
    function: Callable[..., Any],
    expected: tuple[type[BaseException], ...],
) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        request = _request_argument(args, kwargs)
        state = _request_state(request)
        with operation(state, "django_ninja.endpoint", function) as span:
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                _mark_operation_error(span, exc, expected)
                raise

    return wrapper


def _async_endpoint_wrapper(
    function: Callable[..., Any],
    expected: tuple[type[BaseException], ...],
) -> Callable[..., Any]:
    @wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        request = _request_argument(args, kwargs)
        state = _request_state(request)
        with operation(state, "django_ninja.endpoint", function) as span:
            try:
                return await function(*args, **kwargs)
            except BaseException as exc:
                _mark_operation_error(span, exc, expected)
                raise

    return wrapper


def _request_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any | None:
    if args:
        return args[0]
    return kwargs.get("request")


def _request_state(request: Any | None) -> RequestTelemetry | None:
    if request is None:
        return None
    try:
        state = getattr(request, REQUEST_STATE_ATTRIBUTE, None)
    except Exception:
        return None
    if state is None:
        return None

    # Django resolves the route before invoking the view.  Recording it here
    # lets validation warnings and internal spans carry http.route without
    # retaining raw paths or request data.  A malformed state must never
    # replace the exception Ninja is already handling.
    try:
        if getattr(state, "route", None) is None:
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None)
            if isinstance(route, str) and route:
                state.route = route
    except Exception:
        pass
    return state


def _safe(call: Callable[[], Any]) -> None:
    try:
        call()
    except Exception as error:  # noqa: BLE001 - telemetry must not break requests
        _warn_once(
            "telemetry-call",
            f"Django Ninja telemetry recording failed ({type(error).__name__})",
        )


def _mark_operation_error(
    span: Any,
    exc: BaseException,
    expected: tuple[type[BaseException], ...],
) -> None:
    if span is None or not isinstance(exc, Exception):
        return
    if isinstance(exc, expected):
        return
    try:
        span.set_attribute("error.type", _exception_type(exc))
        span.set_status(Status(StatusCode.ERROR))
    except Exception as error:  # noqa: BLE001 - telemetry must not break requests
        _warn_once(
            "span-error",
            "Django Ninja operation span status could not be recorded "
            f"({type(error).__name__})",
        )


def _validation_count(exc: BaseException) -> int:
    try:
        count = len(getattr(exc, "errors", ()))
    except TypeError:
        return 0
    return count if isinstance(count, int) and count >= 0 else 0


def _exception_type(exc: BaseException) -> str:
    cls = type(exc)
    return (
        cls.__qualname__
        if cls.__module__ == "builtins"
        else f"{cls.__module__}.{cls.__qualname__}"
    )


def _warn_once(key: str, message: str) -> None:
    with _warning_lock:
        if key in _warned:
            return
        _warned.add(key)
    _logger.warning(message)
