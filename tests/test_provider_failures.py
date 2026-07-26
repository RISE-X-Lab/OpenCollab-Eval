from __future__ import annotations

from opencollab_eval.engine.provider_failures import summarize_terminal_provider_failures


def test_terminal_provider_failure_summary_uses_only_structured_fields() -> None:
    result = summarize_terminal_provider_failures(
        [
            {
                "label": "solver",
                "exception_type": "PermissionDeniedError",
                "status_code": 403,
                "provider_error_type": "access_terminated_error",
                "message": "must not be copied",
            },
            {
                "label": "reviewer",
                "exception_type": "RuntimeError",
                "status_code": None,
                "provider_error_type": None,
            },
        ]
    )

    assert result == {
        "status": "provider_request_rejected",
        "error_types": ["access_terminated_error"],
        "http_statuses": [403],
        "occurrences": 1,
        "direct_shared_probe_required": True,
    }


def test_unknown_provider_failure_does_not_claim_a_terminal_outage() -> None:
    assert summarize_terminal_provider_failures(
        [{"status_code": 500, "provider_error_type": "server_error"}]
    ) is None
