"""Friday 23:59 Beijing-time schedule alias for the dev pulse workflow."""

from __future__ import annotations

import os

from workflows.dev_pulse_daily import (
    BEIJING_TZ,
    DEFAULT_SLACK_CHANNEL,
    DEFAULT_SLACK_SENDER_NAME,
    Input,
    _env_flag_enabled,
    handler,
)

WORKFLOW_NAME = "dev_pulse_friday_eod"
__all__ = ["Input", "SCHEDULE", "WORKFLOW_NAME", "handler"]

SCHEDULE = {
    "schedule_id": "dev_pulse_friday_eod_bjt",
    "cron": "59 23 * * 5",
    "timezone": BEIJING_TZ,
    "slack_channel": os.getenv("DEV_PULSE_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL),
    "enabled": _env_flag_enabled("DEV_PULSE_ENABLED", default=True),
    "catchup_policy": "skip",
    "input": {
        "slack_channel": os.getenv("DEV_PULSE_SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL),
        "slack_sender_name": os.getenv("DEV_PULSE_SLACK_SENDER_NAME", DEFAULT_SLACK_SENDER_NAME),
        "schedule_label": "friday_2359_bjt",
    },
}
