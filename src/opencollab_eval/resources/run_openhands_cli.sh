#!/usr/bin/env bash
set -euo pipefail

REMOTE_REPO="${OPENCOLLAB_REMOTE_REPO:-}"
PYTHON_BIN="${OPENCOLLAB_OPENHANDS_PYTHON:-python3}"

if [[ -z "$REMOTE_REPO" || ! -d "$REMOTE_REPO" ]]; then
  echo "OpenHands requires OPENCOLLAB_REMOTE_REPO to name an existing directory" >&2
  exit 2
fi
if [[ -z "${LLM_API_KEY:-}" || -z "${LLM_MODEL:-}" ]]; then
  echo "OpenHands requires LLM_API_KEY and LLM_MODEL" >&2
  exit 2
fi

export PYTHONPATH="$REMOTE_REPO/src${PYTHONPATH:+:$PYTHONPATH}"
export TTY_INTERACTIVE="${TTY_INTERACTIVE:-1}"
export OPENHANDS_SUPPRESS_BANNER="${OPENHANDS_SUPPRESS_BANNER:-1}"

exec "$PYTHON_BIN" -c '
from importlib.metadata import version

if version("openhands") != "1.16.0":
    raise SystemExit("OpenHands 1.16.0 is required")

from openhands.sdk import AgentContext
from openhands_cli.stores.agent_store import AgentStore


def offline_context(self):
    return AgentContext(
        skills=[],
        load_user_skills=False,
        load_public_skills=False,
    )


AgentStore._build_agent_context = offline_context
from opencollab_eval.generation.openhands_runtime import install_runtime_overrides

install_runtime_overrides()
from openhands_cli.entrypoint import main
main()
' "$@"
