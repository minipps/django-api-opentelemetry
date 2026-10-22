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


def _traces() -> list[dict[str, Any]]:
    spans = [
        _span(smoke.OK_ROUTE, trace_id="1" * 32, span_id="1" * 16),
        _span(smoke.ITEM_ROUTE, trace_id="2" * 32, span_id="2" * 16),
        _span(smoke.ERROR_ROUTE, trace_id="3" * 32, span_id="3" * 16),
    ]
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
    duration_points = [_metric_point(route) for route in smoke.REQUIRED_ROUTES]
    active_points = [
        {
            "attributes": [_attribute("http.route", route)],
            "asInt": "0",
        }
        for route in smoke.REQUIRED_ROUTES
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


def _validation_log() -> dict[str, Any]:
    return {
        "timeUnixNano": "1",
        "severityNumber": 13,
        "severityText": "WARN",
        "eventName": smoke.VALIDATION_EVENT,
        "body": {"stringValue": "Request validation failed"},
        "attributes": [
            _attribute(smoke.VALIDATION_COUNT, 1),
            _attribute("http.route", smoke.ITEM_ROUTE),
        ],
        "traceId": "2" * 32,
        "spanId": "2" * 16,
    }


def _exception_log() -> dict[str, Any]:
    return {
        "timeUnixNano": "2",
        "severityNumber": 17,
        "severityText": "ERROR",
        "eventName": smoke.EXCEPTION_EVENT,
        "body": {"stringValue": "Unhandled exception in Django request"},
        "attributes": [
            _attribute("http.route", smoke.ERROR_ROUTE),
            _attribute("exception.type", "RuntimeError"),
            _attribute("exception.message", "smoke unhandled error"),
            _attribute("exception.stacktrace", "Traceback (most recent call last)"),
        ],
        "traceId": "3" * 32,
        "spanId": "3" * 16,
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
                                _validation_log(),
                                _exception_log(),
                            ],
                        }
                    ]
                }
            ]
        }
    ]


def _log_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return records[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def test_valid_three_signals() -> None:
    summary = smoke.validate_smoke_data(_traces(), _metrics(), _logs())

    assert summary["server_spans"] == 3
    assert summary["validation_logs"] == 1
    assert summary["exception_logs"] == 1
    assert smoke.DURATION_METRIC in summary["metric_names"]
    assert smoke.ACTIVE_METRIC in summary["metric_names"]


def test_absent_exception_correlation_is_rejected() -> None:
    logs = _logs()
    _log_records(logs)[1]["spanId"] = "f" * 16

    with pytest.raises(smoke.SmokeError, match="correlation is missing"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_validation_input_leak_is_rejected() -> None:
    logs = _logs()
    _log_records(logs)[0]["body"] = {
        "stringValue": f"Request validation failed: {smoke.INVALID_INPUT}"
    }

    with pytest.raises(smoke.SmokeError, match="leaks input"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_duplicate_validation_log_is_rejected() -> None:
    logs = _logs()
    duplicate = copy.deepcopy(_log_records(logs)[0])
    _log_records(logs).append(duplicate)

    with pytest.raises(smoke.SmokeError, match="exactly one"):
        smoke.validate_smoke_data(_traces(), _metrics(), logs)


def test_django_style_route_placeholders_are_normalized() -> None:
    traces = _traces()
    item_span = traces[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    item_span["attributes"][0]["value"]["stringValue"] = "items/<item_id>"
    logs = _logs()
    _log_records(logs)[0]["attributes"][1]["value"]["stringValue"] = "items/<item_id>"

    summary = smoke.validate_smoke_data(traces, _metrics(), logs)

    assert summary["server_spans"] == 3


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
