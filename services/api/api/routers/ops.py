from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from api.deps import verify_operator_api_key
from api.ops_monitors import build_ops_summary

router = APIRouter(prefix="/ops", tags=["ops"])

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "ops"
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _asset(name: str) -> Path:
    return _STATIC_DIR / name


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def ops_index() -> FileResponse:
    return FileResponse(
        _asset("index.html"),
        media_type="text/html; charset=utf-8",
        headers=_NO_STORE_HEADERS,
    )


@router.get("/styles.css", include_in_schema=False)
async def ops_styles() -> FileResponse:
    return FileResponse(
        _asset("styles.css"),
        media_type="text/css; charset=utf-8",
        headers=_NO_STORE_HEADERS,
    )


@router.get("/app.js", include_in_schema=False)
async def ops_app_js() -> FileResponse:
    return FileResponse(
        _asset("app.js"),
        media_type="application/javascript; charset=utf-8",
        headers=_NO_STORE_HEADERS,
    )


async def _summary(request: Request, window_hours: int) -> dict[str, Any]:
    return await build_ops_summary(
        request.app.state.db_pool,
        include_tools=True,
        tool_manager=getattr(request.app.state, "tool_manager", None),
        window_hours=window_hours,
    )


@router.get("/api/summary", dependencies=[Depends(verify_operator_api_key)])
async def ops_summary(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    return await _summary(request, window_hours)


@router.get("/api/monitors", dependencies=[Depends(verify_operator_api_key)])
async def ops_monitors(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    summary = await _summary(request, window_hours)
    return {
        "generated_at": summary["generated_at"],
        "status": summary["status"],
        "monitors": summary["monitors"],
    }


@router.get("/api/errors", dependencies=[Depends(verify_operator_api_key)])
async def ops_errors(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    summary = await _summary(request, window_hours)
    return {
        "generated_at": summary["generated_at"],
        "status": summary["status"],
        "recent_errors": summary["recent_errors"],
        "stuck_work": summary["stuck_work"],
    }


@router.get("/api/workflows", dependencies=[Depends(verify_operator_api_key)])
async def ops_workflows(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    summary = await _summary(request, window_hours)
    return {
        "generated_at": summary["generated_at"],
        "metrics": summary["metrics"]["workflow_runs_24h"],
        "recent_workflows": summary["recent_workflows"],
        "schedule_lag": summary["stuck_work"]["schedule_lag"],
    }


@router.get("/api/executions", dependencies=[Depends(verify_operator_api_key)])
async def ops_executions(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    summary = await _summary(request, window_hours)
    return {
        "generated_at": summary["generated_at"],
        "metrics": summary["metrics"]["agent_executions_24h"],
        "observations": summary["observations"],
        "recent_executions": summary["recent_executions"],
        "stuck_executions": {
            "queued": summary["stuck_work"]["queued_executions"],
            "running": summary["stuck_work"]["running_executions"],
        },
    }


@router.get("/api/dev-pulse", dependencies=[Depends(verify_operator_api_key)])
async def ops_dev_pulse(
    request: Request,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    summary = await _summary(request, window_hours)
    return {
        "generated_at": summary["generated_at"],
        "dev_pulse": summary["dev_pulse"],
    }
