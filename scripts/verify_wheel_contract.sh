#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 PATH_TO_OPENCOLLAB_WHEEL PATH_TO_OPENCOLLAB_EVAL_WHEEL"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 2 ]]; then
  usage >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
export OPENCOLLAB_EVAL_SOURCE_ROOT="${OPENCOLLAB_EVAL_SOURCE_ROOT:-$repo_root}"
if [[ -z "${OPENCOLLAB_SOURCE_ROOT:-}" ]]; then
  for candidate in "$repo_root/../OpenCollab" "$repo_root/../opencollab-source"; do
    if [[ -f "$candidate/pyproject.toml" && -d "$candidate/opencollab" ]]; then
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
tmp_root="$($python_bin -c 'import os; print(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"))')"
venv_dir="$(mktemp -d "$tmp_root/opencollab-eval-wheel-test.XXXXXX")"
trap 'rm -rf "$venv_dir"' EXIT

"$python_bin" -m venv "$venv_dir"
chmod 755 "$venv_dir"
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
      --import-mode=importlib \
      --ignore="$venv_dir/eval-tests/test_conventional_title_check.py" \
      --ignore="$venv_dir/eval-tests/test_hygiene_check.py" \
      --ignore="$venv_dir/eval-tests/test_public_readiness.py" \
      --ignore="$venv_dir/eval-tests/test_publication_workflows.py" \
      --ignore="$venv_dir/eval-tests/test_release_metadata.py" \
      --ignore="$venv_dir/eval-tests/test_secret_history_check.py" \
      "$venv_dir/eval-tests"
)

(
  cd "$venv_dir"
  "$venv_dir/bin/python" -I - <<'PYCODE'
from importlib.metadata import requires, version
from packaging.requirements import Requirement
import opencollab
import opencollab.environments
import opencollab.tools
import opencollab.workflows
import opencollab_eval

paired = next(Requirement(item) for item in requires("opencollab-eval")
              if Requirement(item).name == "opencollab")
assert version("opencollab") in paired.specifier
assert opencollab.__version__ == version("opencollab")
assert opencollab_eval.__version__ == version("opencollab-eval")
PYCODE
  "$venv_dir/bin/python" -I -m opencollab_eval --help >/dev/null
  "$venv_dir/bin/oc-eval" --help >/dev/null
)
