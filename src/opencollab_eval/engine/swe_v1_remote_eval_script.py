"""Container-side script used by the direct Pro-Lite evaluator."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path

from opencollab_eval.engine import (
    eval_candidate_projection,
    eval_runtime_dependencies,
    swe_eval_record_identity,
)
from opencollab_eval.engine import workspace_integrity as workspace_integrity_policy
from opencollab_eval.generation import (
    gen_prediction_snapshot_container,
    gen_prediction_snapshot_support,
    public_preparation_runner,
)

DIRECT_EVAL_SCRIPT = r"""#!/usr/bin/env bash
set +e
repo_root=""
for candidate in /app /testbed /workspace /repo /src; do
  if [ -d "$candidate/.git" ] && [ ! -L "$candidate/.git" ]; then repo_root="$candidate"; break; fi
done
if [ -n "$repo_root" ]; then cd "$repo_root"; else cd /; fi
export GIT_ATTR_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/tmp/opencollab-eval-global.gitconfig GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/tmp/opencollab-eval-system.gitconfig GIT_NO_REPLACE_OBJECTS=1
: > "$GIT_CONFIG_GLOBAL"
: > "$GIT_CONFIG_SYSTEM"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$repo_root"
export PATH="/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:/root/go/bin:/usr/local/node/bin:/opt/node/bin:/root/.local/share/pnpm:/root/.npm-global/bin:$repo_root/node_modules/.bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1 && command -v corepack >/dev/null 2>&1; then
  corepack enable >/tmp/prolite_corepack.log 2>&1 || true
fi

expected_base_commit="$(cat /eval_input/base_commit)"
: > /eval_output/base_commit.log
: > /eval_output/base_snapshot.json
base_commit_status=0
baseline_head=""
if [ -z "$repo_root" ]; then
  echo "missing supported evaluation repository" >> /eval_output/base_commit.log
  base_commit_status=1
elif [ -z "$expected_base_commit" ]; then
  echo "missing expected base commit" >> /eval_output/base_commit.log
  base_commit_status=1
elif ! git cat-file -e "$expected_base_commit^{commit}" >> /eval_output/base_commit.log 2>&1; then
  base_commit_status=1
fi

runtime_store="$(dirname "$repo_root")/.opencollab-eval-runtime-dependencies"
if [ "$base_commit_status" -eq 0 ]; then
  python3 /eval_input/eval_runtime_dependencies.py stash "$repo_root" \
    /eval_input/runtime_dependency_specs.json "$runtime_store" >> /eval_output/base_commit.log 2>&1
  base_commit_status=$?
fi

source_projection_status=99
if [ "$base_commit_status" -eq 0 ]; then
  python3 /eval_input/eval_candidate_projection.py source \
    --repo "$repo_root" --base-commit "$expected_base_commit" \
    --declared-base-commit "$expected_base_commit" \
    --patch /eval_input/model.patch --expectation /eval_input/candidate_expectation.json \
    --output /eval_output/source_candidate_projection.json \
    --failure-output /eval_output/candidate_projection_failure.json \
    > /eval_output/model_patch.log 2>&1
  source_projection_status=$?
fi

if [ "$base_commit_status" -eq 0 ]; then
  cd /tmp
  python3 /eval_input/gen_prediction_snapshot_container.py --prepare-public-input "$repo_root" \
    < /eval_input/base_commit > /eval_output/preparation_input_snapshot.json 2>> /eval_output/base_commit.log
  base_commit_status=$?
  cd "$repo_root"
  if [ "$base_commit_status" -eq 0 ]; then
    baseline_head="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
    if [ "$baseline_head" != "$expected_base_commit" ] || [ -n "$(git status --porcelain --untracked-files=no --ignore-submodules=all 2>> /eval_output/base_commit.log)" ]; then
      base_commit_status=1
    fi
  fi
fi

if [ "$base_commit_status" -eq 0 ]; then
  python3 /eval_input/eval_runtime_dependencies.py restore "$repo_root" "$runtime_store" \
    /tmp/opencollab-preparation-runtime.json >> /eval_output/base_commit.log 2>&1
  base_commit_status=$?
fi

before_repo_status=99
if [ "$base_commit_status" -eq 0 ]; then
  python3 /eval_input/public_preparation_runner.py /eval_input/before_repo.sh \
    /eval_output/before_repo.log "$repo_root"
  before_repo_status=$?
fi
echo "$before_repo_status" > /eval_output/before_repo.exit

post_before_base_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ]; then
  actual_after_before="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
  if [ "$actual_after_before" = "$baseline_head" ]; then
    post_before_base_status=0
  else
    echo "before-repository command changed HEAD to $actual_after_before" >> /eval_output/base_commit.log
    post_before_base_status=1
  fi
fi
echo "$post_before_base_status" > /eval_output/post_before_base.exit

if [ "$base_commit_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ]; then
  python3 /eval_input/eval_runtime_dependencies.py stash "$repo_root" \
    /eval_input/runtime_dependency_specs.json "$runtime_store" >> /eval_output/base_commit.log 2>&1
  base_commit_status=$?
fi

if [ "$base_commit_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ]; then
  cd /tmp
  python3 /eval_input/gen_prediction_snapshot_container.py --public-preparation "$repo_root" \
    < /eval_input/base_commit > /eval_output/post_preparation_snapshot.json 2>> /eval_output/base_commit.log
  base_commit_status=$?
  cd "$repo_root"
  if [ "$base_commit_status" -eq 0 ]; then
    baseline_head="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
    if [ -z "$baseline_head" ] || [ "$(git rev-list --all --count 2>> /eval_output/base_commit.log)" != "1" ] || [ -n "$(git remote 2>> /eval_output/base_commit.log)" ] || [ -n "$(git status --porcelain --untracked-files=no 2>> /eval_output/base_commit.log)" ]; then
      base_commit_status=1
    fi
  fi
  if [ "$base_commit_status" -eq 0 ]; then
    python3 -c 'import json; p=json.load(open("/eval_output/preparation_input_snapshot.json")); q=json.load(open("/eval_output/post_preparation_snapshot.json")); q["preparation_input_snapshot"]=p; json.dump(q,open("/eval_output/base_snapshot.json","w"),sort_keys=True)' >> /eval_output/base_commit.log 2>&1
    base_commit_status=$?
  fi
fi
echo "$base_commit_status" > /eval_output/base_commit.exit

apply_patch_with_fallback() {
  local patch_file="$1"
  local log_file="$2"
  local apply_mode="${3:-strict}"
  local existing_mode="${4:-reject_already_applied}"
  local git_apply_args=(--whitespace=nowarn)
  if [ ! -s "$patch_file" ]; then return 0; fi
  if [ "$apply_mode" = "ignore-space-change" ]; then
    git_apply_args+=(--ignore-space-change)
  fi
  git apply "${git_apply_args[@]}" "$patch_file" > "$log_file" 2>&1
  local status=$?
  if [ "$status" -eq 0 ]; then return 0; fi
  if [ "$existing_mode" = "verify_already_applied" ] && git apply --reverse --check "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1; then
    echo "verified test patch already applied; workspace left unchanged" >> "$log_file"
    return 0
  fi
  if git apply --check --3way "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1; then
    git apply --3way "${git_apply_args[@]}" "$patch_file" >> "$log_file" 2>&1
    status=$?
    if [ "$status" -eq 0 ]; then return 0; fi
  fi
  if command -v patch >/dev/null 2>&1; then
    local patch_args=(--batch --forward -p1)
    if [ "$apply_mode" = "ignore-space-change" ]; then patch_args+=(-l); fi
    if patch "${patch_args[@]}" --dry-run < "$patch_file" >> "$log_file" 2>&1; then
      patch "${patch_args[@]}" < "$patch_file" >> "$log_file" 2>&1
      status=$?
    else
      status=1
    fi
    if grep -Eiq 'Reversed \(or previously applied\) patch detected|Assuming -R' "$log_file"; then
      echo "patch fallback rejected reversed or previously applied patch" >> "$log_file"
      return 1
    fi
  fi
  return "$status"
}

apply_model_patch_strict() {
  git apply --whitespace=nowarn /eval_input/model.patch > /eval_output/model_patch.log 2>&1
}

model_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ]; then
  model_status=$source_projection_status
  if [ "$model_status" -eq 0 ]; then
    python3 /eval_input/eval_candidate_projection.py prepared \
      --repo "$repo_root" --base-commit "$baseline_head" \
      --patch /eval_input/model.patch --expectation /eval_input/candidate_expectation.json \
      --source-projection /eval_output/source_candidate_projection.json \
      --output /eval_output/candidate_projection.json \
      --failure-output /eval_output/candidate_projection_failure.json \
      >> /eval_output/model_patch.log 2>&1
    model_status=$?
  fi
  if [ "$model_status" -eq 0 ]; then apply_model_patch_strict; model_status=$?; fi
  if [ "$model_status" -eq 0 ]; then
    python3 /eval_input/eval_candidate_projection.py verify-worktree \
      --repo "$repo_root" --patch /eval_input/model.patch \
      --projection /eval_output/candidate_projection.json >> /eval_output/model_patch.log 2>&1
    model_status=$?
  fi
fi
echo "$model_status" > /eval_output/model_patch.exit

if [ "$base_commit_status" -eq 0 ] && [ "$model_status" -eq 0 ]; then
  python3 /eval_input/eval_runtime_dependencies.py restore "$repo_root" "$runtime_store" \
    /eval_output/runtime_dependencies.json >> /eval_output/base_commit.log 2>&1
  base_commit_status=$?
  echo "$base_commit_status" > /eval_output/base_commit.exit
fi

service_bootstrap_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$model_status" -eq 0 ]; then
  bash /eval_input/service_bootstrap.sh > /eval_output/service_bootstrap.log 2>&1
  service_bootstrap_status=$?
fi
echo "$service_bootstrap_status" > /eval_output/service_bootstrap.exit

test_status=99
if [ "$model_status" -eq 0 ]; then
  test_status=0
  if [ -s /eval_input/test.patch ]; then
    apply_patch_with_fallback /eval_input/test.patch /eval_output/test_patch.log ignore-space-change verify_already_applied
    test_status=$?
  fi
fi
echo "$test_status" > /eval_output/test_patch.exit

if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$service_bootstrap_status" -eq 0 ] && [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
  cp /eval_input/f2p.command /eval_output/f2p.command
  chmod 0644 /eval_output/f2p.command 2>/dev/null || true
  cp /eval_input/p2p.command /eval_output/p2p.command
  chmod 0644 /eval_output/p2p.command 2>/dev/null || true
  # The host passes the configured per-task budget into the container.  Start
  # one absolute monotonic deadline immediately before f2p and let both phase
  # scripts consume its remaining time.  If the variable is absent (for
  # compatibility with older manually assembled inputs), each script keeps
  # its historical standalone timeout behavior.
  eval_deadline_status=0
  if [ -n "${OPENCOLLAB_EVAL_TIMEOUT_SECONDS:-}" ]; then
    if ! eval_deadline="$(python3 -c 'import math,sys,time; v=float(sys.argv[1]); sys.exit(2) if not math.isfinite(v) or v <= 0 else print(time.monotonic()+v)' "$OPENCOLLAB_EVAL_TIMEOUT_SECONDS" 2>/dev/null)" || [ -z "$eval_deadline" ]; then
      eval_deadline_status=1
      echo "invalid OPENCOLLAB_EVAL_TIMEOUT_SECONDS" > /eval_output/f2p.log
      echo "invalid OPENCOLLAB_EVAL_TIMEOUT_SECONDS" > /eval_output/p2p.log
    else
      export OPENCOLLAB_EVAL_DEADLINE="$eval_deadline"
    fi
  fi
  if [ "$eval_deadline_status" -eq 0 ]; then
    bash /eval_input/f2p.sh > /eval_output/f2p.log 2>&1
    f2p_status=$?
    echo "$f2p_status" > /eval_output/f2p.exit
    # 125 means the phase could not prove its process-group cleanup.  Starting
    # the second phase in that state would run it concurrently with an
    # unowned descendant and contaminate the same checkout; fail closed and
    # leave a durable status artifact instead.
    if [ "$f2p_status" -eq 125 ]; then
      p2p_status=125
      echo "f2p cleanup was not proven; p2p was not started" > /eval_output/p2p.log
    else
      bash /eval_input/p2p.sh > /eval_output/p2p.log 2>&1
      p2p_status=$?
    fi
    echo "$p2p_status" > /eval_output/p2p.exit
  else
    echo 124 > /eval_output/f2p.exit
    echo 124 > /eval_output/p2p.exit
  fi
else
  echo 99 > /eval_output/f2p.exit
  echo 99 > /eval_output/p2p.exit
fi
exit 0
"""


def direct_eval_script() -> str:
    """Return the immutable container-side evaluation program."""
    return DIRECT_EVAL_SCRIPT


def eval_workspace_helper_sources() -> dict[str, bytes]:
    """Return standalone sources used to rebuild an eval workspace."""
    return {
        "opencollab_workspace_integrity.py": Path(workspace_integrity_policy.__file__).read_bytes(),
        "opencollab_snapshot_support.py": Path(gen_prediction_snapshot_support.__file__).read_bytes(),
        "gen_prediction_snapshot_container.py": Path(gen_prediction_snapshot_container.__file__).read_bytes(),
        "public_preparation_runner.py": Path(public_preparation_runner.__file__).read_bytes(),
        "eval_runtime_dependencies.py": Path(eval_runtime_dependencies.__file__).read_bytes(),
        "eval_candidate_projection.py": Path(eval_candidate_projection.__file__).read_bytes(),
        "swe_eval_record_identity.py": Path(swe_eval_record_identity.__file__).read_bytes(),
    }


__all__ = ["DIRECT_EVAL_SCRIPT", "direct_eval_script", "eval_workspace_helper_sources"]
