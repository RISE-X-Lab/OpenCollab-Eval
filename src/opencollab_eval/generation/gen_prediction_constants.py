"""Shared constants and embedded container capture code for prediction generation."""

from __future__ import annotations

import re

from opencollab_eval.engine.swe_eval_records import MAX_JSONL_SCAN_BYTES

DOCKER_WORKDIR = "/testbed"
# Activate the testbed conda env so the agent's `python`/tests see the repo deps.
_ACTIVATE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true"
MAX_EXTRACTED_PATCH_BYTES = 8 * 1024 * 1024
MAX_STATUS_DIAGNOSTIC_BYTES = 64 * 1024
MAX_CAPTURED_STDERR_BYTES = 64 * 1024
CONTAINER_OWNER_SCHEMA_VERSION = 1
CONTAINER_OWNER_LABEL = "opencollab_eval.engine.owner-token"
PENDING_OUTPUT_SCHEMA_VERSION = 1
MAX_PENDING_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_JSONL_SCAN_LINE_BYTES = 64 * 1024 * 1024
MAX_COMPATIBILITY_MARKER_BYTES = 4096
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_OWNER_RECORD_BYTES = 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = MAX_JSONL_SCAN_BYTES
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_CANCELLATION_GRACE_SECONDS = 2.0
_MISSING_CONTAINER_RE = re.compile(r"(?:no such (?:container|object)|not found)", re.IGNORECASE)


# The two system prompts below say who the agent is and stop there.
#
# They used to say a good deal more, and each arm said something different. The
# single-agent prompt carried seven imperative rules -- when to stop exploring,
# which tool to edit with, to verify with a snippet afterwards -- while the
# workflow prompt carried three lines and the team's roles carry role cards that
# deliberately give no procedure at all. A comparison across those arms is not
# reading off how the work was organized; it is partly reading off which arm was
# handed a tuned prompt. Everything those rules stated that is true of the task
# rather than of one arm now lives in ``gen_prediction_task_text``, which every
# arm receives verbatim.
#
# Two rules were dropped rather than moved:
#
# * "Do NOT run git commit." Patch extraction diffs file *content* against a
#   trusted host baseline (``gen_prediction_patch``), so a commit made inside the
#   container changes nothing it reads. The rule constrained nothing, and it is
#   false for a team whose handoff payload is a commit sha.
# * "As soon as you know the fix, APPLY it" and the rest of the pacing advice.
#   How much to explore before acting is a decision the run is measuring, not a
#   setting it should be pinning for one arm only.

#: The working tools every arm holds. The team configuration this arm is
#: compared against carries the same six names plus its collaboration channel
#: (``message_agent``, ``team_status``), and pins that equality in OpenCollab's
#: ``tests/test_handoff_experiment_team.py``.
#:
#: They diverged silently before: this arm had ``file_write`` and no
#: ``apply_patch``, the team's Analyst had ``apply_patch``, no ``file_write``,
#: and ``run_tests`` on top. Four capability differences on the one axis these
#: arms are not supposed to differ on, so a gap in results could be read off the
#: tool bundles instead of off how the work was organized.
WORKING_TOOL_NAMES = (
    "apply_patch",
    "bash",
    "file_read",
    "file_write",
    "grep",
    "run_tests",
)

AGENT_PROMPT = """\
You are an autonomous software engineer working on a real bug in a Python
repository. You are working on this task alone.
"""

WORKFLOW_AGENT_PROMPT = """\
You are a software engineer working on a real bug in a Python repository, in
the role this step of the workflow gives you.
"""
