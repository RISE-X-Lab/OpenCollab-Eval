#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 PATH_TO_OPENCOLLAB_WHEEL PATH_TO_OPENCOLLAB_EVAL_WHEEL" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export OPENCOLLAB_EVAL_SOURCE_ROOT="${OPENCOLLAB_EVAL_SOURCE_ROOT:-$repo_root}"
if [[ -z "${OPENCOLLAB_SOURCE_ROOT:-}" ]]; then
  for candidate in "$repo_root/../OpenCollab" "$repo_root/../opencollab-source"; do
    if [[ -f "$candidate/opencollab/pyproject.toml" ]]; then
      OPENCOLLAB_SOURCE_ROOT="$(cd "$candidate" && pwd)"
      break
    fi
  done
fi
if [[ -z "${OPENCOLLAB_SOURCE_ROOT:-}" ]]; then
  echo "OpenCollab source checkout is required for integrity coverage validation" >&2
  exit 2
fi
export OPENCOLLAB_SOURCE_ROOT
venv_dir="$(mktemp -d "${TMPDIR:-/tmp}/opencollab-eval-wheel-test.XXXXXX")"
trap 'rm -rf "$venv_dir"' EXIT

python3 -m venv "$venv_dir"
"$venv_dir/bin/pip" install "$1" "${2}[swebench]" pytest pytest-asyncio
site_packages="$($venv_dir/bin/python -c 'import site; print(site.getsitepackages()[0])')"
cp -R "$repo_root/tests" "$venv_dir/eval-tests"

(
  cd "$venv_dir"
  PATH="$venv_dir/bin:$PATH" \
  OPENCOLLAB_EXPECTED_WHEEL_ROOT="$site_packages" \
    OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT="$site_packages" \
    PYTHONPATH="$venv_dir/eval-tests" \
    "$venv_dir/bin/pytest" -q -p no:cacheprovider -c /dev/null -o asyncio_mode=auto \
      --import-mode=importlib "$venv_dir/eval-tests"
)

(
  cd "$venv_dir"
  "$venv_dir/bin/python" -I -c \
    "import opencollab, opencollab.sdk, opencollab_eval; version = tuple(map(int, opencollab.__version__.split('.'))); assert (0, 3) <= version < (0, 4)"
  "$venv_dir/bin/python" -I -m opencollab_eval --help >/dev/null
  "$venv_dir/bin/oc-eval" --help >/dev/null
)
