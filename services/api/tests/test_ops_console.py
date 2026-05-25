from __future__ import annotations

import json
import uuid

import httpx
import pytest


async def _clear_ops_tables(db_pool) -> None:
    await db_pool.execute(
        """
        TRUNCATE TABLE
            agent_execution_events,
            agent_final_delivery_outbox,
            agent_execution_requests,
            agent_message_requests,
            agent_release_requests,
            agent_spawn_requests,
            agent_runtime_assignments,
            workflow_events,
            workflow_checkpoints,
            workflow_runs,
            workflow_schedules,
            sandbox_sessions
        CASCADE
        """
    )


@pytest.mark.asyncio
async def test_ops_summary_detects_recent_errors_and_stuck_work(db_pool):
    from api.ops_monitors import build_ops_summary

    await _clear_ops_tables(db_pool)
    suffix = uuid.uuid4().hex[:8]

    await db_pool.execute(
        """
        INSERT INTO workflow_runs (
            run_id, workflow_name, workflow_version, request_hash, root_run_id,
            thread_key, status, input_json, error_text, created_at, started_at,
            updated_at
        )
        VALUES
            ($1, 'nightly_check', 'test', 'hash-failed', $1, 'slack:C-ops:1',
             'failed', '{}'::jsonb, 'Linear API timed out',
             NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '5 minutes',
             NOW() - INTERVAL '4 minutes'),
            ($2, 'stuck_workflow', 'test', 'hash-running', $2, 'slack:C-ops:2',
             'running', '{}'::jsonb, NULL,
             NOW() - INTERVAL '1 hour', NOW() - INTERVAL '1 hour',
             NOW() - INTERVAL '45 minutes'),
            ($3, 'sleeping_workflow', 'test', 'hash-sleeping', $3, 'slack:C-ops:3',
             'sleeping', '{}'::jsonb, NULL,
             NOW() - INTERVAL '2 hours', NOW() - INTERVAL '2 hours',
             NOW() - INTERVAL '2 hours')
        """,
        f"wfr-failed-{suffix}",
        f"wfr-running-{suffix}",
        f"wfr-sleeping-{suffix}",
    )
    await db_pool.execute(
        "UPDATE workflow_runs SET available_at = NOW() - INTERVAL '20 minutes' "
        "WHERE run_id = $1",
        f"wfr-sleeping-{suffix}",
    )

    await db_pool.execute(
        """
        INSERT INTO workflow_schedules (
            schedule_id, workflow_name, schedule_kind, schedule_expr, timezone,
            enabled, next_run_at
        )
        VALUES (
            $1, 'dev_pulse_daily', 'cron', '0 0 * * 2-5', 'Asia/Shanghai',
            TRUE, NOW() - INTERVAL '30 minutes'
        )
        """,
        f"schedule-{suffix}",
    )

    await db_pool.execute(
        """
        INSERT INTO agent_execution_requests (
            execution_id, thread_key, assignment_generation, execute_id,
            request_hash, status, delivery, metadata, created_at, started_at,
            updated_at, error_text
        )
        VALUES
            ($1, 'slack:C-exe:1', 1, 'exec-failed', 'hash-exe-failed',
             'failed_permanent', '{}'::jsonb, '{}'::jsonb,
             NOW() - INTERVAL '3 minutes', NOW() - INTERVAL '3 minutes',
             NOW() - INTERVAL '2 minutes', 'sandbox crashed'),
            ($2, 'slack:C-exe:2', 1, 'exec-queued', 'hash-exe-queued',
             'queued', '{}'::jsonb, '{}'::jsonb,
             NOW() - INTERVAL '20 minutes', NULL,
             NOW() - INTERVAL '20 minutes', NULL),
            ($3, 'slack:C-exe:3', 1, 'exec-running', 'hash-exe-running',
             'running', '{}'::jsonb, '{}'::jsonb,
             NOW() - INTERVAL '1 hour', NOW() - INTERVAL '1 hour',
             NOW() - INTERVAL '40 minutes', NULL)
        """,
        f"exe-failed-{suffix}",
        f"exe-queued-{suffix}",
        f"exe-running-{suffix}",
    )

    await db_pool.execute(
        """
        INSERT INTO agent_final_delivery_outbox (
            execution_id, thread_key, delivery, state, final_payload,
            attempt_count, last_error, created_at, updated_at, next_attempt_at
        )
        VALUES
            ($1, 'slack:C-exe:4', '{"platform":"slack"}'::jsonb,
             'pending', '{"result_text":"hello"}'::jsonb, 2,
             'rate_limited', NOW() - INTERVAL '25 minutes',
             NOW() - INTERVAL '25 minutes', NOW() - INTERVAL '20 minutes'),
            ($2, 'slack:C-exe:5', '{"platform":"slack"}'::jsonb,
             'dead_letter', '{"result_text":"failed"}'::jsonb, 4,
             'channel_not_found', NOW() - INTERVAL '10 minutes',
             NOW() - INTERVAL '10 minutes', NULL)
        """,
        f"exe-delivery-pending-{suffix}",
        f"exe-delivery-dead-{suffix}",
    )

    await db_pool.execute(
        """
        INSERT INTO sandbox_sessions (
            thread_key, sandbox_id, harness, engine, state, thread_name,
            started_at, updated_at
        )
        VALUES (
            'slack:C-sandbox:1', $1, 'amp', 'amp', 'error', 'broken runtime',
            NOW() - INTERVAL '30 minutes', NOW() - INTERVAL '25 minutes'
        )
        """,
        f"sandbox-{suffix}",
    )

    await db_pool.execute(
        """
        INSERT INTO agent_execution_events (
            thread_key, execution_id, event_kind, event_json, created_at
        )
        VALUES (
            'slack:C-exe:1', $1, 'execution_summary',
            $2::jsonb, NOW() - INTERVAL '2 minutes'
        )
        """,
        f"exe-failed-{suffix}",
        json.dumps(
            {
                "type": "obs.execution_summary",
                "tool_error_events": 2,
                "command_error_events": 1,
                "error_events": 1,
                "tool_errors_by_name": {"linear": 2},
            }
        ),
    )

    summary = await build_ops_summary(db_pool, include_tools=False)

    assert summary["status"] == "critical"
    monitor_status = {item["id"]: item["status"] for item in summary["monitors"]}
    assert monitor_status["recent_workflow_failures"] == "critical"
    assert monitor_status["stuck_workflows"] == "critical"
    assert monitor_status["schedule_lag"] == "critical"
    assert monitor_status["recent_execution_failures"] == "critical"
    assert monitor_status["stuck_executions"] == "critical"
    assert monitor_status["slack_delivery_outbox"] == "critical"
    assert monitor_status["sandbox_errors"] == "critical"

    components = {item["component"] for item in summary["recent_errors"]}
    assert {"workflow", "execution", "slack_delivery", "sandbox"}.issubset(components)
    assert summary["metrics"]["workflow_runs_24h"]["failed"] == 1
    assert summary["metrics"]["agent_executions_24h"]["failed_permanent"] == 1
    assert summary["observations"]["tool_error_events_24h"] == 2
    assert summary["stuck_work"]["queued_executions"][0]["execution_id"].startswith(
        "exe-queued-"
    )


@pytest.mark.asyncio
async def test_ops_api_requires_admin_and_serves_browser_shell(client, managed_app):
    page = await client.get("/ops")
    assert page.status_code == 200
    assert "Centaur Ops Console" in page.text
    assert "/ops/api/summary" in page.text

    transport = httpx.ASGITransport(
        app=managed_app,
        client=("198.51.100.10", 49152),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as external:
        unauthenticated = await external.get("/ops/api/summary")
    assert unauthenticated.status_code == 401

    authenticated = await client.get("/ops/api/summary")
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["generated_at"]
    assert payload["status"] in {"ok", "warning", "critical"}
    assert isinstance(payload["monitors"], list)


@pytest.mark.asyncio
async def test_ops_summary_marks_later_success_as_recovered_workflow_failure(db_pool):
    from api.ops_monitors import build_ops_summary

    await _clear_ops_tables(db_pool)
    suffix = uuid.uuid4().hex[:8]

    await db_pool.execute(
        """
        INSERT INTO workflow_schedules (
            schedule_id, workflow_name, schedule_kind, schedule_expr, timezone,
            enabled, next_run_at, last_run_at
        )
        VALUES (
            $1, 'dev_pulse_daily', 'cron', '0 0 * * 2-5', 'Asia/Shanghai',
            TRUE, NOW() + INTERVAL '2 hours', NOW() - INTERVAL '1 day'
        )
        """,
        f"dev-pulse-daily-{suffix}",
    )
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (
            run_id, workflow_name, workflow_version, request_hash, root_run_id,
            status, input_json, output_json, error_text, created_at, started_at,
            completed_at, updated_at
        )
        VALUES
            (
                $1, 'dev_pulse_daily', 'old', 'hash-failed', $1,
                'failed', '{}'::jsonb, NULL, 'Linear returned 400',
                NOW() - INTERVAL '20 minutes', NOW() - INTERVAL '20 minutes',
                NOW() - INTERVAL '19 minutes', NOW() - INTERVAL '19 minutes'
            ),
            (
                $2, 'dev_pulse_daily', 'new', 'hash-success', $2,
                'completed', '{}'::jsonb, $3::jsonb, NULL,
                NOW() - INTERVAL '10 minutes', NOW() - INTERVAL '10 minutes',
                NOW() - INTERVAL '9 minutes', NOW() - INTERVAL '9 minutes'
            )
        """,
        f"wfr-recovered-failed-{suffix}",
        f"wfr-recovered-success-{suffix}",
        json.dumps({"slack_channel": "dev-pulse", "counts": {}}),
    )

    summary = await build_ops_summary(db_pool, include_tools=False)

    monitor = next(item for item in summary["monitors"] if item["id"] == "recent_workflow_failures")
    assert monitor["status"] == "warning"
    assert monitor["evidence"]["unrecovered_count"] == 0
    assert monitor["evidence"]["recovered_count"] == 1
    workflow_error = next(
        item for item in summary["recent_errors"] if item["component"] == "workflow"
    )
    assert workflow_error["recovered"] is True
    assert workflow_error["severity"] == "warning"


@pytest.mark.asyncio
async def test_ops_summary_surfaces_dev_pulse_health(db_pool):
    from api.ops_monitors import build_ops_summary

    await _clear_ops_tables(db_pool)
    suffix = uuid.uuid4().hex[:8]
    output = {
        "slack_channel": "dev-pulse",
        "window_start": "2026-05-21T16:00:00Z",
        "window_end": "2026-05-22T16:00:00Z",
        "counts": {
            "issues_closed": 3,
            "issues_created": 2,
            "prs_opened": 1,
            "prs_closed": 1,
            "outstanding_prs": 4,
            "non_bug_completed": 2,
            "completion_target": 5,
        },
    }

    await db_pool.execute(
        """
        INSERT INTO workflow_schedules (
            schedule_id, workflow_name, schedule_kind, schedule_expr, timezone,
            enabled, next_run_at, last_run_at
        )
        VALUES
            ($1, 'dev_pulse_daily', 'cron', '0 0 * * 2-5', 'Asia/Shanghai',
             TRUE, NOW() + INTERVAL '2 hours', NOW() - INTERVAL '1 day'),
            ($2, 'dev_pulse_friday_eod', 'cron', '59 23 * * 5', 'Asia/Shanghai',
             TRUE, NOW() + INTERVAL '4 days', NOW() - INTERVAL '3 days')
        """,
        f"dev-pulse-daily-{suffix}",
        f"dev-pulse-friday-{suffix}",
    )
    await db_pool.execute(
        """
        INSERT INTO workflow_runs (
            run_id, workflow_name, workflow_version, request_hash, root_run_id,
            status, input_json, output_json, created_at, started_at, completed_at,
            updated_at
        )
        VALUES (
            $1, 'dev_pulse_daily', 'test', 'hash-dev-pulse', $1,
            'completed', '{}'::jsonb, $2::jsonb,
            NOW() - INTERVAL '1 hour', NOW() - INTERVAL '1 hour',
            NOW() - INTERVAL '55 minutes', NOW() - INTERVAL '55 minutes'
        )
        """,
        f"wfr-dev-pulse-{suffix}",
        json.dumps(output),
    )

    summary = await build_ops_summary(db_pool, include_tools=False)

    dev_pulse = summary["dev_pulse"]
    assert dev_pulse["status"] == "ok"
    assert dev_pulse["last_success"]["workflow_name"] == "dev_pulse_daily"
    assert dev_pulse["last_success"]["output"]["counts"]["outstanding_prs"] == 4
    assert dev_pulse["schedules"][0]["next_run_at"]
    monitor_status = {item["id"]: item["status"] for item in summary["monitors"]}
    assert monitor_status["dev_pulse"] == "ok"
