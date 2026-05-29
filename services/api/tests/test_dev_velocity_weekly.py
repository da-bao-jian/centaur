import datetime as dt
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workflows import dev_velocity_weekly  # noqa: E402
from workflows.dev_velocity_weekly import (  # noqa: E402
    Input,
    ReportWindow,
    _active_cycle_issues_from_cycles,
    _active_cycle_issue_rows,
    _active_cycles,
    _format_duration,
    _render_report,
    _report_window,
    handler,
)


def _issue(
    identifier: str,
    title: str,
    *,
    completed: bool = False,
    bug: bool = False,
    parent: dict | None = None,
    created_at: str = "2026-05-18T10:00:00Z",
) -> dict:
    return {
        "id": identifier,
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/luban/issue/{identifier}",
        "createdAt": created_at,
        "completedAt": "2026-05-27T12:00:00Z" if completed else None,
        "state": {"type": "completed" if completed else "started"},
        "assignee": {"name": "Harry"},
        "parent": parent,
        "labels": {"nodes": [{"name": "Bug"}] if bug else [{"name": "Feature"}]},
    }


def test_weekly_velocity_schedule_runs_friday_before_daily_eod_beijing() -> None:
    assert dev_velocity_weekly.WORKFLOW_NAME == "dev_velocity_weekly"
    assert dev_velocity_weekly.SCHEDULE["cron"] == "50 23 * * 5"
    assert dev_velocity_weekly.SCHEDULE["timezone"] == "Asia/Shanghai"
    assert dev_velocity_weekly.SCHEDULE["input"]["slack_sender_name"] == "Pris"
    assert dev_velocity_weekly.SCHEDULE["input"]["lookback_hours"] == 168


def test_weekly_report_window_defaults_to_previous_168_hours() -> None:
    inp = Input(now="2026-05-29T23:50:00+08:00")

    window = _report_window(inp)

    assert window.end == dt.datetime(2026, 5, 29, 15, 50, tzinfo=dt.timezone.utc)
    assert window.start == dt.datetime(2026, 5, 22, 15, 50, tzinfo=dt.timezone.utc)
    assert window.start_local.strftime("%Y-%m-%d %H:%M") == "2026-05-22 23:50"
    assert window.end_local.strftime("%Y-%m-%d %H:%M") == "2026-05-29 23:50"


def test_active_cycle_rows_keep_created_parent_and_cycle_metadata() -> None:
    cycles = [
        {
            "name": "Cycle 11",
            "startsAt": "2026-05-18",
            "endsAt": "2026-06-01",
            "issues": {
                "nodes": [
                    _issue("MOB-1", "Parent completed", completed=True),
                    _issue(
                        "MOB-2",
                        "Child completed",
                        completed=True,
                        parent={"id": "MOB-1", "identifier": "MOB-1"},
                    ),
                ]
            },
        }
    ]

    issues, cycle_names = _active_cycle_issues_from_cycles(
        cycles,
        report_date=dt.date(2026, 5, 29),
    )

    assert cycle_names == ["Cycle 11"]
    assert [issue["identifier"] for issue in issues] == ["MOB-1", "MOB-2"]
    assert issues[0]["_active_cycle_name"] == "Cycle 11"
    assert issues[0]["_active_cycle_starts_at"] == "2026-05-18"
    assert issues[1]["parent"]["identifier"] == "MOB-1"


@pytest.mark.asyncio
async def test_active_cycles_uses_lightweight_cycle_query() -> None:
    class CaptureLinearClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, str, int]] = []

        async def _paginate_connection(self, query, variables, *, connection, limit):
            self.calls.append((query, variables, connection, limit))
            return []

    client = CaptureLinearClient()
    window = ReportWindow(
        start=dt.datetime(2026, 5, 22, 15, 50, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 5, 29, 15, 50, tzinfo=dt.timezone.utc),
        timezone="Asia/Shanghai",
    )

    await _active_cycles(client, Input(linear_team_keys=["MOB"]), window)  # type: ignore[arg-type]

    query, variables, connection, limit = client.calls[0]
    assert connection == "cycles"
    assert limit == 20
    assert "issues(first:" not in query
    assert variables["filter"]["team"] == {"key": {"eq": "MOB"}}


@pytest.mark.asyncio
async def test_active_cycle_issue_rows_fetches_cycle_issues_by_cycle_id() -> None:
    class CaptureLinearClient:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, int]] = []

        async def _issue_page(self, filters, *, limit):
            self.calls.append((filters, limit))
            return [_issue("MOB-1", "Parent completed", completed=True)]

    client = CaptureLinearClient()
    cycles = [
        {
            "id": "cycle-1",
            "name": "Cycle 11",
            "startsAt": "2026-05-18",
            "endsAt": "2026-06-01",
        }
    ]

    issues, cycle_names = await _active_cycle_issue_rows(  # type: ignore[arg-type]
        client,
        cycles,
        report_date=dt.date(2026, 5, 29),
    )

    assert cycle_names == ["Cycle 11"]
    assert [issue["identifier"] for issue in issues] == ["MOB-1"]
    assert client.calls == [({"cycle": {"id": {"eq": "cycle-1"}}}, 100)]


def test_format_duration_keeps_pr_merge_time_compact() -> None:
    assert _format_duration(20 * 60) == "20m"
    assert _format_duration(3 * 3600 + 30 * 60) == "3h 30m"
    assert _format_duration(2 * 86400 + 4 * 3600) == "2d 4h"
    assert _format_duration(None) == "n/a"


def test_render_weekly_report_keeps_only_scorecard() -> None:
    inp = Input(
        now="2026-05-29T23:50:00+08:00",
        reviewer_slack_mentions={"alice": "<@U123>"},
    )
    window = ReportWindow(
        start=dt.datetime(2026, 5, 22, 15, 50, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 5, 29, 15, 50, tzinfo=dt.timezone.utc),
        timezone="Asia/Shanghai",
    )
    closed_parent = _issue("MOB-1", "Ship parent feature", completed=True)
    open_parent = _issue("MOB-2", "Finish parent feature")
    closed_bug = _issue("MOB-3", "Fix runtime resume", completed=True, bug=True)
    open_bug = _issue("MOB-4", "Fix webhook retry", bug=True)
    metrics = {
        "issues_closed": [closed_parent, closed_bug],
        "issues_created": [open_parent],
        "active_cycle_issues": [closed_parent, open_parent, closed_bug, open_bug],
        "active_cycle_closed": [closed_parent, closed_bug],
        "active_cycle_open": [open_parent, open_bug],
        "active_bugs": [closed_bug, open_bug],
        "active_bugs_closed": [closed_bug],
        "open_bugs": [open_bug],
        "parent_created_this_cycle": [closed_parent, open_parent],
        "parent_created_closed": [closed_parent],
        "parent_created_open": [open_parent],
        "active_cycle_names": ["Cycle 11"],
        "prs_opened": [
            {
                "number": 586,
                "title": "fix(frontend): health factor bar",
                "html_url": "https://github.com/lu-bann/mobius/pull/586",
                "user": {"login": "will-luban"},
                "base": {"repo": {"full_name": "lu-bann/mobius"}},
            }
        ],
        "prs_merged": [
            {
                "number": 581,
                "title": "feat(storage): use postgresql",
                "html_url": "https://github.com/lu-bann/mobius/pull/581",
                "created_at": "2026-05-26T10:00:00Z",
                "merged_at": "2026-05-27T10:00:00Z",
                "user": {"login": "chuwt"},
                "base": {"repo": {"full_name": "lu-bann/mobius"}},
            }
        ],
        "outstanding_prs": [
            {
                "number": 582,
                "title": "feat(contract): executor contract",
                "html_url": "https://github.com/lu-bann/mobius/pull/582",
                "created_at": "2026-05-20T10:00:00Z",
                "requested_reviewers": [{"login": "alice"}],
                "base": {"repo": {"full_name": "lu-bann/mobius"}},
            },
            {
                "number": 539,
                "title": "feat(contracts): Aera vault integration",
                "html_url": "https://github.com/lu-bann/mobius/pull/539",
                "created_at": "2026-05-29T10:00:00Z",
                "draft": True,
                "requested_reviewers": [],
                "base": {"repo": {"full_name": "lu-bann/mobius"}},
            },
        ],
        "avg_pr_merge_seconds": 24 * 3600,
    }

    text = _render_report(inp, window, metrics)

    assert "*Weekly Dev Velocity - Fri May 29 (Beijing time)*" in text
    assert "Window: 2026-05-22 23:50 -> 2026-05-29 23:50 BJT" in text
    assert "Basis: week = window; cycle % = current Linear cycle." in text
    assert "Active cycle: Cycle 11" in text
    assert "- Parent issues created this cycle closed: *1 / 2* (50.0%)" in text
    assert "- Bug fixes complete: *1 / 2* (50.0%)" in text
    assert "- Active cycle issues closed: *2 / 4* (50.0%)" in text
    assert "- Not yet closed: *2 / 4* (50.0%)" in text
    assert "- Avg PR merge time: *1d* - merged this week" in text
    assert (
        "- Weekly throughput: *2* Linear closed / *1* created; *1* PRs merged / *1* opened"
        in text
    )
    assert "- Open PR backlog: *1* ready, *1* draft, *1* stale >= 7d" in text
    assert "Linear issues closed this week" not in text
    assert "Open parent issues created this cycle" not in text
    assert "Open bugs in active cycle" not in text
    assert "Mobius PRs merged this week" not in text
    assert "Mobius PRs opened this week" not in text
    assert "Mobius PRs open now" not in text
    assert "MOB-1" not in text
    assert "mobius#581" not in text


class _FakeWorkflowContext:
    def __init__(self) -> None:
        self.step_names: list[str] = []
        self.tool_calls: list[tuple[str, str, dict]] = []

    async def step(self, name, fn, **_kwargs):
        self.step_names.append(name)
        result = fn()
        if inspect.isawaitable(result):
            return await result
        return result

    async def call_tool(self, tool: str, method: str, args: dict | None = None):
        self.tool_calls.append((tool, method, args or {}))
        return {"ok": True, "channel": args.get("channel"), "ts": "123.456"}


@pytest.mark.asyncio
async def test_handler_posts_weekly_velocity_as_pris(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect_metrics(_inp: Input, _window: ReportWindow) -> dict:
        return {
            "issues_closed": [],
            "issues_created": [],
            "active_cycle_issues": [],
            "active_cycle_closed": [],
            "active_cycle_open": [],
            "active_bugs": [],
            "active_bugs_closed": [],
            "open_bugs": [],
            "parent_created_this_cycle": [],
            "parent_created_closed": [],
            "parent_created_open": [],
            "active_cycle_names": [],
            "prs_opened": [],
            "prs_merged": [],
            "outstanding_prs": [],
            "avg_pr_merge_seconds": None,
        }

    monkeypatch.setattr(dev_velocity_weekly, "_collect_metrics", fake_collect_metrics)
    ctx = _FakeWorkflowContext()

    await handler(  # type: ignore[arg-type]
        Input(slack_channel="pris-test", now="2026-05-29T23:50:00+08:00"),
        ctx,
    )

    assert ctx.step_names == ["collect_weekly_velocity_metrics", "post_weekly_velocity_to_slack"]
    assert ctx.tool_calls
    tool, method, args = ctx.tool_calls[0]
    assert (tool, method) == ("slack", "send_message")
    assert args["channel"] == "pris-test"
    assert args["username"] == "Pris"
    assert "Weekly Dev Velocity" in args["text"]
