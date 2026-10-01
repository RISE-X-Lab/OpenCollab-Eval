"""Shared constants and embedded container capture code for prediction generation."""

from __future__ import annotations

import re

from opencollab_eval.benchmarks.task_specification import CONTAINER_REPO_ROOT
from opencollab_eval.candidate_bytes import CANDIDATE_BYTE_BUDGET, MAX_CANDIDATE_FILE_BYTES

DOCKER_WORKDIR = CONTAINER_REPO_ROOT
# Profile resolution supplies the effective standalone limits after parsing.
# Both generators start from the workflow's optional caps and wall timeout.
DEFAULT_BUDGET = None
DEFAULT_MAX_STEPS = None
DEFAULT_TIMEOUT = 1800.0
# Conda-backed task images require the prepared testbed environment. Images
# without a Conda marker keep their native language toolchain unchanged.
_ACTIVATE = (
    "if [ -e /opt/miniconda3/bin/activate ] || "
    "[ -e /opt/miniconda3/envs/testbed ] || "
    "[ \"${CONDA_DEFAULT_ENV:-}\" = testbed ]; then "
    "source /opt/miniconda3/bin/activate testbed >/dev/null || { "
    "printf '%s\\n' 'Environment preparation failed: testbed activation failed' >&2; "
    "exit 86; }; "
    "[ \"${CONDA_DEFAULT_ENV:-}\" = testbed ] || { "
    "printf '%s\\n' 'Environment preparation failed: testbed activation was not confirmed' >&2; "
    "exit 86; }; fi"
)
MAX_EXTRACTED_PATCH_BYTES = CANDIDATE_BYTE_BUDGET.patch_bytes
MAX_STATUS_DIAGNOSTIC_BYTES = 64 * 1024
MAX_CAPTURED_STDERR_BYTES = 64 * 1024
CONTAINER_OWNER_SCHEMA_VERSION = 1
CONTAINER_OWNER_LABEL = "opencollab_eval.engine.owner-token"
PENDING_OUTPUT_SCHEMA_VERSION = 1
MAX_PENDING_OUTPUT_BYTES = CANDIDATE_BYTE_BUDGET.pending_bytes
MAX_JSONL_SCAN_LINE_BYTES = CANDIDATE_BYTE_BUDGET.jsonl_line_bytes
MAX_COMPATIBILITY_MARKER_BYTES = 4096
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_OWNER_RECORD_BYTES = 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = MAX_CANDIDATE_FILE_BYTES
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_CANCELLATION_GRACE_SECONDS = 2.0
_MISSING_CONTAINER_RE = re.compile(r"(?:no such (?:container|object)|not found)", re.IGNORECASE)


# Working tools for the scripted collaboration seats. Native agents own their tools.
WORKING_TOOL_NAMES = ("apply_patch", "bash", "file_read", "file_write", "grep", "submit")

AGENT_PROMPT = """\
You are an autonomous software engineer working on a real bug in a software
repository. You are working on this task alone.
"""

WORKFLOW_AGENT_PROMPT = """\
Obey the current software role. Use public repository evidence only.
"""
