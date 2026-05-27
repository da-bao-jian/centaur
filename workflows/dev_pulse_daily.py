"""Workflow: daily engineering pulse for #dev-pulse."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from centaur_sdk import secret

if TYPE_CHECKING:
    from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "dev_pulse_daily"

BEIJING_TZ = "Asia/Shanghai"
DEFAULT_SLACK_CHANNEL = "dev-pulse"
DEFAULT_SLACK_SENDER_NAME = "Pris"
DEFAULT_GITHUB_REPOS = ("lu-bann/mobius",)
BUG_LABELS = {"bug", "bugs", "defect", "regression"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
MAX_SECTION_ITEMS = 10
MAX_OUTSTANDING_PRS = 20
MAX_ACTIVE_CYCLES = 20
MAX_ACTIVE_CYCLE_ISSUES = 100


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSE_ENV_VALUES


def _csv_env(name: str, default: tuple[str, ...] = ()) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default)
    return [part.strip() for part in value.split(",") if part.strip()]


def _int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _json_dict_env(name: str) -> dict[str, str]:
    value = os.getenv(name)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(val) for key, val in parsed.items() if val}


SCHEDULE = {
    "schedule_id": "dev_pulse_daily_midnight_bjt",
    "cron": "0 0 * * 2-5",
    "timezone": BEIJING_TZ,
    "slack_channel": os.getenv("DEV_PULSE_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL),
    "enabled": _env_flag_enabled("DEV_PULSE_ENABLED", default=True),
    "catchup_policy": "skip",
    "input": {
        "slack_channel": os.getenv("DEV_PULSE_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL),
        "slack_sender_name": os.getenv("DEV_PULSE_SLACK_SENDER_NAME", DEFAULT_SLACK_SENDER_NAME),
        "schedule_label": "midnight_bjt",
    },
}


@dataclass
class Input:
    slack_channel: str = DEFAULT_SLACK_CHANNEL
    slack_sender_name: str = DEFAULT_SLACK_SENDER_NAME
    timezone: str = BEIJING_TZ
    lookback_hours: int = 24
    github_repos: list[str] = field(
        default_factory=lambda: _csv_env("DEV_PULSE_GITHUB_REPOS", DEFAULT_GITHUB_REPOS)
    )
    linear_team_keys: list[str] = field(
        default_factory=lambda: _csv_env("DEV_PULSE_LINEAR_TEAM_KEYS")
    )
    daily_non_bug_target: int | None = field(
        default_factory=lambda: _int_env("DEV_PULSE_DAILY_NON_BUG_TARGET")
    )
    reviewer_slack_mentions: dict[str, str] = field(
        default_factory=lambda: _json_dict_env("DEV_PULSE_REVIEWER_SLACK_MENTIONS")
    )
    now: str | None = None
    schedule_label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportWindow:
    start: dt.datetime
    end: dt.datetime
    timezone: str

    @property
    def start_local(self) -> dt.datetime:
        return self.start.astimezone(ZoneInfo(self.timezone))

    @property
    def end_local(self) -> dt.datetime:
        return self.end.astimezone(ZoneInfo(self.timezone))

    @property
    def report_date(self) -> dt.date:
        return (self.end_local - dt.timedelta(seconds=1)).date()


def _parse_now(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _report_window(inp: Input) -> ReportWindow:
    end = _parse_now(inp.now)
    hours = max(int(inp.lookback_hours or 24), 1)
    return ReportWindow(
        start=end - dt.timedelta(hours=hours),
        end=end,
        timezone=inp.timezone or BEIJING_TZ,
    )


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _within_window(value: str | None, window: ReportWindow) -> bool:
    parsed = _parse_datetime(value)
    return parsed is not None and window.start <= parsed < window.end


def _slack_link(url: str | None, label: str) -> str:
    cleaned_label = label.replace("|", "-")
    if not url:
        return cleaned_label
    return f"<{url}|{cleaned_label}>"


def _shorten(text: str, limit: int = 120) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}..."


def _labels(issue: dict[str, Any]) -> list[str]:
    raw_labels = issue.get("labels") or {}
    nodes = raw_labels.get("nodes") if isinstance(raw_labels, dict) else []
    return [str(label.get("name") or "").strip() for label in nodes if isinstance(label, dict)]


def _is_bug_issue(issue: dict[str, Any]) -> bool:
    names = {label.lower() for label in _labels(issue)}
    return bool(names & BUG_LABELS)


def _issue_assignee(issue: dict[str, Any]) -> str:
    assignee = issue.get("assignee")
    if isinstance(assignee, dict):
        return str(assignee.get("name") or "Unassigned")
    return "Unassigned"


def _issue_line(issue: dict[str, Any]) -> str:
    identifier = str(issue.get("identifier") or issue.get("id") or "issue")
    title = _shorten(str(issue.get("title") or "Untitled issue"))
    return f"- {_slack_link(issue.get('url'), identifier)} {title} - {_issue_assignee(issue)}"


def _repo_from_pr(pr: dict[str, Any]) -> str:
    base = pr.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    if full_name:
        return str(full_name)
    html_url = str(pr.get("html_url") or "")
    parsed = urlparse(html_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return str(pr.get("repo") or "")


def _pr_label(pr: dict[str, Any]) -> str:
    repo = _repo_from_pr(pr).split("/")[-1] or "repo"
    number = pr.get("number") or "?"
    return f"{repo}#{number}"


def _github_user(login: str) -> str:
    return f"@{login}" if login else "@unknown"


def _reviewer_mention(name: str, mapping: dict[str, str]) -> str:
    raw = mapping.get(name) or mapping.get(name.lower())
    if raw:
        return raw
    if name.startswith("team:"):
        return f"@{name.removeprefix('team:')}"
    return _github_user(name)


def _requested_reviewers(pr: dict[str, Any]) -> list[str]:
    reviewers: list[str] = []
    for user in pr.get("requested_reviewers") or []:
        if isinstance(user, dict) and user.get("login"):
            reviewers.append(str(user["login"]))
    for team in pr.get("requested_teams") or []:
        if isinstance(team, dict) and team.get("slug"):
            reviewers.append(f"team:{team['slug']}")
    return reviewers


def _age_days(pr: dict[str, Any], window: ReportWindow) -> int:
    created = _parse_datetime(str(pr.get("created_at") or ""))
    if created is None:
        return 0
    return max((window.end - created).days, 0)


def _pr_line(pr: dict[str, Any]) -> str:
    author = pr.get("user")
    author_login = author.get("login") if isinstance(author, dict) else ""
    title = _shorten(str(pr.get("title") or "Untitled PR"))
    return (
        f"- {_slack_link(pr.get('html_url'), _pr_label(pr))} {title} "
        f"- {_github_user(str(author_login or 'unknown'))}"
    )


def _outstanding_pr_line(
    pr: dict[str, Any],
    *,
    reviewer_mapping: dict[str, str],
    window: ReportWindow,
) -> str:
    reviewers = _requested_reviewers(pr)
    reviewer_text = ", ".join(_reviewer_mention(name, reviewer_mapping) for name in reviewers)
    review_status = f"{reviewer_text} requested" if reviewer_text else "no reviewer requested"
    draft_status = "draft" if pr.get("draft") else "ready"
    title = _shorten(str(pr.get("title") or "Untitled PR"), limit=100)
    return (
        f"- {_slack_link(pr.get('html_url'), _pr_label(pr))} {title} - "
        f"{draft_status}; {review_status}. Open {_age_days(pr, window)}d."
    )


def _render_limited_section(
    title: str,
    items: list[dict[str, Any]],
    formatter,
    *,
    empty: str = "None.",
    limit: int = MAX_SECTION_ITEMS,
) -> str:
    lines = [f"*{title} ({len(items)})*"]
    if not items:
        lines.append(empty)
        return "\n".join(lines)
    shown = items[:limit]
    lines.extend(formatter(item) for item in shown)
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more")
    return "\n".join(lines)


class LinearPulseClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url="https://api.linear.app/graphql",
            headers={
                "Authorization": secret("LINEAR_API_KEY"),
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._http.post("", json={"query": query, "variables": variables or {}})
        if response.status_code >= 400:
            raise RuntimeError(
                f"Linear API HTTP {response.status_code}: {_shorten(response.text, 500)}"
            )
        data = response.json()
        if data.get("errors"):
            message = data["errors"][0].get("message", str(data["errors"]))
            raise RuntimeError(f"Linear API error: {message}")
        return data.get("data") or {}

    async def issues_between(
        self,
        field: str,
        window: ReportWindow,
        *,
        team_keys: list[str],
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        seen: set[str] = set()
        teams = team_keys or [None]
        for team_key in teams:
            filters: dict[str, Any] = {field: {"gte": _iso(window.start), "lt": _iso(window.end)}}
            if team_key:
                filters["team"] = {"key": {"eq": team_key}}
            for issue in await self._issue_page(filters, limit=limit):
                issue_id = str(issue.get("id") or issue.get("identifier") or "")
                if issue_id and issue_id not in seen:
                    seen.add(issue_id)
                    issues.append(issue)
        return issues

    async def active_cycle_issues(
        self,
        *,
        team_keys: list[str],
        report_date: dt.date,
        limit: int = MAX_ACTIVE_CYCLES,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        cycles: list[dict[str, Any]] = []
        teams = team_keys or [None]
        for team_key in teams:
            cycles.extend(
                await self._cycles(
                    team_key=team_key,
                    report_date=report_date,
                    limit=limit,
                )
            )

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
            cycle_names.append(str(cycle.get("name") or f"Cycle {cycle.get('number')}"))
            raw_issues = cycle.get("issues") or {}
            nodes = raw_issues.get("nodes") if isinstance(raw_issues, dict) else []
            for issue in nodes or []:
                issue_id = str(issue.get("id") or issue.get("identifier") or "")
                if issue_id and issue_id not in seen:
                    seen.add(issue_id)
                    issues.append(issue)
        return issues, cycle_names

    async def _issue_page(self, filters: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        query = """
        query DevPulseIssues($first: Int!, $after: String, $filter: IssueFilter) {
          issues(first: $first, after: $after, filter: $filter, orderBy: updatedAt) {
            nodes {
              id
              identifier
              title
              url
              createdAt
              updatedAt
              completedAt
              state { id name type }
              assignee { id name }
              creator { id name }
              team { id name key }
              cycle { id name number }
              labels { nodes { id name } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        return await self._paginate_connection(
            query,
            {"filter": filters},
            connection="issues",
            limit=limit,
        )

    async def _cycles(
        self,
        *,
        team_key: str | None,
        report_date: dt.date,
        limit: int,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {
            "startsAt": {"lte": report_date.isoformat()},
            "endsAt": {"gte": report_date.isoformat()},
        }
        if team_key:
            filters["team"] = {"key": {"eq": team_key}}
        query = f"""
        query DevPulseCycles($first: Int!, $after: String, $filter: CycleFilter) {{
          cycles(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {{
            nodes {{
              id
              name
              number
              startsAt
              endsAt
              team {{ id name key }}
              issues(first: {MAX_ACTIVE_CYCLE_ISSUES}) {{
                nodes {{
                  id
                  identifier
                  title
                  completedAt
                  state {{ id name type }}
                  labels {{ nodes {{ id name }} }}
                }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        return await self._paginate_connection(
            query,
            {"filter": filters},
            connection="cycles",
            limit=limit,
        )

    async def _paginate_connection(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        connection: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while len(nodes) < limit:
            page_size = min(100, limit - len(nodes))
            data = await self._query(
                query,
                {
                    **variables,
                    "first": page_size,
                    "after": after,
                },
            )
            page = data.get(connection) or {}
            page_nodes = page.get("nodes") or []
            nodes.extend(page_nodes)
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        return nodes


class GitHubPulseClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {secret('GITHUB_TOKEN')}",
            },
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def pull_requests_for_repo(
        self,
        repo: str,
        *,
        state: str,
        sort: str,
        direction: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        owner_repo = repo.strip().strip("/")
        if "/" not in owner_repo:
            return []
        prs: list[dict[str, Any]] = []
        page = 1
        while len(prs) < limit:
            response = await self._http.get(
                f"/repos/{owner_repo}/pulls",
                params={
                    "state": state,
                    "sort": sort,
                    "direction": direction,
                    "per_page": min(100, limit - len(prs)),
                    "page": page,
                },
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            for pr in batch:
                if isinstance(pr, dict):
                    prs.append(pr)
            page += 1
        return prs


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
        active_cycle_issues, cycle_names = await client.active_cycle_issues(
            team_keys=inp.linear_team_keys,
            report_date=window.report_date,
        )
    finally:
        await client.close()

    non_bug_closed = [issue for issue in closed if not _is_bug_issue(issue)]
    active_non_bug = [issue for issue in active_cycle_issues if not _is_bug_issue(issue)]

    if inp.daily_non_bug_target is not None:
        target = inp.daily_non_bug_target
        target_source = "configured daily target"
    elif active_non_bug:
        target = len(active_non_bug)
        target_source = "active Linear cycle target"
    else:
        target = 0
        target_source = "not configured"

    return {
        "issues_created": created,
        "issues_closed": closed,
        "non_bug_completed": len(non_bug_closed),
        "completion_target": target,
        "completion_target_source": target_source,
        "active_cycle_names": cycle_names,
    }


async def _collect_github(inp: Input, window: ReportWindow) -> dict[str, Any]:
    client = GitHubPulseClient()
    opened: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    outstanding: list[dict[str, Any]] = []
    try:
        for repo in inp.github_repos:
            all_recent = await client.pull_requests_for_repo(
                repo,
                state="all",
                sort="created",
                direction="desc",
            )
            opened.extend(pr for pr in all_recent if _within_window(pr.get("created_at"), window))

            closed_recent = await client.pull_requests_for_repo(
                repo,
                state="closed",
                sort="updated",
                direction="desc",
            )
            closed.extend(pr for pr in closed_recent if _within_window(pr.get("closed_at"), window))

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
        "prs_closed": closed,
        "outstanding_prs": outstanding[:MAX_OUTSTANDING_PRS],
    }


async def _collect_metrics(inp: Input, window: ReportWindow) -> dict[str, Any]:
    linear = await _collect_linear(inp, window)
    github = await _collect_github(inp, window)
    return {**linear, **github}


def _completion_section(metrics: dict[str, Any]) -> str:
    completed = int(metrics.get("non_bug_completed") or 0)
    target = int(metrics.get("completion_target") or 0)
    source = str(metrics.get("completion_target_source") or "not configured")
    cycle_names = [name for name in metrics.get("active_cycle_names") or [] if name]
    if target > 0:
        pct = completed / target * 100
        line = f"Non-bug issues completed: *{completed} / {target}* ({pct:.1f}%)"
    else:
        line = f"Non-bug issues completed: *{completed}* (target not configured)"
    lines = ["*Completion rate*", line, f"Target source: {source}"]
    if cycle_names:
        lines.append(f"Active cycle: {', '.join(cycle_names[:3])}")
    return "\n".join(lines)


def _render_report(inp: Input, window: ReportWindow, metrics: dict[str, Any]) -> str:
    start = window.start_local.strftime("%Y-%m-%d %H:%M")
    end = window.end_local.strftime("%Y-%m-%d %H:%M")
    title_date = (
        f"{window.report_date.strftime('%a')} "
        f"{window.report_date.strftime('%b')} {window.report_date.day}"
    )
    lines = [
        f"*Dev Pulse EOD - {title_date} (Beijing time)*",
        f"Window: {start} -> {end} BJT",
        "",
        _completion_section(metrics),
        "",
        _render_limited_section(
            "Issues closed",
            metrics.get("issues_closed") or [],
            _issue_line,
        ),
        "",
        _render_limited_section(
            "Issues created",
            metrics.get("issues_created") or [],
            _issue_line,
        ),
        "",
        _render_limited_section(
            "PRs opened since last Dev Pulse",
            metrics.get("prs_opened") or [],
            _pr_line,
        ),
        "",
        _render_limited_section(
            "PRs closed since last Dev Pulse",
            metrics.get("prs_closed") or [],
            _pr_line,
        ),
        "",
        _render_limited_section(
            "Outstanding open PRs - all currently open",
            metrics.get("outstanding_prs") or [],
            lambda pr: _outstanding_pr_line(
                pr,
                reviewer_mapping=inp.reviewer_slack_mentions,
                window=window,
            ),
            empty="None.",
            limit=MAX_OUTSTANDING_PRS,
        ),
    ]
    return "\n".join(lines).strip()


async def _post_report_to_slack(inp: Input, ctx: WorkflowContext, text: str) -> dict[str, Any]:
    return await ctx.call_tool(
        "slack",
        "send_message",
        {
            "channel": inp.slack_channel.strip().lstrip("#") or DEFAULT_SLACK_CHANNEL,
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
        "collect_dev_pulse_metrics",
        lambda: _collect_metrics(inp, window),
    )
    text = _render_report(inp, window, metrics)
    await ctx.step(
        "post_dev_pulse_to_slack",
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
            "prs_opened": len(metrics.get("prs_opened") or []),
            "prs_closed": len(metrics.get("prs_closed") or []),
            "outstanding_prs": len(metrics.get("outstanding_prs") or []),
            "non_bug_completed": int(metrics.get("non_bug_completed") or 0),
            "completion_target": int(metrics.get("completion_target") or 0),
        },
    }
