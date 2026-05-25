from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any


OPS_WINDOW_HOURS = int(os.getenv("OPS_WINDOW_HOURS", "24"))
EXECUTION_QUEUE_WARNING_S = int(os.getenv("OPS_EXECUTION_QUEUE_WARNING_S", "120"))
EXECUTION_QUEUE_CRITICAL_S = int(os.getenv("OPS_EXECUTION_QUEUE_CRITICAL_S", "600"))
EXECUTION_RUNNING_CRITICAL_S = int(os.getenv("OPS_EXECUTION_RUNNING_CRITICAL_S", "1800"))
WORKFLOW_RUNNING_CRITICAL_S = int(os.getenv("OPS_WORKFLOW_RUNNING_CRITICAL_S", "1800"))
WORKFLOW_OVERDUE_WARNING_S = int(os.getenv("OPS_WORKFLOW_OVERDUE_WARNING_S", "120"))
WORKFLOW_OVERDUE_CRITICAL_S = int(os.getenv("OPS_WORKFLOW_OVERDUE_CRITICAL_S", "600"))
DELIVERY_WARNING_S = int(os.getenv("OPS_DELIVERY_WARNING_S", "120"))
DELIVERY_CRITICAL_S = int(os.getenv("OPS_DELIVERY_CRITICAL_S", "600"))
SCHEDULE_WARNING_S = int(os.getenv("OPS_SCHEDULE_WARNING_S", "300"))
SCHEDULE_CRITICAL_S = int(os.getenv("OPS_SCHEDULE_CRITICAL_S", "900"))

DEV_PULSE_WORKFLOWS = ("dev_pulse_daily", "dev_pulse_friday_eod")

_SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _status_from_bool(
    critical: bool,
    warning: bool = False,
) -> str:
    if critical:
        return "critical"
    if warning:
        return "warning"
    return "ok"


def _rollup_status(items: list[dict[str, Any]]) -> str:
    status = "ok"
    for item in items:
        candidate = str(item.get("status") or "ok")
        if _SEVERITY_RANK.get(candidate, 0) > _SEVERITY_RANK[status]:
            status = candidate
    return status


def _monitor(
    monitor_id: str,
    status: str,
    title: str,
    summary: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": monitor_id,
        "status": status,
        "title": title,
        "summary": summary,
        "evidence": evidence or {},
    }


async def _status_counts(pool, table: str, window_hours: int) -> dict[str, int]:
    rows = await pool.fetch(
        f"""
        SELECT status, COUNT(*)::int AS count
        FROM {table}
        WHERE created_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
        GROUP BY status
        ORDER BY status
        """,
        float(window_hours),
    )
    return {str(row["status"]): int(row["count"]) for row in rows}


async def _fetch_recent_workflow_failures(pool, window_hours: int, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        WITH recent_failures AS (
            SELECT run_id, workflow_name, thread_key, status, error_text, input_json,
                   created_at, started_at, completed_at, updated_at
            FROM workflow_runs
            WHERE status = 'failed'
              AND updated_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
            ORDER BY updated_at DESC
            LIMIT $2
        )
        SELECT f.*,
               EXISTS (
                   SELECT 1
                   FROM workflow_runs newer
                   WHERE newer.workflow_name = f.workflow_name
                     AND newer.status = 'completed'
                     AND COALESCE(newer.completed_at, newer.updated_at, newer.created_at)
                         > COALESCE(f.completed_at, f.updated_at, f.created_at)
               )
               OR (
                   COALESCE(f.input_json->'metadata'->>'source', '') = 'workflow_schedule'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM workflow_schedules sched
                       WHERE sched.workflow_name = f.workflow_name
                         AND sched.enabled = TRUE
                   )
               ) AS recovered
        FROM recent_failures f
        ORDER BY f.updated_at DESC
        LIMIT $2
        """,
        float(window_hours),
        limit,
    )
    return [
        {
            "component": "workflow",
            "severity": "warning" if row["recovered"] else "critical",
            "id": row["run_id"],
            "title": (
                f"{row['workflow_name']} failed"
                + (" (recovered)" if row["recovered"] else "")
            ),
            "message": row["error_text"] or "workflow failed",
            "status": row["status"],
            "recovered": bool(row["recovered"]),
            "thread_key": row["thread_key"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "completed_at": _iso(row["completed_at"]),
        }
        for row in rows
    ]


async def _fetch_recent_execution_failures(pool, window_hours: int, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, status, terminal_reason, error_text,
               created_at, started_at, completed_at, updated_at
        FROM agent_execution_requests
        WHERE status = 'failed_permanent'
          AND updated_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        float(window_hours),
        limit,
    )
    return [
        {
            "component": "execution",
            "severity": "critical",
            "id": row["execution_id"],
            "title": "Agent execution failed",
            "message": row["error_text"] or row["terminal_reason"] or "execution failed",
            "status": row["status"],
            "thread_key": row["thread_key"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "completed_at": _iso(row["completed_at"]),
        }
        for row in rows
    ]


async def _fetch_recent_delivery_failures(pool, window_hours: int, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, delivery, state, attempt_count,
               last_error, created_at, updated_at
        FROM agent_final_delivery_outbox
        WHERE (
            state = 'dead_letter'
            OR (last_error IS NOT NULL AND last_error <> '')
        )
          AND NOT (
              thread_key LIKE 'workflow:%'
              AND state = 'dead_letter'
              AND COALESCE(last_error, '') LIKE 'missing_slack_delivery_target:%'
              AND NULLIF(delivery->>'thread_ts', '') IS NULL
          )
          AND updated_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        float(window_hours),
        limit,
    )
    return [
        {
            "component": "slack_delivery",
            "severity": "critical" if row["state"] == "dead_letter" else "warning",
            "id": row["execution_id"],
            "title": "Slack delivery failure",
            "message": row["last_error"] or row["state"],
            "status": row["state"],
            "thread_key": row["thread_key"],
            "attempt_count": row["attempt_count"],
            "delivery": _json(row["delivery"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


async def _fetch_recent_sandbox_errors(pool, window_hours: int, limit: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT thread_key, sandbox_id, harness, engine, state, thread_name,
               started_at, updated_at
        FROM sandbox_sessions
        WHERE state = 'error'
          AND updated_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        float(window_hours),
        limit,
    )
    return [
        {
            "component": "sandbox",
            "severity": "critical",
            "id": row["sandbox_id"],
            "title": "Sandbox runtime is in error state",
            "message": row["thread_name"] or row["thread_key"],
            "status": row["state"],
            "thread_key": row["thread_key"],
            "harness": row["harness"],
            "engine": row["engine"],
            "started_at": _iso(row["started_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


async def _fetch_recent_errors(pool, window_hours: int, limit: int = 25) -> list[dict[str, Any]]:
    batches = [
        await _fetch_recent_workflow_failures(pool, window_hours, limit),
        await _fetch_recent_execution_failures(pool, window_hours, limit),
        await _fetch_recent_delivery_failures(pool, window_hours, limit),
        await _fetch_recent_sandbox_errors(pool, window_hours, limit),
    ]
    errors = [item for batch in batches for item in batch]
    errors.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return errors[:limit]


async def _fetch_stuck_workflows(pool, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    running_rows = await pool.fetch(
        """
        SELECT run_id, workflow_name, thread_key, status,
               EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))::int AS age_seconds,
               created_at, started_at, updated_at
        FROM workflow_runs
        WHERE status = 'running'
          AND COALESCE(started_at, created_at) < NOW() - ($1::double precision * INTERVAL '1 second')
        ORDER BY COALESCE(started_at, created_at) ASC
        LIMIT $2
        """,
        float(WORKFLOW_RUNNING_CRITICAL_S),
        limit,
    )
    overdue_rows = await pool.fetch(
        """
        SELECT run_id, workflow_name, thread_key, status,
               EXTRACT(EPOCH FROM (NOW() - available_at))::int AS overdue_seconds,
               created_at, available_at, updated_at
        FROM workflow_runs
        WHERE status IN ('sleeping', 'waiting')
          AND available_at < NOW() - ($1::double precision * INTERVAL '1 second')
        ORDER BY available_at ASC
        LIMIT $2
        """,
        float(WORKFLOW_OVERDUE_WARNING_S),
        limit,
    )
    return {
        "running_workflows": [
            {
                "run_id": row["run_id"],
                "workflow_name": row["workflow_name"],
                "thread_key": row["thread_key"],
                "status": row["status"],
                "age_seconds": row["age_seconds"],
                "created_at": _iso(row["created_at"]),
                "started_at": _iso(row["started_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in running_rows
        ],
        "overdue_workflows": [
            {
                "run_id": row["run_id"],
                "workflow_name": row["workflow_name"],
                "thread_key": row["thread_key"],
                "status": row["status"],
                "overdue_seconds": row["overdue_seconds"],
                "created_at": _iso(row["created_at"]),
                "available_at": _iso(row["available_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in overdue_rows
        ],
    }


async def _fetch_stuck_executions(pool, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    queued_rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, status,
               EXTRACT(EPOCH FROM (NOW() - created_at))::int AS age_seconds,
               created_at, updated_at
        FROM agent_execution_requests
        WHERE status = 'queued'
          AND created_at < NOW() - ($1::double precision * INTERVAL '1 second')
        ORDER BY created_at ASC
        LIMIT $2
        """,
        float(EXECUTION_QUEUE_WARNING_S),
        limit,
    )
    running_rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, status,
               EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))::int AS age_seconds,
               created_at, started_at, last_progress_at, silence_deadline_at,
               hard_deadline_at, updated_at
        FROM agent_execution_requests
        WHERE status IN ('running', 'cancel_requested', 'retry_wait')
          AND (
              COALESCE(started_at, created_at) < NOW() - ($1::double precision * INTERVAL '1 second')
              OR silence_deadline_at < NOW()
              OR hard_deadline_at < NOW()
          )
        ORDER BY COALESCE(started_at, created_at) ASC
        LIMIT $2
        """,
        float(EXECUTION_RUNNING_CRITICAL_S),
        limit,
    )
    return {
        "queued_executions": [
            {
                "execution_id": row["execution_id"],
                "thread_key": row["thread_key"],
                "status": row["status"],
                "age_seconds": row["age_seconds"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in queued_rows
        ],
        "running_executions": [
            {
                "execution_id": row["execution_id"],
                "thread_key": row["thread_key"],
                "status": row["status"],
                "age_seconds": row["age_seconds"],
                "created_at": _iso(row["created_at"]),
                "started_at": _iso(row["started_at"]),
                "last_progress_at": _iso(row["last_progress_at"]),
                "silence_deadline_at": _iso(row["silence_deadline_at"]),
                "hard_deadline_at": _iso(row["hard_deadline_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in running_rows
        ],
    }


async def _fetch_stuck_deliveries(pool, limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, delivery, state, final_payload,
               attempt_count, last_error, created_at, updated_at, next_attempt_at,
               lease_owner, lease_expires_at,
               EXTRACT(EPOCH FROM (NOW() - COALESCE(next_attempt_at, updated_at, created_at)))::int
                   AS overdue_seconds
        FROM agent_final_delivery_outbox
        WHERE state IN ('pending', 'sending', 'retry_wait')
          AND COALESCE(next_attempt_at, updated_at, created_at)
              < NOW() - ($1::double precision * INTERVAL '1 second')
        ORDER BY COALESCE(next_attempt_at, updated_at, created_at) ASC
        LIMIT $2
        """,
        float(DELIVERY_WARNING_S),
        limit,
    )
    return [
        {
            "execution_id": row["execution_id"],
            "thread_key": row["thread_key"],
            "delivery": _json(row["delivery"]),
            "state": row["state"],
            "attempt_count": row["attempt_count"],
            "last_error": row["last_error"],
            "overdue_seconds": row["overdue_seconds"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "next_attempt_at": _iso(row["next_attempt_at"]),
            "lease_owner": row["lease_owner"],
            "lease_expires_at": _iso(row["lease_expires_at"]),
        }
        for row in rows
    ]


async def _fetch_schedule_lag(pool, limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT schedule_id, workflow_name, schedule_kind, schedule_expr, timezone,
               enabled, next_run_at, last_run_at,
               EXTRACT(EPOCH FROM (NOW() - next_run_at))::int AS lag_seconds
        FROM workflow_schedules
        WHERE enabled = TRUE
          AND next_run_at < NOW() - ($1::double precision * INTERVAL '1 second')
        ORDER BY next_run_at ASC
        LIMIT $2
        """,
        float(SCHEDULE_WARNING_S),
        limit,
    )
    return [
        {
            "schedule_id": row["schedule_id"],
            "workflow_name": row["workflow_name"],
            "schedule_kind": row["schedule_kind"],
            "schedule_expr": row["schedule_expr"],
            "timezone": row["timezone"],
            "enabled": row["enabled"],
            "lag_seconds": row["lag_seconds"],
            "next_run_at": _iso(row["next_run_at"]),
            "last_run_at": _iso(row["last_run_at"]),
        }
        for row in rows
    ]


async def _fetch_dev_pulse(pool) -> dict[str, Any]:
    schedules = await pool.fetch(
        """
        SELECT schedule_id, workflow_name, schedule_kind, schedule_expr, timezone,
               enabled, next_run_at, last_run_at, input_json
        FROM workflow_schedules
        WHERE workflow_name = ANY($1::text[])
        ORDER BY workflow_name, schedule_id
        """,
        list(DEV_PULSE_WORKFLOWS),
    )
    recent_runs = await pool.fetch(
        """
        SELECT run_id, workflow_name, status, input_json, output_json, error_text,
               created_at, started_at, completed_at, updated_at
        FROM workflow_runs
        WHERE workflow_name = ANY($1::text[])
        ORDER BY created_at DESC
        LIMIT 10
        """,
        list(DEV_PULSE_WORKFLOWS),
    )
    last_success = await pool.fetchrow(
        """
        SELECT run_id, workflow_name, status, input_json, output_json,
               created_at, started_at, completed_at, updated_at
        FROM workflow_runs
        WHERE workflow_name = ANY($1::text[])
          AND status = 'completed'
        ORDER BY completed_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """,
        list(DEV_PULSE_WORKFLOWS),
    )
    last_failure = await pool.fetchrow(
        """
        SELECT run_id, workflow_name, status, input_json, output_json, error_text,
               created_at, started_at, completed_at, updated_at
        FROM workflow_runs
        WHERE workflow_name = ANY($1::text[])
          AND status = 'failed'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        list(DEV_PULSE_WORKFLOWS),
    )

    schedule_items = [
        {
            "schedule_id": row["schedule_id"],
            "workflow_name": row["workflow_name"],
            "schedule_kind": row["schedule_kind"],
            "schedule_expr": row["schedule_expr"],
            "timezone": row["timezone"],
            "enabled": row["enabled"],
            "next_run_at": _iso(row["next_run_at"]),
            "last_run_at": _iso(row["last_run_at"]),
            "input": _json(row["input_json"]),
        }
        for row in schedules
    ]
    recent_items = [
        {
            "run_id": row["run_id"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "input": _json(row["input_json"]),
            "output": _json(row["output_json"]),
            "error_text": row["error_text"],
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "completed_at": _iso(row["completed_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in recent_runs
    ]

    def _run(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = {
            "run_id": row["run_id"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "input": _json(row["input_json"]),
            "output": _json(row["output_json"]),
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "completed_at": _iso(row["completed_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        if "error_text" in row:
            item["error_text"] = row["error_text"]
        return item

    enabled_count = sum(1 for item in schedule_items if item["enabled"])
    failed_latest = bool(recent_items and recent_items[0]["status"] == "failed")
    status = _status_from_bool(
        critical=failed_latest,
        warning=enabled_count == 0 or last_success is None,
    )
    if enabled_count == 0:
        summary = "Dev Pulse schedules are missing or disabled."
    elif failed_latest:
        summary = "The latest Dev Pulse workflow failed."
    elif last_success is None:
        summary = "Dev Pulse is scheduled but has no successful run yet."
    else:
        summary = "Dev Pulse has a recent successful run and enabled schedule."

    return {
        "status": status,
        "summary": summary,
        "schedules": schedule_items,
        "recent_runs": recent_items,
        "last_success": _run(last_success),
        "last_failure": _run(last_failure),
    }


async def _fetch_observations(pool, window_hours: int) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM((event_json->>'tool_error_events')::int), 0)::int
                AS tool_error_events_24h,
            COALESCE(SUM((event_json->>'command_error_events')::int), 0)::int
                AS command_error_events_24h,
            COALESCE(SUM((event_json->>'error_events')::int), 0)::int
                AS error_events_24h,
            COALESCE(SUM((event_json->>'total_tokens')::int), 0)::int
                AS total_tokens_24h,
            COALESCE(SUM((event_json->>'cost_usd')::numeric), 0)::float
                AS cost_usd_24h
        FROM agent_execution_events
        WHERE event_kind = 'execution_summary'
          AND created_at >= NOW() - ($1::double precision * INTERVAL '1 hour')
        """,
        float(window_hours),
    )
    if row is None:
        return {
            "tool_error_events_24h": 0,
            "command_error_events_24h": 0,
            "error_events_24h": 0,
            "total_tokens_24h": 0,
            "cost_usd_24h": 0.0,
        }
    return {
        "tool_error_events_24h": int(row["tool_error_events_24h"] or 0),
        "command_error_events_24h": int(row["command_error_events_24h"] or 0),
        "error_events_24h": int(row["error_events_24h"] or 0),
        "total_tokens_24h": int(row["total_tokens_24h"] or 0),
        "cost_usd_24h": float(row["cost_usd_24h"] or 0.0),
    }


async def _fetch_runtime_counts(pool) -> dict[str, Any]:
    sandbox_rows = await pool.fetch(
        """
        SELECT state, COUNT(*)::int AS count
        FROM sandbox_sessions
        GROUP BY state
        ORDER BY state
        """
    )
    assignment_rows = await pool.fetch(
        """
        SELECT state, COUNT(*)::int AS count
        FROM agent_runtime_assignments
        GROUP BY state
        ORDER BY state
        """
    )
    return {
        "sandbox_sessions": {str(row["state"]): int(row["count"]) for row in sandbox_rows},
        "runtime_assignments": {
            str(row["state"]): int(row["count"]) for row in assignment_rows
        },
    }


async def _fetch_metrics(pool, window_hours: int) -> dict[str, Any]:
    return {
        "workflow_runs_24h": await _status_counts(pool, "workflow_runs", window_hours),
        "agent_executions_24h": await _status_counts(
            pool, "agent_execution_requests", window_hours
        ),
        "runtime": await _fetch_runtime_counts(pool),
    }


async def _fetch_recent_workflow_runs(pool, limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT run_id, workflow_name, status, thread_key, input_json, output_json,
               error_text, created_at, started_at, completed_at, updated_at
        FROM workflow_runs
        ORDER BY created_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "run_id": row["run_id"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "thread_key": row["thread_key"],
            "input": _json(row["input_json"]),
            "output": _json(row["output_json"]),
            "error_text": row["error_text"],
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "completed_at": _iso(row["completed_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


async def _fetch_recent_executions(pool, limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT execution_id, thread_key, status, terminal_reason, error_text,
               metadata, delivery, created_at, started_at, completed_at, updated_at
        FROM agent_execution_requests
        ORDER BY created_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "execution_id": row["execution_id"],
            "thread_key": row["thread_key"],
            "status": row["status"],
            "terminal_reason": row["terminal_reason"],
            "error_text": row["error_text"],
            "metadata": _json(row["metadata"]),
            "delivery": _json(row["delivery"]),
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "completed_at": _iso(row["completed_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _tool_monitor(tool_manager: Any | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if tool_manager is None:
        summary = {"loaded_count": None, "failed_count": None, "total_methods": None}
        return (
            summary,
            _monitor(
                "tool_registry",
                "warning",
                "Tool registry unavailable",
                "The tool manager was not attached to the request app state.",
                evidence=summary,
            ),
        )

    tools = getattr(tool_manager, "tools", {}) or {}
    failures = list(getattr(tool_manager, "load_failures", []) or [])
    total_methods = 0
    for tool in tools.values():
        total_methods += len(getattr(tool, "methods", []) or [])
    summary = {
        "loaded_count": len(tools),
        "failed_count": len(failures),
        "total_methods": total_methods,
        "failed": failures[:20],
    }
    status = "critical" if failures else "ok"
    return (
        summary,
        _monitor(
            "tool_registry",
            status,
            "Tool registry",
            (
                f"{len(failures)} tools failed to load."
                if failures
                else f"{len(tools)} tools loaded successfully."
            ),
            evidence=summary,
        ),
    )


def _build_monitors(
    *,
    recent_errors: list[dict[str, Any]],
    stuck_work: dict[str, Any],
    schedule_lag: list[dict[str, Any]],
    dev_pulse: dict[str, Any],
    metrics: dict[str, Any],
    observations: dict[str, Any],
    tool_monitor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    workflow_failures = [
        item for item in recent_errors if item.get("component") == "workflow"
    ]
    unrecovered_workflow_failures = [
        item for item in workflow_failures if not item.get("recovered")
    ]
    recovered_workflow_failures = [
        item for item in workflow_failures if item.get("recovered")
    ]
    execution_failed = int(metrics["agent_executions_24h"].get("failed_permanent", 0))
    sandbox_errors = int(metrics["runtime"]["sandbox_sessions"].get("error", 0))
    stuck_running_workflows = stuck_work["running_workflows"]
    overdue_workflows = stuck_work["overdue_workflows"]
    queued_executions = stuck_work["queued_executions"]
    running_executions = stuck_work["running_executions"]
    stuck_deliveries = stuck_work["deliveries"]
    max_schedule_lag = max((item["lag_seconds"] or 0 for item in schedule_lag), default=0)
    max_delivery_lag = max(
        (item["overdue_seconds"] or 0 for item in stuck_deliveries),
        default=0,
    )
    max_queue_age = max(
        (item["age_seconds"] or 0 for item in queued_executions),
        default=0,
    )

    monitors = [
        _monitor(
            "recent_workflow_failures",
            _status_from_bool(
                critical=bool(unrecovered_workflow_failures),
                warning=bool(recovered_workflow_failures),
            ),
            "Recent workflow failures",
            (
                f"{len(unrecovered_workflow_failures)} unrecovered workflow failure(s) in the last 24 hours."
                if unrecovered_workflow_failures
                else (
                    f"{len(recovered_workflow_failures)} recovered workflow failure(s) in the last 24 hours."
                    if recovered_workflow_failures
                    else "No workflow failures in the last 24 hours."
                )
            ),
            evidence={
                "failed_24h": int(metrics["workflow_runs_24h"].get("failed", 0)),
                "unrecovered_count": len(unrecovered_workflow_failures),
                "recovered_count": len(recovered_workflow_failures),
            },
        ),
        _monitor(
            "stuck_workflows",
            _status_from_bool(
                critical=bool(stuck_running_workflows)
                or any(
                    (item.get("overdue_seconds") or 0) >= WORKFLOW_OVERDUE_CRITICAL_S
                    for item in overdue_workflows
                ),
                warning=bool(overdue_workflows),
            ),
            "Stuck workflows",
            (
                f"{len(stuck_running_workflows)} running and {len(overdue_workflows)} overdue."
                if stuck_running_workflows or overdue_workflows
                else "No stale running, sleeping, or waiting workflows."
            ),
            evidence={
                "running_count": len(stuck_running_workflows),
                "overdue_count": len(overdue_workflows),
            },
        ),
        _monitor(
            "schedule_lag",
            _status_from_bool(
                critical=max_schedule_lag >= SCHEDULE_CRITICAL_S,
                warning=max_schedule_lag >= SCHEDULE_WARNING_S,
            ),
            "Workflow schedule lag",
            (
                f"{len(schedule_lag)} enabled schedules are overdue."
                if schedule_lag
                else "No enabled schedules are overdue."
            ),
            evidence={"overdue_count": len(schedule_lag), "max_lag_seconds": max_schedule_lag},
        ),
        _monitor(
            "recent_execution_failures",
            _status_from_bool(execution_failed > 0),
            "Recent execution failures",
            (
                f"{execution_failed} agent execution failed in the last 24 hours."
                if execution_failed
                else "No failed agent executions in the last 24 hours."
            ),
            evidence={
                "failed_permanent_24h": execution_failed,
                "tool_error_events_24h": observations["tool_error_events_24h"],
                "command_error_events_24h": observations["command_error_events_24h"],
            },
        ),
        _monitor(
            "stuck_executions",
            _status_from_bool(
                critical=bool(running_executions)
                or max_queue_age >= EXECUTION_QUEUE_CRITICAL_S,
                warning=bool(queued_executions),
            ),
            "Stuck executions",
            (
                f"{len(queued_executions)} queued and {len(running_executions)} running executions need attention."
                if queued_executions or running_executions
                else "No stale queued or running executions."
            ),
            evidence={
                "queued_count": len(queued_executions),
                "running_count": len(running_executions),
                "oldest_queue_age_seconds": max_queue_age,
            },
        ),
        _monitor(
            "slack_delivery_outbox",
            _status_from_bool(
                critical=max_delivery_lag >= DELIVERY_CRITICAL_S
                or any(
                    item.get("component") == "slack_delivery"
                    and item.get("status") == "dead_letter"
                    for item in recent_errors
                ),
                warning=bool(stuck_deliveries),
            ),
            "Slack final delivery",
            (
                f"{len(stuck_deliveries)} Slack deliveries are overdue."
                if stuck_deliveries
                else "No overdue Slack final deliveries."
            ),
            evidence={
                "overdue_count": len(stuck_deliveries),
                "max_overdue_seconds": max_delivery_lag,
            },
        ),
        _monitor(
            "sandbox_errors",
            _status_from_bool(sandbox_errors > 0),
            "Sandbox runtime errors",
            (
                f"{sandbox_errors} sandbox sessions are in error state."
                if sandbox_errors
                else "No sandbox sessions are in error state."
            ),
            evidence={"error_count": sandbox_errors},
        ),
        _monitor(
            "dev_pulse",
            str(dev_pulse["status"]),
            "Dev Pulse workflow",
            str(dev_pulse["summary"]),
            evidence={
                "schedule_count": len(dev_pulse["schedules"]),
                "last_success": dev_pulse["last_success"],
                "last_failure": dev_pulse["last_failure"],
            },
        ),
    ]
    if tool_monitor is not None:
        monitors.append(tool_monitor)
    return monitors


async def build_ops_summary(
    pool,
    *,
    include_tools: bool = False,
    tool_manager: Any | None = None,
    window_hours: int = OPS_WINDOW_HOURS,
) -> dict[str, Any]:
    window_hours = max(min(int(window_hours), 168), 1)
    recent_errors = await _fetch_recent_errors(pool, window_hours)
    workflow_stuck = await _fetch_stuck_workflows(pool)
    execution_stuck = await _fetch_stuck_executions(pool)
    deliveries = await _fetch_stuck_deliveries(pool)
    schedule_lag = await _fetch_schedule_lag(pool)
    metrics = await _fetch_metrics(pool, window_hours)
    observations = await _fetch_observations(pool, window_hours)
    dev_pulse = await _fetch_dev_pulse(pool)
    recent_workflows = await _fetch_recent_workflow_runs(pool)
    recent_executions = await _fetch_recent_executions(pool)

    tool_summary: dict[str, Any] | None = None
    tool_monitor: dict[str, Any] | None = None
    if include_tools:
        tool_summary, tool_monitor = _tool_monitor(tool_manager)

    stuck_work = {
        **workflow_stuck,
        **execution_stuck,
        "deliveries": deliveries,
        "schedule_lag": schedule_lag,
    }
    monitors = _build_monitors(
        recent_errors=recent_errors,
        stuck_work=stuck_work,
        schedule_lag=schedule_lag,
        dev_pulse=dev_pulse,
        metrics=metrics,
        observations=observations,
        tool_monitor=tool_monitor,
    )

    return {
        "generated_at": _iso(_utc_now()),
        "window_hours": window_hours,
        "status": _rollup_status(monitors),
        "monitors": monitors,
        "recent_errors": recent_errors,
        "stuck_work": stuck_work,
        "metrics": metrics,
        "observations": observations,
        "dev_pulse": dev_pulse,
        "recent_workflows": recent_workflows,
        "recent_executions": recent_executions,
        "tools": tool_summary,
    }
