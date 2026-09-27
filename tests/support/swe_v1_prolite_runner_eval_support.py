"""Shared preparation for swe v1 prolite runner eval."""

from __future__ import annotations

from tests.support.swe_v1_prolite_runner_test_support import (
    json,
)


def _owned_eval_marker(namespace, marker_path, container_id, container_name, *, state="active"):
    cleanup = namespace["remote_cleanup"]
    marker_path.write_text(
        json.dumps(
            {
                "schema": cleanup.EVAL_CONTAINER_SCHEMA,
                "state": state,
                "container_name": container_name,
                "container_id": container_id if state == "active" else "",
                "owner_nonce": namespace["owner_nonce"],
                "owner_label": cleanup.EVAL_OWNER_LABEL,
                "owner_schema_label": cleanup.EVAL_SCHEMA_LABEL,
                "owner_schema": cleanup.EVAL_SCHEMA_LABEL_VALUE,
            }
        ),
        encoding="utf-8",
    )

