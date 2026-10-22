"""Focused checks for the OTLP smoke-check JSONL parser and assertions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from scripts import smoke


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


def _span(
    route: str,
    *,
    trace_id: str,
    span_id: str,
    kind: int = 2,
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": f"GET {route}",
        "kind": kind,
        "attributes": [_attribute("http.route", route)],
    }


def _operation_span(name: str, *, trace_id: str, span_id: str) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "attributes": [],
    }


def _traces() -> list[dict[str, Any]]:
    spans = [
        _span(smoke.OK_ROUTE, trace_id="1" * 32, span_id="1" * 16),
        _span(smoke.ITEM_ROUTE, trace_id="2" * 32, span_id="2" * 16),
        _span(smoke.ERROR_ROUTE, trace_id="3" * 32, span_id="3" * 16),
        _span(smoke.DRF_OK_ROUTE, trace_id="4" * 32, span_id="4" * 16),
        _span(smoke.DRF_ITEM_ROUTE, trace_id="5" * 32, span_id="5" * 16),
        _span(smoke.DRF_ERROR_ROUTE, trace_id="6" * 32, span_id="6" * 16),
    ]
    for number, name in enumerate(
        (
            smoke.DRF_DISPATCH_SPAN,
            smoke.DRF_INITIAL_SPAN,
            smoke.DRF_RENDER_SPAN,
        ),
        start=1,
    ):
        spans.append(
            _operation_span(
                name,
                trace_id="4" * 32,
                span_id=f"{number:x}" * 16,
            )
        )
    # Three dispatches and initials, and two renders (the unexpected error has
    # no DRF response to render).
    spans.extend(
        [
            _operation_span(
                smoke.DRF_DISPATCH_SPAN, trace_id="5" * 32, span_id="a" * 16
            ),
            _operation_span(
                smoke.DRF_INITIAL_SPAN, trace_id="5" * 32, span_id="b" * 16
            ),
            _operation_span(smoke.DRF_RENDER_SPAN, trace_id="5" * 32, span_id="c" * 16),
            _operation_span(
                smoke.DRF_DISPATCH_SPAN, trace_id="6" * 32, span_id="d" * 16
            ),
            _operation_span(
                smoke.DRF_INITIAL_SPAN, trace_id="6" * 32, span_id="e" * 16
            ),
        ]
    )
    return [
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "scope": {"name": smoke.SCOPE_NAME},
                            "spans": spans,
                        }
                    ]
                }
            ]
        }
    ]


def _metric_point(route: str, count: int = 1) -> dict[str, Any]:
    return {
        "attributes": [_attribute("http.route", route)],
        "count": str(count),
    }


def _metrics() -> list[dict[str, Any]]:
    duration_points = [_metric_point(route) for route in smoke.ALL_ROUTES]
    active_points = [
        {
            "attributes": [_attribute("http.route", route)],
            "asInt": "0",
        }
        for route in smoke.ALL_ROUTES
    ]
    return [
        {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "scope": {"name": smoke.SCOPE_NAME},
                            "metrics": [
                                {
                                    "name": smoke.DURATION_METRIC,
                                    "unit": "s",
                                    "histogram": {"dataPoints": duration_points},
                                },
                                {
                                    "name": smoke.ACTIVE_METRIC,
                                    "unit": "{request}",
                                    "sum": {"dataPoints": active_points},
                                },
                            ],
                        }
                    ]
                }
            ]
        }
    ]


def _validation_log(
    event_name: str,
    count_attribute: str,
    route: str,
    *,
    trace_id: str,
    span_id: str,
) -> dict[str, Any]:
    return {
        "timeUnixNano": "1",
        "severityNumber": 13,
        "severityText": "WARN",
        "eventName": event_name,
        "body": {"stringValue": "Request validation failed"},
        "attributes": [
            _attribute(count_attribute, 1),
            _attribute("http.route", route),
        ],
        "traceId": trace_id,
        "spanId": span_id,
    }


def _exception_log(
    route: str,
    *,
    trace_id: str,
    span_id: str,
    message: str,
) -> dict[str, Any]:
    return {
        "timeUnixNano": "2",
        "severityNumber": 17,
        "severityText": "ERROR",
        "eventName": smoke.EXCEPTION_EVENT,
        "body": {"stringValue": "Unhandled exception in Django request"},
        "attributes": [
            _attribute("http.route", route),
            _attribute("exception.type", "RuntimeError"),
            _attribute("exception.message", message),
            _attribute("exception.stacktrace", "Traceback (most recent call last)"),
        ],
        "traceId": trace_id,
        "spanId": span_id,
    }


def _logs() -> list[dict[str, Any]]:
    return [
        {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "scope": {"name": smoke.SCOPE_NAME},
                            "logRecords": [
                                _validation_log(
                                    smoke.NINJA_VALIDATION_EVENT,
                                    smoke.NINJA_VALIDATION_COUNT,
                                    smoke.ITEM_ROUTE,
                                    trace_id="2" * 32,
                                    span_id="2" * 16,
                                ),
                                _validation_log(
                                    smoke.DRF_VALIDATION_EVENT,
                                    smoke.DRF_VALIDATION_COUNT,
                                    smoke.DRF_ITEM_ROUTE,
                                    trace_id="5" * 32,
                                    span_id="5" * 16,
                                ),
                                _exception_log(
                                    smoke.ERROR_ROUTE,
                                    trace_id="3" * 32,
                                    span_id="3" * 16,
                                    message="smoke unhandled error",
                                ),
                                _exception_log(
                                    smoke.DRF_ERROR_ROUTE,
                                    trace_id="6" * 32,
                                    span_id="6" * 16,
                                    message="smoke unhandled DRF error",
                                ),
                            ],
                        }
                    ]
                }
            ]
        }
    ]


def _log_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return records[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def _server_spans(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return traces[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_valid_three_signals() -> None:
    summary = smoke.validate_smoke_data(_traces(), _metrics(), _logs())

    assert summary["server_spans"] == 6
    assert summary["validation_logs"] == 2
    assert summary["exception_logs"] == 2
    assert summary["drf_dispatch_spans"] == 3
    assert summary["drf_initial_spans"] == 3
    assert summary["drf_render_spans"] == 2
    assert smoke.DURATION_METRIC in summary["metric_names"]
    assert smoke.ACTIVE_METRIC in summary["metric_names"]


def test_absent_exception_correlation_is_rejected() -> None:
    logs = _logs()
    exceptions = [
        record
        for record in _log_records(logs)
        if record["eventName"] == smoke.EXCEPTION_EVENT
    ]
    exceptions[0]["spanId"] = "f" * 16

    with pytest.raises(smoke.SmokeError, match="correlation is missing"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_drf_validation_input_leak_is_rejected() -> None:
    logs = _logs()
    validation = next(
        record
        for record in _log_records(logs)
        if record["eventName"] == smoke.DRF_VALIDATION_EVENT
    )
    validation["body"] = {
        "stringValue": f"Request validation failed: {smoke.INVALID_INPUT}"
    }

    with pytest.raises(smoke.SmokeError, match="leaks input"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_drf_validation_field_leak_is_rejected() -> None:
    logs = _logs()
    validation = next(
        record
        for record in _log_records(logs)
        if record["eventName"] == smoke.DRF_VALIDATION_EVENT
    )
    validation["attributes"].append(_attribute("validation.field", "item_id"))

    with pytest.raises(smoke.SmokeError, match="leaks validation details"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_duplicate_validation_log_is_rejected() -> None:
    logs = _logs()
    duplicate = copy.deepcopy(_log_records(logs)[0])
    _log_records(logs).append(duplicate)

    with pytest.raises(smoke.SmokeError, match="exactly one"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_missing_drf_validation_log_is_rejected() -> None:
    logs = _logs()
    records = _log_records(logs)
    records.pop(1)

    with pytest.raises(smoke.SmokeError, match="one Ninja and one DRF"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_missing_drf_operation_span_is_rejected() -> None:
    traces = _traces()
    spans = _server_spans(traces)
    spans.pop()

    with pytest.raises(smoke.SmokeError, match="django_drf.initial"):
        smoke.validate_smoke_data(traces, _metrics(), _logs())


def test_missing_duration_measurement_is_rejected() -> None:
    metrics = _metrics()
    points = metrics[0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0][
        "histogram"
    ]["dataPoints"]
    points.pop()

    with pytest.raises(smoke.SmokeError, match="duration measurement"):
        smoke.validate_smoke_data(_traces(), metrics, _logs())


def test_django_style_route_placeholders_are_normalized() -> None:
    traces = _traces()
    item_span = _server_spans(traces)[1]
    item_span["attributes"][0]["value"]["stringValue"] = "items/<item_id>"
    logs = _logs()
    _log_records(logs)[0]["attributes"][1]["value"]["stringValue"] = "items/<item_id>"

    summary = smoke.validate_smoke_data(traces, _metrics(), logs)

    assert summary["server_spans"] == 6


def test_fresh_output_accepts_missing_and_empty_files(tmp_path: Path) -> None:
    smoke.verify_fresh_output(tmp_path)
    (tmp_path / "logs.jsonl").write_text("", encoding="utf-8")
    smoke.verify_fresh_output(tmp_path)


def test_fresh_output_rejects_stale_collector_data(tmp_path: Path) -> None:
    (tmp_path / "traces.jsonl").write_text('{"resourceSpans": []}\n', encoding="utf-8")

    with pytest.raises(smoke.SmokeError, match="down -v"):
        smoke.verify_fresh_output(tmp_path)


def test_fresh_output_requires_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeError, match="missing"):
        smoke.verify_fresh_output(tmp_path / "missing")


def test_partial_trailing_json_line_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    complete = json.dumps({"resourceSpans": []})
    path.write_text(f'{complete}\n{{"resourceSpans": "unfinished"', encoding="utf-8")

    assert smoke.read_jsonl(path) == [{"resourceSpans": []}]


def test_malformed_complete_line_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(
        f"{json.dumps({'resourceSpans': []})}\nnot-json\n", encoding="utf-8"
    )

    with pytest.raises(smoke.SmokeError, match="line 2"):
        smoke.read_jsonl(path)


def test_read_jsonl_ignores_partial_writer_output(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    complete = json.dumps({"resourceSpans": []})
    path.write_text(f'{complete}\n{{"resourceSpans":', encoding="utf-8")

    assert smoke.read_jsonl(path) == [{"resourceSpans": []}]
