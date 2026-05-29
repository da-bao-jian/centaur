"""Workflow: weekly engineering velocity summary."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from workflows.dev_pulse_daily import (
    BEIJING_TZ,
    DEFAULT_GITHUB_REPOS,
    DEFAULT_SLACK_CHANNEL,
    DEFAULT_SLACK_SENDER_NAME,
    MAX_ACTIVE_CYCLE_ISSUES,
    MAX_ACTIVE_CYCLES,
    MAX_OUTSTANDING_PRS,
    GitHubPulseClient,
    LinearPulseClient,
    ReportWindow,
    _age_days,
    _csv_env,
    _env_flag_enabled,
    _int_env,
    _is_bug_issue,
    _iso,
    _json_dict_env,
    _parse_date,
    _parse_datetime,
    _parse_now,
    _within_window,
)

if TYPE_CHECKING:
    from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "dev_velocity_weekly"

DEFAULT_WEEKLY_LOOKBACK_HOURS = 168
DEFAULT_WEEKLY_SLACK_CHANNEL = os.getenv(
    "DEV_VELOCITY_WEEKLY_SLACK_CHANNEL",
    os.getenv("DEV_PULSE_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL),
)
STALE_PR_DAYS = 7


def _weekly_enabled() -> bool:
    default = _env_flag_enabled("DEV_PULSE_ENABLED", default=True)
    return _env_flag_enabled("DEV_VELOCITY_WEEKLY_ENABLED", default=default)


def _weekly_github_repos() -> list[str]:
    configured = _csv_env("DEV_VELOCITY_WEEKLY_GITHUB_REPOS")
    if configured:
        return configured
    return _csv_env("DEV_PULSE_GITHUB_REPOS", DEFAULT_GITHUB_REPOS)


def _weekly_linear_team_keys() -> list[str]:
    configured = _csv_env("DEV_VELOCITY_WEEKLY_LINEAR_TEAM_KEYS")
    if configured:
        return configured
    return _csv_env("DEV_PULSE_LINEAR_TEAM_KEYS")


def _weekly_reviewer_mentions() -> dict[str, str]:
    configured = _json_dict_env("DEV_VELOCITY_WEEKLY_REVIEWER_SLACK_MENTIONS")
    if configured:
        return configured
    return _json_dict_env("DEV_PULSE_REVIEWER_SLACK_MENTIONS")


SCHEDULE = {
    "schedule_id": "dev_velocity_weekly_sunday_bjt",
    "cron": "59 23 * * 0",
    "timezone": BEIJING_TZ,
    "slack_channel": DEFAULT_WEEKLY_SLACK_CHANNEL,
    "enabled": _weekly_enabled(),
    "catchup_policy": "skip",
    "input": {
        "slack_channel": DEFAULT_WEEKLY_SLACK_CHANNEL,
        "slack_sender_name": os.getenv(
            "DEV_VELOCITY_WEEKLY_SLACK_SENDER_NAME",
            os.getenv("DEV_PULSE_SLACK_SENDER_NAME", DEFAULT_SLACK_SENDER_NAME),
        ),
        "lookback_hours": _int_env(
            "DEV_VELOCITY_WEEKLY_LOOKBACK_HOURS",
            DEFAULT_WEEKLY_LOOKBACK_HOURS,
        ),
        "schedule_label": "sunday_2359_bjt",
    },
}


@dataclass
class Input:
    slack_channel: str = DEFAULT_WEEKLY_SLACK_CHANNEL
    slack_sender_name: str = DEFAULT_SLACK_SENDER_NAME
    timezone: str = BEIJING_TZ
    lookback_hours: int = DEFAULT_WEEKLY_LOOKBACK_HOURS
    github_repos: list[str] = field(default_factory=_weekly_github_repos)
    linear_team_keys: list[str] = field(default_factory=_weekly_linear_team_keys)
    reviewer_slack_mentions: dict[str, str] = field(default_factory=_weekly_reviewer_mentions)
    now: str | None = None
    schedule_label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _report_window(inp: Input) -> ReportWindow:
    end = _parse_now(inp.now)
    hours = max(int(inp.lookback_hours or DEFAULT_WEEKLY_LOOKBACK_HOURS), 1)
    return ReportWindow(
        start=end - dt.timedelta(hours=hours),
        end=end,
        timezone=inp.timezone or BEIJING_TZ,
    )


def _completed(issue: dict[str, Any]) -> bool:
    state = issue.get("state")
    state_type = state.get("type") if isinstance(state, dict) else None
    return state_type == "completed" or bool(issue.get("completedAt"))


def _issue_sort_value(issue: dict[str, Any], field: str) -> dt.datetime:
    return _parse_datetime(str(issue.get(field) or "")) or dt.datetime.min.replace(
        tzinfo=dt.timezone.utc
    )


def _closed_recent_first(issue: dict[str, Any]) -> dt.datetime:
    return _issue_sort_value(issue, "completedAt")


def _created_recent_first(issue: dict[str, Any]) -> dt.datetime:
    return _issue_sort_value(issue, "createdAt")


def _is_parent_issue(issue: dict[str, Any]) -> bool:
    parent = issue.get("parent")
    return not isinstance(parent, dict) or not parent.get("id")


def _created_in_active_cycle(issue: dict[str, Any], window: ReportWindow) -> bool:
    created = _parse_datetime(str(issue.get("createdAt") or ""))
    if created is None or created >= window.end:
        return False
    starts_at = _parse_date(str(issue.get("_active_cycle_starts_at") or ""))
    ends_at = _parse_date(str(issue.get("_active_cycle_ends_at") or ""))
    created_date = created.astimezone(ZoneInfo(window.timezone)).date()
    if starts_at and created_date < starts_at:
        return False
    if ends_at and created_date > ends_at:
        return False
    return True


def _percentage(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "n/a"
    return f"{numerator / denominator * 100:.1f}%"


def _ratio_metric(numerator: int, denominator: int) -> str:
    return f"*{numerator} / {denominator}* ({_percentage(numerator, denominator)})"


def _duration_seconds(pr: dict[str, Any]) -> int | None:
    created = _parse_datetime(str(pr.get("created_at") or ""))
    merged = _parse_datetime(str(pr.get("merged_at") or ""))
    if created is None or merged is None or merged < created:
        return None
    return int((merged - created).total_seconds())


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/a"
    total_minutes = max(int(round(float(seconds) / 60)), 0)
    if total_minutes < 60:
        return f"{total_minutes}m"
    total_hours = total_minutes // 60
    minutes = total_minutes % 60
    if total_hours < 24:
        return f"{total_hours}h {minutes}m" if minutes else f"{total_hours}h"
    days = total_hours // 24
    hours = total_hours % 24
    return f"{days}d {hours}h" if hours else f"{days}d"


def _average_merge_seconds(prs: list[dict[str, Any]]) -> int | None:
    durations = [duration for pr in prs if (duration := _duration_seconds(pr)) is not None]
    if not durations:
        return None
    return int(sum(durations) / len(durations))


def _cycle_name(cycle: dict[str, Any]) -> str:
    return str(cycle.get("name") or f"Cycle {cycle.get('number')}")


def _active_cycle_issues_from_cycles(
    cycles: list[dict[str, Any]],
    *,
    report_date: dt.date,
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[dict[str, Any]] = []
    cycle_names: list[str] = []
    seen: set[str] = set()
    for cycle in cycles:
        starts_at = _parse_date(str(cycle.get("startsAt") or ""))
        ends_at = _parse_date(str(cycle.get("endsAt") or ""))
        if starts_at and starts_at > report_date:
            continue
        if ends_at and ends_at < report_date:
            continue

        cycle_name = _cycle_name(cycle)
        cycle_names.append(cycle_name)
        raw_issues = cycle.get("issues") or {}
        nodes = raw_issues.get("nodes") if isinstance(raw_issues, dict) else []
        for raw_issue in nodes or []:
            if not isinstance(raw_issue, dict):
                continue
            issue_id = str(raw_issue.get("id") or raw_issue.get("identifier") or "")
            if issue_id and issue_id in seen:
                continue
            if issue_id:
                seen.add(issue_id)
            issue = dict(raw_issue)
            issue["_active_cycle_name"] = cycle_name
            issue["_active_cycle_starts_at"] = starts_at.isoformat() if starts_at else None
            issue["_active_cycle_ends_at"] = ends_at.isoformat() if ends_at else None
            issues.append(issue)
    return issues, cycle_names


async def _active_cycles(
    client: LinearPulseClient,
    inp: Input,
    window: ReportWindow,
) -> list[dict[str, Any]]:
    cycles: list[dict[str, Any]] = []
    teams = inp.linear_team_keys or [None]
    query = """
    query WeeklyVelocityCycles($first: Int!, $after: String, $filter: CycleFilter) {
      cycles(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {
        nodes {
          id
          name
          number
          startsAt
          endsAt
          team { id name key }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    for team_key in teams:
        filters: dict[str, Any] = {
            "startsAt": {"lte": window.report_date.isoformat()},
            "endsAt": {"gte": window.report_date.isoformat()},
        }
        if team_key:
            filters["team"] = {"key": {"eq": team_key}}
        cycles.extend(
            await client._paginate_connection(
                query,
                {"filter": filters},
                connection="cycles",
                limit=MAX_ACTIVE_CYCLES,
            )
        )
    return cycles


async def _active_cycle_issue_rows(
    client: LinearPulseClient,
    cycles: list[dict[str, Any]],
    *,
    report_date: dt.date,
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[dict[str, Any]] = []
    cycle_names: list[str] = []
    seen: set[str] = set()
    for cycle in cycles:
        starts_at = _parse_date(str(cycle.get("startsAt") or ""))
        ends_at = _parse_date(str(cycle.get("endsAt") or ""))
        if starts_at and starts_at > report_date:
            continue
        if ends_at and ends_at < report_date:
            continue

        cycle_name = _cycle_name(cycle)
        cycle_names.append(cycle_name)
        cycle_id = str(cycle.get("id") or "")
        if not cycle_id:
            continue

        for raw_issue in await client._issue_page(
            {"cycle": {"id": {"eq": cycle_id}}},
            limit=MAX_ACTIVE_CYCLE_ISSUES,
        ):
            issue_id = str(raw_issue.get("id") or raw_issue.get("identifier") or "")
            if issue_id and issue_id in seen:
                continue
            if issue_id:
                seen.add(issue_id)
            issue = dict(raw_issue)
            issue["_active_cycle_name"] = cycle_name
            issue["_active_cycle_starts_at"] = starts_at.isoformat() if starts_at else None
            issue["_active_cycle_ends_at"] = ends_at.isoformat() if ends_at else None
            issues.append(issue)
    return issues, cycle_names


async def _collect_linear(inp: Input, window: ReportWindow) -> dict[str, Any]:
    client = LinearPulseClient()
    try:
        created = await client.issues_between(
            "createdAt",
            window,
            team_keys=inp.linear_team_keys,
        )
        closed = await client.issues_between(
            "completedAt",
            window,
            team_keys=inp.linear_team_keys,
        )
        active_cycle_issues, cycle_names = await _active_cycle_issue_rows(
            client,
            await _active_cycles(client, inp, window),
            report_date=window.report_date,
        )
    finally:
        await client.close()

    active_closed = [issue for issue in active_cycle_issues if _completed(issue)]
    active_open = [issue for issue in active_cycle_issues if not _completed(issue)]
    active_bugs = [issue for issue in active_cycle_issues if _is_bug_issue(issue)]
    active_bugs_closed = [issue for issue in active_bugs if _completed(issue)]
    open_bugs = [issue for issue in active_bugs if not _completed(issue)]
    parent_created_this_cycle = [
        issue
        for issue in active_cycle_issues
        if _is_parent_issue(issue) and _created_in_active_cycle(issue, window)
    ]
    parent_created_closed = [issue for issue in parent_created_this_cycle if _completed(issue)]
    parent_created_open = [issue for issue in parent_created_this_cycle if not _completed(issue)]

    return {
        "issues_created": sorted(created, key=_created_recent_first, reverse=True),
        "issues_closed": sorted(closed, key=_closed_recent_first, reverse=True),
        "active_cycle_issues": active_cycle_issues,
        "active_cycle_closed": active_closed,
        "active_cycle_open": active_open,
        "active_bugs": active_bugs,
        "active_bugs_closed": active_bugs_closed,
        "open_bugs": sorted(open_bugs, key=_created_recent_first, reverse=True),
        "parent_created_this_cycle": parent_created_this_cycle,
        "parent_created_closed": parent_created_closed,
        "parent_created_open": sorted(parent_created_open, key=_created_recent_first, reverse=True),
        "active_cycle_names": cycle_names,
    }


async def _collect_github(inp: Input, window: ReportWindow) -> dict[str, Any]:
    client = GitHubPulseClient()
    opened: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    outstanding: list[dict[str, Any]] = []
    try:
        for repo in inp.github_repos:
            recent = await client.pull_requests_for_repo(
                repo,
                state="all",
                sort="created",
                direction="desc",
            )
            opened.extend(pr for pr in recent if _within_window(pr.get("created_at"), window))

            closed_recent = await client.pull_requests_for_repo(
                repo,
                state="closed",
                sort="updated",
                direction="desc",
            )
            merged.extend(
                pr
                for pr in closed_recent
                if pr.get("merged_at") and _within_window(pr.get("merged_at"), window)
            )

            open_prs = await client.pull_requests_for_repo(
                repo,
                state="open",
                sort="updated",
                direction="desc",
                limit=200,
            )
            outstanding.extend(open_prs)
    finally:
        await client.close()

    return {
        "prs_opened": opened,
        "prs_merged": merged,
        "outstanding_prs": outstanding[:MAX_OUTSTANDING_PRS],
        "avg_pr_merge_seconds": _average_merge_seconds(merged),
    }


async def _collect_metrics(inp: Input, window: ReportWindow) -> dict[str, Any]:
    linear = await _collect_linear(inp, window)
    github = await _collect_github(inp, window)
    return {**linear, **github}


def _scorecard(metrics: dict[str, Any], window: ReportWindow) -> str:
    parent_total = len(metrics.get("parent_created_this_cycle") or [])
    parent_closed = len(metrics.get("parent_created_closed") or [])
    bug_total = len(metrics.get("active_bugs") or [])
    bug_closed = len(metrics.get("active_bugs_closed") or [])
    active_total = len(metrics.get("active_cycle_issues") or [])
    active_closed = len(metrics.get("active_cycle_closed") or [])
    active_open = len(metrics.get("active_cycle_open") or [])
    issues_closed = len(metrics.get("issues_closed") or [])
    issues_created = len(metrics.get("issues_created") or [])
    prs_merged = len(metrics.get("prs_merged") or [])
    prs_opened = len(metrics.get("prs_opened") or [])
    outstanding = metrics.get("outstanding_prs") or []
    ready = len([pr for pr in outstanding if not pr.get("draft")])
    draft = len([pr for pr in outstanding if pr.get("draft")])
    stale = len([pr for pr in outstanding if _age_days(pr, window) >= STALE_PR_DAYS])

    return "\n".join(
        [
            "*Velocity scorecard*",
            (
                "- Parent issues created this cycle closed: "
                f"{_ratio_metric(parent_closed, parent_total)} - active cycle parents"
            ),
            f"- Bug fixes complete: {_ratio_metric(bug_closed, bug_total)} - active cycle bugs",
            f"- Active cycle issues closed: {_ratio_metric(active_closed, active_total)}",
            f"- Not yet closed: {_ratio_metric(active_open, active_total)}",
            (
                "- Avg PR merge time: "
                f"*{_format_duration(metrics.get('avg_pr_merge_seconds'))}* - merged this week"
            ),
            (
                f"- Weekly throughput: *{issues_closed}* Linear closed / "
                f"*{issues_created}* created; "
                f"*{prs_merged}* PRs merged / *{prs_opened}* opened"
            ),
            (
                f"- Open PR backlog: *{ready}* ready, *{draft}* draft, "
                f"*{stale}* stale >= {STALE_PR_DAYS}d"
            ),
        ]
    )


def _render_report(_inp: Input, window: ReportWindow, metrics: dict[str, Any]) -> str:
    start = window.start_local.strftime("%Y-%m-%d %H:%M")
    end = window.end_local.strftime("%Y-%m-%d %H:%M")
    title_date = (
        f"{window.report_date.strftime('%a')} "
        f"{window.report_date.strftime('%b')} {window.report_date.day}"
    )
    cycle_names = [name for name in metrics.get("active_cycle_names") or [] if name]
    cycle_line = (
        f"Active cycle: {', '.join(cycle_names[:3])}" if cycle_names else "Active cycle: none"
    )

    lines = [
        f"*Weekly Dev Velocity - {title_date} (Beijing time)*",
        f"Window: {start} -> {end} BJT",
        "Basis: week = window; cycle % = current Linear cycle.",
        cycle_line,
        "",
        _scorecard(metrics, window),
    ]
    return "\n".join(lines).strip()


async def _post_report_to_slack(inp: Input, ctx: WorkflowContext, text: str) -> dict[str, Any]:
    return await ctx.call_tool(
        "slack",
        "send_message",
        {
            "channel": inp.slack_channel.strip().lstrip("#") or DEFAULT_WEEKLY_SLACK_CHANNEL,
            "text": text,
            "no_attribution": True,
            "unfurl_links": False,
            "unfurl_media": False,
            "username": inp.slack_sender_name.strip() or DEFAULT_SLACK_SENDER_NAME,
        },
    )


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    window = _report_window(inp)
    metrics = await ctx.step(
        "collect_weekly_velocity_metrics",
        lambda: _collect_metrics(inp, window),
    )
    text = _render_report(inp, window, metrics)
    await ctx.step(
        "post_weekly_velocity_to_slack",
        lambda: _post_report_to_slack(inp, ctx, text),
    )
    return {
        "window_start": _iso(window.start),
        "window_end": _iso(window.end),
        "slack_channel": inp.slack_channel,
        "slack_text": text,
        "counts": {
            "issues_closed": len(metrics.get("issues_closed") or []),
            "issues_created": len(metrics.get("issues_created") or []),
            "active_cycle_issues": len(metrics.get("active_cycle_issues") or []),
            "active_cycle_closed": len(metrics.get("active_cycle_closed") or []),
            "active_cycle_open": len(metrics.get("active_cycle_open") or []),
            "active_bugs": len(metrics.get("active_bugs") or []),
            "active_bugs_closed": len(metrics.get("active_bugs_closed") or []),
            "parent_created_this_cycle": len(metrics.get("parent_created_this_cycle") or []),
            "parent_created_closed": len(metrics.get("parent_created_closed") or []),
            "prs_opened": len(metrics.get("prs_opened") or []),
            "prs_merged": len(metrics.get("prs_merged") or []),
            "outstanding_prs": len(metrics.get("outstanding_prs") or []),
            "avg_pr_merge_seconds": metrics.get("avg_pr_merge_seconds"),
        },
    }
