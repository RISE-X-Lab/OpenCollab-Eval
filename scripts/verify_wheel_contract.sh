#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 PATH_TO_OPENCOLLAB_WHEEL PATH_TO_OPENCOLLAB_EVAL_WHEEL" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$(mktemp -d "${TMPDIR:-/tmp}/opencollab-eval-wheel-test.XXXXXX")"
trap 'rm -rf "$venv_dir"' EXIT

python3 -m venv "$venv_dir"
"$venv_dir/bin/pip" install "$1" "$2" pytest pytest-asyncio
site_packages="$($venv_dir/bin/python -c 'import site; print(site.getsitepackages()[0])')"

(
  cd "$repo_root"
  OPENCOLLAB_EXPECTED_WHEEL_ROOT="$site_packages" \
    PYTHONPATH="$repo_root/src" \
    "$venv_dir/bin/pytest" -q tests
)

(
  cd "$venv_dir"
  "$venv_dir/bin/python" -c \
    "import opencollab, opencollab.sdk, opencollab_eval; assert opencollab.__version__ == '0.2.0'"
  "$venv_dir/bin/python" -m opencollab_eval --help >/dev/null
)
