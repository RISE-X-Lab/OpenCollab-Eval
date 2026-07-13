#!/usr/bin/env bash
set -euo pipefail

python_bin="${OPENCOLLAB_EVAL_PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$python_bin" ] || {
    echo "python3/python is required for process-session ownership" >&2
    exit 127
}
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
guard_source="$script_dir/../commands/container_process_guard.py"
[ -f "$guard_source" ] || {
    echo "container process guard source is missing" >&2
    exit 125
}
exec "$python_bin" "$guard_source" "$@"
