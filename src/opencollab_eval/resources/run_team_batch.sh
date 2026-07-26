#!/usr/bin/env bash
# Legacy batch team entrypoint retained as an explicit integrity gate.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run_team_batch.sh [legacy batch arguments]

This legacy batch entrypoint is disabled because its one-shot team runner does
not meet the current solver-isolation and host-trusted extraction contract.

Use python -m opencollab_eval.commands.swe_v1_prolite_runner for current remote batches, or invoke
python -m opencollab_eval.generation.gen_prediction_workflow for one local blind-generation task.
EOF
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "help" ]; then
        usage
        return 0
    fi
    echo "error: legacy team batch generation is disabled by the evaluation-integrity gate" >&2
    echo "use python -m opencollab_eval.commands.swe_v1_prolite_runner for current batches" >&2
    return 125
}

main "$@"
