"""Offline tests for the error-report script (task 17).

These pin the behavior of ``scripts.error_report``: a read-only report of the
demo's recent X-Ray faults/errors plus the canary alarm state. The script is
structured for dependency inversion (engineering-practices §1) — a PURE
formatting/parsing core (``xray_trace_console_url`` / ``summarize_fault_traces``
/ ``format_report``) separated from the AWS I/O (a paginated
``get_trace_summaries`` and a ``describe_alarms`` read) — so the whole of this
suite runs fully offline: no AWS credentials, no network, no ``boto3`` client
constructed. The X-Ray and CloudWatch reads are exercised against hand-rolled
fakes injected in place of the real boto3 clients (mirroring
``test_player_stats.py``'s fakes), so no ``moto`` dependency is added.

Trace-summary shape throughout mirrors the X-Ray ``GetTraceSummaries`` output:
each summary is a dict with ``Id``, ``HasFault``/``HasError``/``HasThrottle``
booleans, an ``Http`` block carrying ``HttpStatus``, and a best-effort
``FaultRootCauses`` list from which a message is extracted.
"""

from __future__ import annotations

from typing import Any

from scripts import error_report

# --- xray_trace_console_url: the exact console deep link ----------------------

_SAMPLE_TRACE_ID = "1-68cf1e2a-1a2b3c4d5e6f7a8b9c0d1e2f"


def test_console_url_builds_the_expected_cloudwatch_xray_deep_link() -> None:
    url = error_report.xray_trace_console_url(_SAMPLE_TRACE_ID, region="us-east-1")
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        "?region=us-east-1#xray:traces/1-68cf1e2a-1a2b3c4d5e6f7a8b9c0d1e2f"
    )


def test_console_url_honours_a_non_default_region() -> None:
    url = error_report.xray_trace_console_url(_SAMPLE_TRACE_ID, region="eu-west-1")
    assert url.startswith("https://eu-west-1.console.aws.amazon.com/cloudwatch/home")
    assert "?region=eu-west-1#xray:traces/" in url


# --- summarize_fault_traces: keep only faults/errors, extract fields ----------


# A demo service name used to default in-scope test summaries (Requirement 14.6).
_DEMO_SERVICE = "XraySudokuDemoStack-NewGameFunction8B845F7F-3PwrA4lKqTEW"


def _summary(
    trace_id: str,
    *,
    fault: bool = False,
    error: bool = False,
    http_status: int | None = None,
    root_cause_message: str | None = None,
    service_names: list[str] | None = None,
) -> dict[str, Any]:
    """Build a GetTraceSummaries-shaped summary dict.

    When ``service_names`` is given, attaches ``ServiceIds`` as
    ``[{"Name": n} for n in service_names]``; when omitted, defaults to a single
    demo service so the summary stays in-scope under the demo-scoping filter
    (Requirement 14.6).
    """
    if service_names is None:
        service_names = [_DEMO_SERVICE]
    summary: dict[str, Any] = {
        "Id": trace_id,
        "HasFault": fault,
        "HasError": error,
        "HasThrottle": False,
        "ServiceIds": [{"Name": name} for name in service_names],
    }
    if http_status is not None:
        summary["Http"] = {"HttpStatus": http_status}
    if root_cause_message is not None:
        summary["FaultRootCauses"] = [
            {"Services": [{"EntityPath": [{"Name": "handler", "Exceptions": [
                {"Message": root_cause_message}
            ]}]}]}
        ]
    return summary


def test_summarize_keeps_only_fault_or_error_summaries() -> None:
    summaries = [
        _summary("1-a-1", fault=True, http_status=500),
        _summary("1-a-2", error=True, http_status=400),
        _summary("1-a-3"),  # clean: neither fault nor error -> dropped
    ]
    faults = error_report.summarize_fault_traces(summaries)
    kept_ids = {f.trace_id for f in faults}
    assert kept_ids == {"1-a-1", "1-a-2"}


def test_summarize_extracts_http_status_and_flags() -> None:
    faults = error_report.summarize_fault_traces(
        [_summary("1-a-1", fault=True, http_status=502)]
    )
    assert len(faults) == 1
    fault = faults[0]
    assert fault.trace_id == "1-a-1"
    assert fault.has_fault is True
    assert fault.has_error is False
    assert fault.http_status == 502


def test_summarize_extracts_a_best_effort_root_cause_message() -> None:
    faults = error_report.summarize_fault_traces(
        [_summary("1-a-1", fault=True, http_status=500,
                  root_cause_message="ValueError: bad board")]
    )
    assert faults[0].root_cause_message == "ValueError: bad board"


def test_summarize_attaches_the_console_url_for_each_kept_trace() -> None:
    faults = error_report.summarize_fault_traces(
        [_summary("1-a-1", fault=True)], region="us-east-1"
    )
    assert faults[0].console_url == error_report.xray_trace_console_url(
        "1-a-1", region="us-east-1"
    )


def test_summarize_handles_empty_input_without_crashing() -> None:
    assert error_report.summarize_fault_traces([]) == []


def test_summarize_iterates_in_a_defined_order_sorted_by_trace_id() -> None:
    # Deliberately unsorted input; output must be deterministic (sorted by id).
    summaries = [
        _summary("1-c-3", fault=True),
        _summary("1-a-1", fault=True),
        _summary("1-b-2", error=True),
    ]
    faults = error_report.summarize_fault_traces(summaries)
    assert [f.trace_id for f in faults] == ["1-a-1", "1-b-2", "1-c-3"]


def test_summarize_tolerates_a_missing_http_block() -> None:
    faults = error_report.summarize_fault_traces([_summary("1-a-1", fault=True)])
    assert faults[0].http_status is None
    assert faults[0].root_cause_message is None


# --- demo scoping: drop faults from unrelated services (Requirement 14.6) -----


def test_summarize_drops_traces_from_unrelated_services() -> None:
    # A mix of real-account noise: two faults from unrelated projects and one
    # genuine demo fault. Only the demo trace must survive (Requirement 14.6).
    summaries = [
        _summary(
            "1-unrelated-1",
            fault=True,
            http_status=500,
            service_names=["test-sam-deploy-ProcessPurchasesFn-2IPmzzMzEWgU"],
        ),
        _summary(
            "1-unrelated-2",
            fault=True,
            http_status=500,
            service_names=["NoSuchTable-test-sam-deploy"],
        ),
        _summary(
            "1-demo-1",
            fault=True,
            http_status=500,
            service_names=["XraySudokuDemoStack-NewGameFunction8B845F7F-3PwrA4lKqTEW"],
        ),
    ]
    faults = error_report.summarize_fault_traces(summaries)
    assert [f.trace_id for f in faults] == ["1-demo-1"]


def test_summarize_keeps_a_trace_whose_service_contains_the_stack_substring() -> None:
    faults = error_report.summarize_fault_traces(
        [
            _summary(
                "1-demo-1",
                fault=True,
                service_names=[
                    "XraySudokuDemoStack-SolveFunction12DDD7CE-D7a990Bi6VnO"
                ],
            )
        ]
    )
    assert [f.trace_id for f in faults] == ["1-demo-1"]


def test_summarize_keeps_the_sudoku_api_and_canary_services() -> None:
    faults = error_report.summarize_fault_traces(
        [
            _summary("1-api", error=True, service_names=["SudokuApi/prod"]),
            _summary("1-canary", fault=True, service_names=["xraysudokudemosddbbe0"]),
        ]
    )
    assert {f.trace_id for f in faults} == {"1-api", "1-canary"}


def test_summarize_drops_a_fault_with_no_service_ids() -> None:
    # A fault/error summary carrying no ServiceIds is unscopable -> dropped.
    summary = {
        "Id": "1-noscope",
        "HasFault": True,
        "HasError": False,
        "HasThrottle": False,
    }
    assert error_report.summarize_fault_traces([summary]) == []


# --- format_report: human report + JSON form ---------------------------------

_WINDOW = "last 1h (2026-09-19T21:00:00+00:00 - 2026-09-19T22:00:00+00:00)"


def test_format_report_renders_each_fault_with_its_console_url() -> None:
    faults = error_report.summarize_fault_traces(
        [
            _summary("1-a-1", fault=True, http_status=500,
                     root_cause_message="KeyError: playerId"),
            _summary("1-b-2", error=True, http_status=404),
        ]
    )
    report = error_report.format_report(faults, alarm_state="OK", window_desc=_WINDOW)
    assert "1-a-1" in report
    assert "1-b-2" in report
    # Each kept trace's clickable console URL appears in the report.
    for fault in faults:
        assert fault.console_url in report
    # The window and alarm state are surfaced.
    assert _WINDOW in report
    assert "OK" in report


def test_format_report_distinguishes_fault_from_error() -> None:
    faults = error_report.summarize_fault_traces(
        [
            _summary("1-fault", fault=True, http_status=500),
            _summary("1-err", error=True, http_status=400),
        ]
    )
    report = error_report.format_report(faults, alarm_state=None, window_desc=_WINDOW)
    lowered = report.lower()
    assert "fault" in lowered
    # 4xx errors are called out as (expected) client errors, not failures.
    assert "error" in lowered


def test_format_report_no_errors_case_reads_as_healthy_not_an_error() -> None:
    report = error_report.format_report([], alarm_state="OK", window_desc=_WINDOW)
    lowered = report.lower()
    assert "no errors" in lowered or "no faults" in lowered
    # It must not present the healthy case as a failure.
    assert "traceback" not in lowered


def test_format_report_surfaces_a_firing_alarm() -> None:
    report = error_report.format_report([], alarm_state="ALARM", window_desc=_WINDOW)
    assert "ALARM" in report


def test_report_as_json_has_the_expected_keys() -> None:
    faults = error_report.summarize_fault_traces(
        [_summary("1-a-1", fault=True, http_status=500)]
    )
    payload = error_report.report_as_dict(
        faults, alarm_state="ALARM", window_desc=_WINDOW
    )
    assert payload["window"] == _WINDOW
    assert payload["canary_alarm_state"] == "ALARM"
    assert payload["fault_count"] == 1
    assert isinstance(payload["faults"], list)
    first = payload["faults"][0]
    for key in ("trace_id", "has_fault", "has_error", "http_status",
                "root_cause_message", "console_url"):
        assert key in first


# --- X-Ray I/O layer: paginated get_trace_summaries with a fake client --------


class _FakeXRayClient:
    """Stand-in for an X-Ray client whose ``get_trace_summaries`` pages.

    Records the kwargs of each call so the test can assert the time window and
    the fault/error filter expression, and drives pagination via ``NextToken``
    exactly as boto3 does.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_trace_summaries(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def test_fetch_fault_trace_summaries_aggregates_across_two_pages_keeping_only_faults() -> None:
    page1 = {
        "TraceSummaries": [
            _summary("1-a-1", fault=True, http_status=500),
            _summary("1-a-2"),  # clean -> dropped by summarize
        ],
        "NextToken": "tok-1",
    }
    page2 = {
        "TraceSummaries": [
            _summary("1-a-3", error=True, http_status=404),
        ]
    }
    client = _FakeXRayClient(pages=[page1, page2])

    faults = error_report.fetch_fault_trace_summaries(
        client,
        start_time=1_600_000_000.0,
        end_time=1_600_003_600.0,
        region="us-east-1",
    )

    # Both pages were fetched, and only the fault/error traces were kept.
    assert {f.trace_id for f in faults} == {"1-a-1", "1-a-3"}
    assert len(client.calls) == 2


def test_fetch_pages_via_next_token() -> None:
    page1 = {"TraceSummaries": [_summary("1-a-1", fault=True)], "NextToken": "tok-1"}
    page2 = {"TraceSummaries": [_summary("1-a-2", fault=True)]}
    client = _FakeXRayClient(pages=[page1, page2])

    error_report.fetch_fault_trace_summaries(
        client, start_time=1_600_000_000.0, end_time=1_600_003_600.0
    )

    assert len(client.calls) == 2
    # The second call carries the NextToken returned by the first page.
    assert client.calls[1].get("NextToken") == "tok-1"


def test_fetch_passes_the_time_window_and_a_fault_error_filter() -> None:
    client = _FakeXRayClient(pages=[{"TraceSummaries": [_summary("1-a-1", fault=True)]}])

    error_report.fetch_fault_trace_summaries(
        client, start_time=1_600_000_000.0, end_time=1_600_003_600.0
    )

    call = client.calls[0]
    assert call["StartTime"] == 1_600_000_000.0
    assert call["EndTime"] == 1_600_003_600.0
    # The filter scopes to faults/errors.
    expression = call["FilterExpression"]
    assert "fault" in expression
    assert "error" in expression


def test_fetch_handles_an_empty_window_as_no_faults() -> None:
    client = _FakeXRayClient(pages=[{"TraceSummaries": []}])
    faults = error_report.fetch_fault_trace_summaries(
        client, start_time=1_600_000_000.0, end_time=1_600_003_600.0
    )
    assert faults == []


# --- CloudWatch alarm read: describe_alarms with a fake client ----------------


class _FakeCloudWatchClient:
    """Stand-in for a CloudWatch client exposing ``describe_alarms``."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def describe_alarms(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def test_read_canary_alarm_state_surfaces_an_alarm_state() -> None:
    client = _FakeCloudWatchClient(
        {
            "MetricAlarms": [
                {
                    "AlarmName": "XraySudokuDemoStack-SudokuCanarySuccessAlarm1234",
                    "StateValue": "ALARM",
                }
            ]
        }
    )
    state = error_report.read_canary_alarm_state(client)
    assert state == "ALARM"
    # It queried by the documented alarm-name prefix.
    assert client.calls[0]["AlarmNamePrefix"] == (
        "XraySudokuDemoStack-SudokuCanarySuccessAlarm"
    )


def test_read_canary_alarm_state_returns_ok_when_healthy() -> None:
    client = _FakeCloudWatchClient(
        {
            "MetricAlarms": [
                {
                    "AlarmName": "XraySudokuDemoStack-SudokuCanarySuccessAlarmABCD",
                    "StateValue": "OK",
                }
            ]
        }
    )
    assert error_report.read_canary_alarm_state(client) == "OK"


def test_read_canary_alarm_state_returns_none_when_no_alarm_present() -> None:
    # No matching alarm (e.g. the canary/alarm isn't deployed): None, not a crash.
    client = _FakeCloudWatchClient({"MetricAlarms": []})
    assert error_report.read_canary_alarm_state(client) is None
