#!/usr/bin/env python3
"""Bounded OTLP smoke check for the built wheel."""

import importlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCOPE_NAME = "django_api_opentelemetry"
NINJA_VALIDATION_EVENT = "django_ninja.validation.failed"
DRF_VALIDATION_EVENT = "django_drf.validation.failed"
EXCEPTION_EVENT = "http.server.request.exception"
DURATION_METRIC = "http.server.request.duration"
ACTIVE_METRIC = "http.server.active_requests"
NINJA_VALIDATION_COUNT = "django_ninja.validation.error_count"
DRF_VALIDATION_COUNT = "django_drf.validation.error_count"
ROUTE_ATTRIBUTE = "http.route"
OK_ROUTE, ITEM_ROUTE, ERROR_ROUTE = "/ok", "/items/{item_id}", "/error"
DRF_OK_ROUTE, DRF_ITEM_ROUTE, DRF_ERROR_ROUTE = (
    "/drf/ok",
    "/drf/items",
    "/drf/error",
)
REQUIRED_ROUTES = (OK_ROUTE, ITEM_ROUTE, ERROR_ROUTE)
DRF_REQUIRED_ROUTES = (DRF_OK_ROUTE, DRF_ITEM_ROUTE, DRF_ERROR_ROUTE)
ALL_ROUTES = REQUIRED_ROUTES + DRF_REQUIRED_ROUTES
DRF_DISPATCH_SPAN = "django_drf.dispatch"
DRF_INITIAL_SPAN = "django_drf.initial"
DRF_RENDER_SPAN = "django_drf.render"
DRF_OPERATION_COUNTS = {
    DRF_DISPATCH_SPAN: 3,
    DRF_INITIAL_SPAN: 3,
    # The unexpected view error escapes before DRF finalizes a response, so only
    # the successful and validation responses are rendered through DRF.
    DRF_RENDER_SPAN: 2,
}
INVALID_INPUT = "smoke-invalid-input"
DRF_INVALID_FIELD = "item_id"
TRACES_FILE, METRICS_FILE, LOGS_FILE = "traces.jsonl", "metrics.jsonl", "logs.jsonl"
SIGNAL_FILES = (TRACES_FILE, METRICS_FILE, LOGS_FILE)
HOST, PORT = "127.0.0.1", 8000
DEFAULT_TELEMETRY_DIR = Path(os.environ.get("SMOKE_TELEMETRY_DIR", "/telemetry"))
DEFAULT_OTLP_ENDPOINT = os.environ.get("SMOKE_OTLP_ENDPOINT", "http://collector:4318")
DEFAULT_TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "60"))


class SmokeError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise SmokeError(f"cannot read {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    lines = text.splitlines(keepends=True)
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            if number == len(lines) and not raw.endswith(("\n", "\r")):
                continue
            raise SmokeError(f"line {number} is malformed JSON: {exc.msg}") from exc
    return records


def verify_fresh_output(directory: Path) -> None:
    _require(directory.is_dir(), f"telemetry directory is missing: {directory}")
    stale = [
        name
        for name in SIGNAL_FILES
        if (directory / name).is_file() and (directory / name).stat().st_size > 0
    ]
    if stale:
        names = ", ".join(stale)
        raise SmokeError(f"stale collector output ({names}); run compose down -v")


def _attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        value = item.get("value", {})
        result[item["key"]] = value.get("stringValue", value.get("intValue"))
    return result


def _route(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    route = value if value.startswith("/") else f"/{value}"
    return route.replace("<item_id>", "{item_id}")


def _scoped(
    records: list[dict[str, Any]], resource_key: str, scope_key: str, item_key: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        for resource in record.get(resource_key, []):
            for scope in resource.get(scope_key, []):
                if scope.get("scope", {}).get("name") == SCOPE_NAME:
                    result.extend(scope.get(item_key, []))
    return result


def validate_smoke_data(
    traces: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    logs: list[dict[str, Any]],
) -> dict[str, Any]:
    all_spans = _scoped(traces, "resourceSpans", "scopeSpans", "spans")
    spans = [span for span in all_spans if span.get("kind") == 2]
    routes: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        route = _route(_attributes(span.get("attributes", [])).get(ROUTE_ATTRIBUTE))
        if route:
            routes.setdefault(route, []).append(span)
    for route in ALL_ROUTES:
        _require(
            len(routes.get(route, [])) == 1, f"expected one SERVER span at {route}"
        )
    _require(len(spans) == 6, f"expected six SERVER spans, got {len(spans)}")

    operation_counts: dict[str, int] = {}
    for span in all_spans:
        name = span.get("name")
        if isinstance(name, str) and name in DRF_OPERATION_COUNTS:
            operation_counts[name] = operation_counts.get(name, 0) + 1
    for name, expected in DRF_OPERATION_COUNTS.items():
        _require(
            operation_counts.get(name, 0) == expected,
            f"expected {expected} {name} spans, got {operation_counts.get(name, 0)}",
        )

    logs = _scoped(logs, "resourceLogs", "scopeLogs", "logRecords")
    validation_logs = [
        log
        for log in logs
        if log.get("eventName") in {NINJA_VALIDATION_EVENT, DRF_VALIDATION_EVENT}
    ]
    _require(
        len(validation_logs) == 2,
        "expected exactly one Ninja and one DRF validation log",
    )
    validations: dict[Any, list[dict[str, Any]]] = {}
    for log in validation_logs:
        validations.setdefault(log.get("eventName"), []).append(log)
    for event_name, count_attribute, route, extra_privacy in (
        (NINJA_VALIDATION_EVENT, NINJA_VALIDATION_COUNT, ITEM_ROUTE, ()),
        (
            DRF_VALIDATION_EVENT,
            DRF_VALIDATION_COUNT,
            DRF_ITEM_ROUTE,
            (DRF_INVALID_FIELD,),
        ),
    ):
        matching = validations.get(event_name, [])
        _require(
            len(matching) == 1, f"expected exactly one validation log: {event_name}"
        )
        validation = matching[0]
        _require(validation.get("severityText") == "WARN", "validation log is not WARN")
        attributes = _attributes(validation.get("attributes", []))
        _require(
            str(attributes.get(count_attribute)) == "1",
            f"wrong validation count for {event_name}",
        )
        _require(
            _route(attributes.get(ROUTE_ATTRIBUTE)) == route,
            f"wrong route for {event_name}",
        )
        encoded = json.dumps(validation)
        _require(INVALID_INPUT not in encoded, f"{event_name} leaks input")
        for value in extra_privacy:
            _require(value not in encoded, f"{event_name} leaks validation details")

    exceptions = [log for log in logs if log.get("eventName") == EXCEPTION_EVENT]
    _require(len(exceptions) == 2, "expected exactly two exception logs")
    exceptions_by_route: dict[str, dict[str, Any]] = {}
    for exception in exceptions:
        _require(exception.get("severityText") == "ERROR", "exception is not ERROR")
        attributes = _attributes(exception.get("attributes", []))
        exception_type = attributes.get("exception.type")
        _require(
            isinstance(exception_type, str) and exception_type.endswith("RuntimeError"),
            "wrong exception.type",
        )
        route = _route(attributes.get(ROUTE_ATTRIBUTE))
        if route not in (ERROR_ROUTE, DRF_ERROR_ROUTE):
            raise SmokeError(f"exception has unexpected route: {route}")
        _require(route not in exceptions_by_route, f"duplicate exception at {route}")
        exceptions_by_route[route] = exception

    for route in (ERROR_ROUTE, DRF_ERROR_ROUTE):
        exception = exceptions_by_route.get(route)
        if exception is None:
            raise SmokeError(f"missing exception log at {route}")
        trace_id, span_id = exception.get("traceId"), exception.get("spanId")
        _require(bool(trace_id and span_id), "exception has no trace/span IDs")
        correlated = next(
            (span for span in routes[route] if span.get("spanId") == span_id),
            None,
        )
        if correlated is None or correlated.get("traceId") != trace_id:
            raise SmokeError(f"exception correlation is missing at {route}")

    metric_items = _scoped(metrics, "resourceMetrics", "scopeMetrics", "metrics")
    metric_names = {metric.get("name") for metric in metric_items}
    for required in (DURATION_METRIC, ACTIVE_METRIC):
        _require(required in metric_names, f"missing required metric: {required}")

    duration_counts: dict[str, int] = {}
    for metric in metric_items:
        if metric.get("name") != DURATION_METRIC:
            continue
        histogram = metric.get("histogram") or metric.get("exponentialHistogram") or {}
        for point in histogram.get("dataPoints", []):
            route = _route(
                _attributes(point.get("attributes", [])).get(ROUTE_ATTRIBUTE)
            )
            if route is None:
                continue
            try:
                count = int(point.get("count", "0"))
            except (TypeError, ValueError) as exc:
                raise SmokeError("duration metric count is malformed") from exc
            duration_counts[route] = max(duration_counts.get(route, 0), count)
    for route in ALL_ROUTES:
        _require(
            duration_counts.get(route) == 1,
            f"expected one duration measurement at {route}",
        )

    return {
        "server_spans": len(spans),
        "validation_logs": len(validations),
        "exception_logs": len(exceptions),
        "drf_dispatch_spans": operation_counts.get(DRF_DISPATCH_SPAN, 0),
        "drf_initial_spans": operation_counts.get(DRF_INITIAL_SPAN, 0),
        "drf_render_spans": operation_counts.get(DRF_RENDER_SPAN, 0),
        "metric_names": tuple(sorted(name for name in metric_names if name)),
    }


def run_smoke(
    telemetry_dir: Path, otlp_endpoint: str, timeout: float
) -> dict[str, Any]:
    process: subprocess.Popen[str] | None = None
    server_output = ""
    try:
        package = importlib.import_module("django_api_opentelemetry")
        distribution = importlib.metadata.distribution("django-api-opentelemetry")
        origin = Path(package.__file__).resolve()
        root = Path(str(distribution.locate_file(""))).resolve()
        _require(
            not origin.is_relative_to(Path("/workspace/src").resolve()),
            f"package imported from source/editable tree: {origin}",
        )
        _require(origin.is_relative_to(root), f"package imported outside {root}")
        verify_fresh_output(telemetry_dir)

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OTEL_")
            and key not in {"PYTHONPATH", "PYTHONHOME", "DJANGO_SETTINGS_MODULE"}
        }
        env.update(
            DJANGO_NINJA_OTEL_AUTO_CONFIGURE="true",
            OTEL_EXPORTER_OTLP_ENDPOINT=otlp_endpoint,
            OTEL_METRIC_EXPORT_INTERVAL="1000",
            OTEL_BSP_SCHEDULE_DELAY="100",
            OTEL_BLRP_SCHEDULE_DELAY="100",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "smoke_app:application",
                "--log-level=warning",
                "--no-access-log",
            ],
            cwd=Path(__file__).resolve().parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SmokeError(f"uvicorn exited with status {process.returncode}")
            try:
                with socket.create_connection((HOST, PORT), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise SmokeError("uvicorn did not listen within 20s")
        drf_validation_body = json.dumps({"item_id": INVALID_INPUT}).encode()
        smoke_requests = (
            (OK_ROUTE, "GET", None, 200, "success"),
            (f"/items/{INVALID_INPUT}", "GET", None, 422, "validation"),
            (ERROR_ROUTE, "GET", None, 500, "error"),
            (DRF_OK_ROUTE, "GET", None, 200, "DRF success"),
            (
                DRF_ITEM_ROUTE,
                "POST",
                drf_validation_body,
                400,
                "DRF validation",
            ),
            (DRF_ERROR_ROUTE, "GET", None, 500, "DRF error"),
        )
        for path, method, data, expected, label in smoke_requests:
            headers = {"Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                f"http://{HOST}:{PORT}{path}",
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    status, body = response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read().decode()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise SmokeError(
                    f"{label} request failed: {type(exc).__name__}"
                ) from exc
            _require(status == expected, f"{label} request returned HTTP {status}")
            if expected == 200:
                _require(
                    json.loads(body) == {"status": "ok"}, "unexpected success body"
                )

        if process.poll() is None:
            process.terminate()
        try:
            server_output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            server_output, _ = process.communicate(timeout=5)
        process = None

        deadline = time.monotonic() + timeout
        last_error: SmokeError | None = None
        while True:
            try:
                summary = validate_smoke_data(
                    read_jsonl(telemetry_dir / TRACES_FILE),
                    read_jsonl(telemetry_dir / METRICS_FILE),
                    read_jsonl(telemetry_dir / LOGS_FILE),
                )
                break
            except SmokeError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise SmokeError(
                        f"telemetry not ready within {timeout:.0f}s: {last_error}"
                    ) from exc
                time.sleep(0.2)
        print(f"smoke passed: package={origin}, {summary}", flush=True)
        return summary
    except BaseException as exc:
        if process is not None:
            process.kill()
            server_output = process.communicate(timeout=5)[0] or ""
        print(f"smoke diagnostics ({type(exc).__name__}: {exc})", file=sys.stderr)
        for name in SIGNAL_FILES:
            path = telemetry_dir / name
            size = path.stat().st_size if path.exists() else "missing"
            print(f"{name}: {size}", file=sys.stderr)
        if server_output.strip():
            print(f"uvicorn output (tail):\n{server_output[-4000:]}", file=sys.stderr)
        raise


if __name__ == "__main__":
    run_smoke(DEFAULT_TELEMETRY_DIR, DEFAULT_OTLP_ENDPOINT, DEFAULT_TIMEOUT)
