from api.agent import _terminal_error_from_harness_event


def test_terminal_error_from_codex_turn_failed_preserves_message_and_details():
    event = {
        "type": "turn.failed",
        "error": {
            "message": "Reconnecting... 2/5",
            "additionalDetails": "timeout waiting for child process to exit",
        },
    }

    assert (
        _terminal_error_from_harness_event(event)
        == "Reconnecting... 2/5: timeout waiting for child process to exit"
    )


def test_terminal_error_from_codex_turn_failed_without_message_has_safe_fallback():
    assert _terminal_error_from_harness_event({"type": "turn.failed", "error": {}}) == (
        "Harness turn failed"
    )
