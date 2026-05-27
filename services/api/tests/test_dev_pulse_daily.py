import datetime as dt
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workflows import dev_pulse_daily  # noqa: E402
from workflows import dev_pulse_friday_eod  # noqa: E402
from workflows.dev_pulse_daily import (  # noqa: E402
    Input,
    LinearPulseClient,
    ReportWindow,
    handler,
    _is_bug_issue,
    _render_report,
    _report_window,
)


def test_dev_pulse_schedule_runs_tuesday_to_friday_midnight_beijing() -> None:
    assert dev_pulse_daily.SCHEDULE["cron"] == "0 0 * * 2-5"
    assert dev_pulse_daily.SCHEDULE["timezone"] == "Asia/Shanghai"
    assert dev_pulse_daily.SCHEDULE["slack_channel"] == "dev-pulse"
    assert dev_pulse_daily.SCHEDULE["input"]["slack_sender_name"] == "Pris"
    assert dev_pulse_daily.DEFAULT_GITHUB_REPOS == ("lu-bann/mobius",)


def test_friday_alias_runs_at_2359_beijing() -> None:
    assert dev_pulse_friday_eod.WORKFLOW_NAME == "dev_pulse_friday_eod"
    assert dev_pulse_friday_eod.SCHEDULE["cron"] == "59 23 * * 5"
    assert dev_pulse_friday_eod.SCHEDULE["timezone"] == "Asia/Shanghai"
    assert dev_pulse_friday_eod.SCHEDULE["input"]["slack_sender_name"] == "Pris"


def test_report_window_uses_previous_24_hours_from_beijing_eod() -> None:
    inp = Input(now="2026-05-26T00:00:00+08:00")

    window = _report_window(inp)

    assert window.end == dt.datetime(2026, 5, 25, 16, 0, tzinfo=dt.timezone.utc)
    assert window.start == dt.datetime(2026, 5, 24, 16, 0, tzinfo=dt.timezone.utc)
    assert window.start_local.strftime("%Y-%m-%d %H:%M") == "2026-05-25 00:00"
    assert window.end_local.strftime("%Y-%m-%d %H:%M") == "2026-05-26 00:00"
    assert window.report_date == dt.date(2026, 5, 25)


def test_bug_issue_detection_uses_labels() -> None:
    bug = {"labels": {"nodes": [{"name": "Bug"}]}}
    feature = {"labels": {"nodes": [{"name": "feature"}]}}

    assert _is_bug_issue(bug) is True
    assert _is_bug_issue(feature) is False


@pytest.mark.asyncio
async def test_active_cycle_query_uses_server_side_date_filter() -> None:
    class CaptureLinearClient(LinearPulseClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, str, int]] = []

        async def _paginate_connection(self, query, variables, *, connection, limit):
            self.calls.append((query, variables, connection, limit))
            return []

    client = CaptureLinearClient()

    await client._cycles(team_key="MOB", report_date=dt.date(2026, 5, 24), limit=20)

    assert len(client.calls) == 1
    query, variables, connection, limit = client.calls[0]
    assert connection == "cycles"
    assert limit == 20
    assert "$filter: CycleFilter" in query
    assert "issues(first: 100)" in query
    assert variables == {
        "filter": {
            "startsAt": {"lte": "2026-05-24"},
            "endsAt": {"gte": "2026-05-24"},
            "team": {"key": {"eq": "MOB"}},
        }
    }


def test_render_report_keeps_outstanding_prs_unwindowed_and_mentions_reviewers() -> None:
    inp = Input(
        now="2026-05-29T23:59:00+08:00",
        reviewer_slack_mentions={"alice": "<@U123>", "team:infra": "<!subteam^S123|infra>"},
    )
    window = ReportWindow(
        start=dt.datetime(2026, 5, 28, 15, 59, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 5, 29, 15, 59, tzinfo=dt.timezone.utc),
        timezone="Asia/Shanghai",
    )
    metrics = {
        "issues_closed": [
            {
                "identifier": "ENG-1",
                "title": "Ship account settings",
                "url": "https://linear.app/acme/issue/ENG-1",
                "assignee": {"name": "Harry"},
            }
        ],
        "issues_created": [],
        "prs_opened": [
            {
                "number": 40,
                "title": "Open fresh PR",
                "html_url": "https://github.com/acme/centaur/pull/40",
                "user": {"login": "charlie"},
                "base": {"repo": {"full_name": "acme/centaur"}},
            }
        ],
        "prs_closed": [
            {
                "number": 41,
                "title": "Close finished PR",
                "html_url": "https://github.com/acme/centaur/pull/41",
                "user": {"login": "dana"},
                "base": {"repo": {"full_name": "acme/centaur"}},
            }
        ],
        "outstanding_prs": [
            {
                "number": 43,
                "title": "Ready without explicit reviewer",
                "html_url": "https://github.com/acme/centaur/pull/43",
                "created_at": "2026-05-29T12:00:00Z",
                "requested_reviewers": [],
                "requested_teams": [],
                "user": {"login": "eve"},
                "base": {"repo": {"full_name": "acme/centaur"}},
            },
            {
                "number": 42,
                "title": "Add deploy workflow",
                "html_url": "https://github.com/acme/centaur/pull/42",
                "created_at": "2026-05-20T12:00:00Z",
                "requested_reviewers": [{"login": "alice"}],
                "requested_teams": [{"slug": "infra"}],
                "user": {"login": "bob"},
                "base": {"repo": {"full_name": "acme/centaur"}},
            },
            {
                "number": 39,
                "title": "Draft integration",
                "html_url": "https://github.com/acme/centaur/pull/39",
                "created_at": "2026-05-19T12:00:00Z",
                "draft": True,
                "requested_reviewers": [],
                "requested_teams": [],
                "user": {"login": "frank"},
                "base": {"repo": {"full_name": "acme/centaur"}},
            },
        ],
        "non_bug_completed": 1,
        "completion_target": 4,
        "completion_target_source": "configured daily target",
        "active_cycle_names": ["Cycle 12"],
    }

    text = _render_report(inp, window, metrics)

    assert "Window: 2026-05-28 23:59 -> 2026-05-29 23:59 BJT" in text
    assert "*Completion rate*" in text
    assert "Non-bug issues completed: *1 / 4* (25.0%)" in text
    assert "*Issues closed (1)*" in text
    assert "<https://linear.app/acme/issue/ENG-1|ENG-1> Ship account settings" in text
    assert "*PRs opened since last Dev Pulse (1)*" in text
    assert "- <https://github.com/acme/centaur/pull/40|centaur#40> Open fresh PR" in text
    assert "  Author: @charlie" in text
    assert "*PRs closed since last Dev Pulse (1)*" in text
    assert "- <https://github.com/acme/centaur/pull/41|centaur#41> Close finished PR" in text
    assert "  Author: @dana" in text
    assert "*Outstanding open PRs - all currently open (3)*" in text
    assert "- <https://github.com/acme/centaur/pull/43|centaur#43> Ready without explicit reviewer" in text
    assert "  Status: ready | Open: 0d | Reviewers: none" in text
    assert "- <https://github.com/acme/centaur/pull/42|centaur#42> Add deploy workflow" in text
    assert "  Status: ready | Open: 9d | Reviewers: <@U123>, <!subteam^S123|infra>" in text
    assert "- <https://github.com/acme/centaur/pull/39|centaur#39> Draft integration" in text
    assert "  Status: draft | Open: 10d | Reviewers: none" in text


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
        return {"ok": True}


@pytest.mark.asyncio
async def test_handler_posts_as_pris(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_collect_metrics(_inp: Input, _window: ReportWindow) -> dict:
        return {
            "issues_closed": [],
            "issues_created": [],
            "prs_opened": [],
            "prs_closed": [],
            "outstanding_prs": [],
            "non_bug_completed": 0,
            "completion_target": 0,
            "completion_target_source": "not configured",
            "active_cycle_names": [],
        }

    monkeypatch.setattr(dev_pulse_daily, "_collect_metrics", fake_collect_metrics)
    ctx = _FakeWorkflowContext()

    await handler(Input(now="2026-05-26T00:00:00+08:00"), ctx)  # type: ignore[arg-type]

    assert ctx.step_names == ["collect_dev_pulse_metrics", "post_dev_pulse_to_slack"]
    assert ctx.tool_calls
    tool, method, args = ctx.tool_calls[0]
    assert (tool, method) == ("slack", "send_message")
    assert args["username"] == "Pris"
