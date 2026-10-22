"""Guarded Django REST framework 3.16-3.18 instrumentation.

The module is importable without Django REST framework installed. DRF imports
and every patch happen lazily inside :func:`install`, and only after the
version and all expected hook signatures have been verified.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from django.core.exceptions import PermissionDenied
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

_SUPPORTED_VERSIONS = frozenset({(3, 16), (3, 17), (3, 18)})
_VERSION_PATTERN = re.compile(r"^\s*(\d+)\.(\d+)")
_DISTRIBUTION = "djangorestframework"

_PATCH_MARK = "_django_drf_otel_patched"

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_VAR_POSITIONAL = inspect.Parameter.VAR_POSITIONAL
_VAR_KEYWORD = inspect.Parameter.VAR_KEYWORD

_EXPECTED_DJANGO_EXCEPTIONS = (Http404, PermissionDenied)

_state_lock = threading.Lock()
_install_attempted = False

_warning_lock = threading.Lock()
_warned: set[str] = set()


@dataclass(frozen=True, slots=True)
class _Hooks:
    """Verified DRF classes and exception types used by the patches."""

    api_view: Any
    response: Any
    api_exception: type[BaseException]
    validation_error: type[BaseException]


def install() -> None:
    """Install the DRF hooks once for this process.

    Missing DRF is a silent no-op. An unsupported DRF version or an unexpected
    hook shape produces one warning and leaves every hook untouched. Repeated
    calls are idempotent.
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
                "Django REST framework instrumentation disabled: unsupported "
                f"version {installed_version!r} (expected >=3.16,<3.19)",
            )
            return

        try:
            hooks = _verified_hooks()
        except Exception as error:  # noqa: BLE001 - startup must fail open
            _warn_once(
                "verify",
                "Django REST framework instrumentation disabled: hook "
                f"verification failed ({type(error).__name__})",
            )
            return
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
            "Django REST framework instrumentation disabled: distribution "
            f"metadata could not be read ({type(error).__name__})",
        )
        return None


def _version_supported(installed_version: str) -> bool:
    match = _VERSION_PATTERN.match(installed_version)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) in _SUPPORTED_VERSIONS


def _verified_hooks() -> _Hooks | None:
    try:
        from rest_framework.exceptions import APIException, ValidationError
        from rest_framework.response import Response
        from rest_framework.views import APIView
    except Exception as error:  # noqa: BLE001 - fail open without DRF hooks
        _warn_once(
            "import",
            "Django REST framework instrumentation disabled: expected hooks "
            f"could not be imported ({type(error).__name__})",
        )
        return None

    expected = (
        (
            getattr(APIView, "dispatch", None),
            (
                ("self", _POK),
                ("request", _POK),
                ("args", _VAR_POSITIONAL),
                ("kwargs", _VAR_KEYWORD),
            ),
        ),
        (
            getattr(APIView, "initial", None),
            (
                ("self", _POK),
                ("request", _POK),
                ("args", _VAR_POSITIONAL),
                ("kwargs", _VAR_KEYWORD),
            ),
        ),
        (
            getattr(APIView, "handle_exception", None),
            (("self", _POK), ("exc", _POK)),
        ),
        (
            getattr(Response, "render", None),
            (("self", _POK),),
        ),
    )
    for function, parameters in expected:
        if function is None:
            _warn_once(
                "missing-hook",
                "Django REST framework instrumentation disabled: an expected "
                "hook is missing",
            )
            return None
        if not _signature_matches(function, parameters):
            _warn_once(
                "signature",
                "Django REST framework instrumentation disabled: unexpected "
                f"hook signature for {_function_name(function)}",
            )
            return None
        if getattr(function, _PATCH_MARK, False):
            _warn_once(
                "already-patched",
                "Django REST framework instrumentation disabled: hooks are "
                "already patched",
            )
            return None

    if not (
        isinstance(APIException, type)
        and issubclass(APIException, Exception)
        and isinstance(ValidationError, type)
        and issubclass(ValidationError, APIException)
    ):
        _warn_once(
            "exception-types",
            "Django REST framework instrumentation disabled: expected "
            "exception types are unavailable",
        )
        return None

    return _Hooks(
        api_view=APIView,
        response=Response,
        api_exception=APIException,
        validation_error=ValidationError,
    )


def _signature_matches(
    function: Any,
    parameters: tuple[tuple[str, Any], ...],
) -> bool:
    try:
        actual = tuple(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return False
    if len(actual) != len(parameters):
        return False
    return all(
        parameter.name == name and parameter.kind is kind
        for parameter, (name, kind) in zip(actual, parameters, strict=True)
    )


def _function_name(function: Any) -> str:
    module = getattr(function, "__module__", type(function).__module__)
    qualname = getattr(function, "__qualname__", type(function).__qualname__)
    return f"{module}.{qualname}"


def _patch_hooks(hooks: _Hooks) -> bool:
    original_dispatch = hooks.api_view.dispatch
    original_initial = hooks.api_view.initial
    original_handle_exception = hooks.api_view.handle_exception
    original_render = hooks.response.render

    try:
        new_dispatch = _make_dispatch(hooks, original_dispatch)
        new_initial = _make_initial(hooks, original_initial)
        new_handle_exception = _make_handle_exception(hooks, original_handle_exception)
        new_render = _make_render(hooks, original_render)
    except Exception as error:  # noqa: BLE001 - fail open
        _warn_once(
            "wrap",
            "Django REST framework instrumentation disabled: hook wrapping "
            f"failed ({type(error).__name__})",
        )
        return False

    replacements = (
        (hooks.api_view, "dispatch", original_dispatch, new_dispatch),
        (hooks.api_view, "initial", original_initial, new_initial),
        (
            hooks.api_view,
            "handle_exception",
            original_handle_exception,
            new_handle_exception,
        ),
        (hooks.response, "render", original_render, new_render),
    )

    installed: list[tuple[Any, str, Any, bool]] = []
    try:
        for target, name, original, replacement in replacements:
            if getattr(target, name) is not original:
                raise RuntimeError("hook changed during installation")
            had_own_attribute = name in getattr(target, "__dict__", {})
            setattr(target, name, replacement)
            installed.append((target, name, original, had_own_attribute))
    except Exception as error:  # noqa: BLE001 - restore an all-or-nothing patch
        for target, name, original, had_own_attribute in reversed(installed):
            try:
                if had_own_attribute:
                    setattr(target, name, original)
                else:
                    delattr(target, name)
            except Exception:
                pass
        _warn_once(
            "patch",
            "Django REST framework instrumentation disabled: hook installation "
            f"failed ({type(error).__name__})",
        )
        return False

    for _, _, _, replacement in replacements:
        try:
            setattr(replacement, _PATCH_MARK, True)
        except Exception:
            pass
    return True


def _make_dispatch(hooks: _Hooks, original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def dispatch(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        state = _request_state(request)
        if state is None:
            return original(self, request, *args, **kwargs)

        with operation(state, "django_drf.dispatch", original) as span:
            try:
                response = original(self, request, *args, **kwargs)
            except BaseException as exc:
                _mark_operation_error(span, exc, hooks)
                _record_failure(state, exc, hooks)
                raise
            _mark_response_error(span, response)
            return response

    return dispatch


def _make_initial(hooks: _Hooks, original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def initial(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        state = _request_state(request)
        if state is None:
            return original(self, request, *args, **kwargs)

        with operation(state, "django_drf.initial", original) as span:
            try:
                return original(self, request, *args, **kwargs)
            except BaseException as exc:
                _mark_operation_error(span, exc, hooks)
                raise

    return initial


def _make_handle_exception(
    hooks: _Hooks, original: Callable[..., Any]
) -> Callable[..., Any]:
    @wraps(original)
    def handle_exception(self: Any, exc: BaseException) -> Any:
        state = _request_state(getattr(self, "request", None))
        if state is None:
            return original(self, exc)

        if isinstance(exc, hooks.validation_error):
            _safe(
                lambda: record_validation(
                    state,
                    _validation_leaf_count(exc),
                    framework="django_drf",
                )
            )

        try:
            response = original(self, exc)
        except BaseException as escaping:
            _record_failure(state, escaping, hooks)
            raise

        status_code = _response_status(response)
        if (
            status_code is not None
            and status_code >= 500
            and isinstance(exc, Exception)
        ):
            _safe(lambda: record_exception(state, exc))
        return response

    return handle_exception


def _make_render(hooks: _Hooks, original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def render(self: Any) -> Any:
        if _already_rendered(self):
            return original(self)

        state = _response_state(self)
        if state is None:
            return original(self)

        with operation(state, "django_drf.render", original) as span:
            try:
                return original(self)
            except BaseException as exc:
                _mark_operation_error(span, exc, hooks)
                _record_failure(state, exc, hooks)
                raise

    return render


def _request_state(request: Any | None) -> RequestTelemetry | None:
    if request is None:
        return None

    try:
        state = getattr(request, REQUEST_STATE_ATTRIBUTE, None)
    except Exception:
        return None
    if not isinstance(state, RequestTelemetry):
        return None

    # Django resolves the route before invoking the view. Recording it here
    # lets validation warnings and operation spans carry http.route without
    # retaining raw paths or request data.
    try:
        if state.route is None:
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None)
            if isinstance(route, str) and route:
                state.route = route
    except Exception:
        pass
    return state


def _response_state(response: Any) -> RequestTelemetry | None:
    try:
        context = getattr(response, "renderer_context", None)
    except Exception:
        return None
    if not isinstance(context, Mapping):
        return None
    try:
        request = context.get("request")
    except Exception:
        return None
    return _request_state(request)


def _already_rendered(response: Any) -> bool:
    try:
        return bool(getattr(response, "_is_rendered", False))
    except Exception:
        return False


def _validation_leaf_count(exc: BaseException) -> int:
    try:
        return _leaf_count(
            getattr(exc, "detail")  # noqa: B009 - DRF attaches detail dynamically
        )
    except Exception:  # noqa: BLE001 - never inspect application data further
        return 0


def _leaf_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(_leaf_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_leaf_count(item) for item in value)
    return 1


def _is_error_exception(exc: BaseException, hooks: _Hooks) -> bool:
    if not isinstance(exc, Exception):
        return False
    if isinstance(exc, hooks.api_exception):
        status_code = _exception_status(exc)
        return status_code is not None and status_code >= 500
    return not isinstance(exc, _EXPECTED_DJANGO_EXCEPTIONS)


def _record_failure(state: RequestTelemetry, exc: BaseException, hooks: _Hooks) -> None:
    if not _is_error_exception(exc, hooks):
        return
    _safe(lambda: record_exception(state, exc))


def _mark_operation_error(span: Any, exc: BaseException, hooks: _Hooks) -> None:
    if span is None or not _is_error_exception(exc, hooks):
        return
    _mark_span_error(span, _exception_type(exc))


def _mark_response_error(span: Any, response: Any) -> None:
    if span is None:
        return
    status_code = _response_status(response)
    if status_code is not None and status_code >= 500:
        _mark_span_error(span, str(status_code))


def _mark_span_error(span: Any, error_type: str) -> None:
    try:
        span.set_attribute("error.type", error_type)
        span.set_status(Status(StatusCode.ERROR))
    except Exception as error:  # noqa: BLE001 - telemetry must not break requests
        _warn_once(
            "span-error",
            "Django REST framework operation span status could not be "
            f"recorded ({type(error).__name__})",
        )


def _exception_status(exc: BaseException) -> int | None:
    try:
        status_code = getattr(exc, "status_code", None)
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _response_status(response: Any) -> int | None:
    try:
        status_code = getattr(response, "status_code", None)
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _exception_type(exc: BaseException) -> str:
    cls = type(exc)
    return (
        cls.__qualname__
        if cls.__module__ == "builtins"
        else f"{cls.__module__}.{cls.__qualname__}"
    )


def _safe(call: Callable[[], Any]) -> None:
    try:
        call()
    except Exception as error:  # noqa: BLE001 - telemetry must not break requests
        _warn_once(
            "telemetry-call",
            "Django REST framework telemetry recording failed "
            f"({type(error).__name__})",
        )


def _warn_once(key: str, message: str) -> None:
    with _warning_lock:
        if key in _warned:
            return
        _warned.add(key)
    _logger.warning(message)
