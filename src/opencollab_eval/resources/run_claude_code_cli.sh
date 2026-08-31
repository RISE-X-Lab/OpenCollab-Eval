#!/usr/bin/env bash
set -euo pipefail

container_id="${1:?missing task container id}"
prompt_file="${2:?missing prompt file}"
output_dir="${3:?missing output directory}"
python_bin="${OPENCOLLAB_CLAUDE_SIDECAR_PYTHON:-python3}"
expected_model="${OPENCOLLAB_CLAUDE_EXPECTED_MODEL:?missing expected Claude model identity}"
expected_version="${OPENCOLLAB_CLAUDE_EXPECTED_VERSION:?missing expected Claude Code version}"
runtime_image="${OPENCOLLAB_CLAUDE_RUNTIME_IMAGE:?missing Claude runtime image}"
expected_runtime_id="${OPENCOLLAB_CLAUDE_RUNTIME_IMAGE_ID:?missing Claude runtime image identity}"
max_patch_bytes=$((8 * 1024 * 1024))
max_file_bytes=$((2 * 1024 * 1024 * 1024))
max_census_bytes=$((4 * 1024 * 1024 * 1024))
max_census_entries=1000000

if [[ -z "${LLM_API_KEY:-}" || -z "${LLM_BASE_URL:-}" ]]; then
  echo "Claude Code requires LLM_API_KEY and LLM_BASE_URL" >&2
  exit 2
fi
if [[ "${LLM_MODEL:-}" != "$expected_model" ]]; then
  echo "Claude Code requires LLM_MODEL=$expected_model" >&2
  exit 2
fi
if [[ ! "$expected_runtime_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Claude runtime image identity must be an immutable sha256 digest" >&2
  exit 2
fi
docker_host="${DOCKER_HOST:-}"
if [[ -z "$docker_host" ]]; then
  docker_socket="/var/run/docker.sock"
elif [[ "$docker_host" == unix:///* ]]; then
  docker_socket="${docker_host#unix://}"
else
  echo "Claude Code requires DOCKER_HOST to be empty or unix://<absolute socket path>" >&2
  exit 2
fi

# Docker CLI requests can otherwise wait forever when the daemon/socket is
# wedged.  Keep this bound for short-lived control calls only; the streaming
# Claude runtime invocation below intentionally remains an unwrapped `docker
# run -i` so its stdin/stdout semantics are unchanged.  The existing
# DOCKER_CLIENT_TIMEOUT is accepted as the compatibility default, while the
# Claude-specific variable lets callers choose a shorter/longer control bound.
docker_control_timeout="${OPENCOLLAB_CLAUDE_DOCKER_CONTROL_TIMEOUT_SECONDS:-${DOCKER_CLIENT_TIMEOUT:-120}}"
docker_health_timeout="${OPENCOLLAB_CLAUDE_DOCKER_HEALTH_TIMEOUT_SECONDS:-5}"
docker_health_retry_budget="${OPENCOLLAB_CLAUDE_DOCKER_HEALTH_RETRY_BUDGET_SECONDS:-30}"
# This is an internal per-health-loop deadline; never inherit a caller's
# stale value into ordinary control requests.
unset OPENCOLLAB_DOCKER_DEADLINE_MONOTONIC
validate_positive_timeout() {
  "$python_bin" -c '
import math
import sys

try:
    value = float(sys.argv[1])
except (IndexError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
raise SystemExit(0 if math.isfinite(value) and value > 0 else 1)
' "$1"
}

# macOS ships `shasum` rather than GNU `sha256sum`.  Hashing is part of the
# evidence contract, so a missing convenience utility must not turn an
# otherwise valid run into a technical failure.  Keep a Python fallback for
# minimal hosts (the sidecar interpreter is already required above).
sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    "$python_bin" -c '
import hashlib
import sys
h = hashlib.sha256()
for chunk in iter(lambda: sys.stdin.buffer.read(1024 * 1024), b""):
    h.update(chunk)
print(h.hexdigest())
'
  fi
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    "$python_bin" -c '
import hashlib
import sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
' "$path"
  fi
}
if ! validate_positive_timeout "$docker_control_timeout"; then
  echo "Claude Code Docker control timeout must be finite and positive" >&2
  exit 2
fi
if ! validate_positive_timeout "$docker_health_timeout"; then
  echo "Claude Code Docker health timeout must be finite and positive" >&2
  exit 2
fi
if ! "$python_bin" -c '
import math
import sys

try:
    value = float(sys.argv[1])
except (IndexError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
raise SystemExit(0 if math.isfinite(value) and 0 < value <= 5 else 1)
' "$docker_health_timeout"; then
  echo "Claude Code Docker health timeout must be at most five seconds" >&2
  exit 2
fi
if ! validate_positive_timeout "$docker_health_retry_budget"; then
  echo "Claude Code Docker health retry budget must be finite and positive" >&2
  exit 2
fi
if ! "$python_bin" -c '
import math
import sys

try:
    value = float(sys.argv[1])
except (IndexError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
raise SystemExit(0 if math.isfinite(value) and 0 < value <= 30 else 1)
' "$docker_health_retry_budget"; then
  echo "Claude Code Docker health retry budget must be at most thirty seconds" >&2
  exit 2
fi
# Keep the Claude sidecar's upstream socket deadline aligned with the host
# relay. The hard cap prevents an untrusted environment value from creating
# an unbounded socket wait; 21,600 seconds is the largest model timeout
# accepted by the current runner contract.
relay_upstream_timeout_max=$((6 * 60 * 60 + 60))
relay_upstream_timeout="$("$python_bin" - \
  "${OPENCOLLAB_LLM_TIMEOUT:-600}" \
  "${OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT:-180}" \
  "${OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT:-180}" \
  "$relay_upstream_timeout_max" <<'PY'
import math
import sys

try:
    llm_timeout, first_event, stream_idle, maximum = (
        float(value) for value in sys.argv[1:5]
    )
except (IndexError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
values = (llm_timeout, first_event, stream_idle, maximum)
if any(not math.isfinite(value) or value <= 0 for value in values):
    raise SystemExit(1)
derived = min(llm_timeout, max(first_event, stream_idle)) + 60.0
if not math.isfinite(derived) or derived > maximum:
    raise SystemExit(1)
print(f"{derived:.9f}".rstrip("0").rstrip("."))
PY
)" || {
  echo "Claude relay upstream timeout settings must be finite, positive, and bounded" >&2
  exit 2
}
docker_control_with_timeout() {
  local timeout="$1"
  shift
  "$python_bin" -c '
import math
import os
import signal
import subprocess
import sys
import time

timeout = float(sys.argv[1])
deadline_raw = os.environ.get("OPENCOLLAB_DOCKER_DEADLINE_MONOTONIC")
if deadline_raw is not None:
    try:
        deadline = float(deadline_raw)
    except (TypeError, ValueError, OverflowError):
        # Keep watchdog/launcher failures distinct from Docker CLI status 125.
        # Docker uses 125 for daemon/runtime errors; the bounded health loop
        # may safely retry it.
        raise SystemExit(251)
    if not math.isfinite(deadline):
        raise SystemExit(251)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # Private status used by the retry loop to distinguish an exhausted
        # total budget from one probe timing out while budget remains.
        raise SystemExit(122)
    timeout = min(timeout, remaining)
command = ["docker", *sys.argv[2:]]
try:
    process = subprocess.Popen(command, start_new_session=True)
except OSError as exc:
    print(f"could not start Docker control command: {exc}", file=sys.stderr)
    raise SystemExit(127)


def kill_and_reap() -> bool:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    try:
        process.wait(timeout=min(5.0, max(0.1, timeout)))
    except (OSError, ChildProcessError, subprocess.TimeoutExpired):
        return False
    return True


def forward_signal(signum, _frame) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except OSError:
        raise SystemExit(251)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        if not kill_and_reap():
            raise SystemExit(251)
    except (OSError, ChildProcessError):
        raise SystemExit(251)
    raise SystemExit(128 + signum)


signal.signal(signal.SIGINT, forward_signal)
signal.signal(signal.SIGTERM, forward_signal)
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    if not kill_and_reap():
        print("could not reap timed-out Docker control command", file=sys.stderr)
        raise SystemExit(251)
    print("Docker control command timed out", file=sys.stderr)
    raise SystemExit(124)
if returncode < 0:
    returncode = 128 - returncode
raise SystemExit(returncode)
' "$timeout" "$@"
}

docker_control() {
  docker_control_with_timeout "$docker_control_timeout" "$@"
}

wait_for_docker_health() {
  local reference="$1"
  local probe="$2"
  (
    local deadline
    deadline="$("$python_bin" -c '
import sys
import time

try:
    budget = float(sys.argv[1])
except (IndexError, TypeError, ValueError, OverflowError):
    raise SystemExit(1)
print(time.monotonic() + budget)
' "$docker_health_retry_budget")" || exit 2
    export OPENCOLLAB_DOCKER_DEADLINE_MONOTONIC="$deadline"
    while :; do
      local status=0
      if docker_control_with_timeout "$docker_health_timeout" exec "$reference" python3 -c "$probe" \
        >/dev/null 2>&1; then
        exit 0
      else
        status=$?
      fi
      if [[ "$status" -eq 122 ]]; then
        break
      fi
      if [[ "$status" -eq 251 ]]; then
        echo "Docker health probe process could not be reaped" >&2
        exit 125
      fi
      sleep 0.05
    done
    echo "Docker health probe did not become ready within its bounded retry budget" >&2
    exit 124
  )
}

# Initialize cleanup state before the first Docker call.  A daemon timeout can
# happen during image inspection, before the normal setup assignments below;
# nounset-safe empty values keep the EXIT trap from masking that failure.
actual_runtime_id=""
network_name=""
workspace=""
test_id=""
test_name=""
runtime_name=""
trusted_git_dir=""
trusted_git_home=""
gateway_id=""
gateway_name=""
relay_id=""
relay_name=""
gateway_cidfile=""
relay_cidfile=""
test_cidfile=""
runtime_cidfile=""

mkdir -p "$output_dir"
actual_runtime_id="$(docker_control image inspect --format '{{.Id}}' "$runtime_image")"
if [[ "$actual_runtime_id" != "$expected_runtime_id" ]]; then
  echo "Claude Code runtime image identity mismatch" >&2
  exit 2
fi
task_image_id="$(docker_control inspect --format '{{.Image}}' "$container_id")"
if [[ ! "$task_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "task container image identity is invalid" >&2
  exit 2
fi
network_name="oc-claude-net-${OPENHANDS_INSTANCE_ID:?missing anonymous solver task id}"
printf '{"solver":"claude-code","expected_model":"%s","expected_runtime_image_id":"%s","task_image_id":"%s","solver_task_id":"%s","network_name":"%s"}\n' \
  "$expected_model" "$expected_runtime_id" "$task_image_id" \
  "$OPENHANDS_INSTANCE_ID" "$network_name" \
  > "$output_dir/external_solver.required.json"
workspace="$(mktemp -d "$output_dir/claude-workspace.XXXXXX")"
gateway_cidfile="$output_dir/gateway-container.id"
relay_id=""
relay_cidfile="$output_dir/relay-container.id"
test_cidfile="$output_dir/test-container.id"
runtime_cidfile="$output_dir/runtime-container.id"

# A daemon may create a named container and write its cidfile before the
# client-side `docker run -d` call times out or returns an error.  In that
# case command substitution leaves the ID variable empty, so cleanup must use
# the freshly-created cidfile or the deterministic name as a fallback.
cleanup_container_by_id_name_or_cidfile() {
  local id="$1"
  local name="$2"
  local cidfile="$3"
  local reference=""
  local cid=""
  if [[ "$id" =~ ^[0-9a-f]{12,64}$ ]]; then
    reference="$id"
  elif [[ -n "$cidfile" && -f "$cidfile" && ! -L "$cidfile" ]]; then
    cid="$(<"$cidfile")"
    if [[ "$cid" =~ ^[0-9a-f]{12,64}$ ]]; then
      reference="$cid"
    fi
  fi
  if [[ -z "$reference" ]]; then
    reference="$name"
  fi
  [[ -n "$reference" ]] || return 0
  if remove_container_and_prove_absent "$reference"; then
    return 0
  fi
  # If an ID/cidfile was stale or malformed, retry by name.  Names are
  # generated per invocation and therefore remain the safest final fallback.
  if [[ -n "$name" && "$reference" != "$name" ]]; then
    remove_container_and_prove_absent "$name"
    return $?
  fi
  return 1
}

remove_container_and_prove_absent() {
  local reference="$1"
  local inspect_output
  local inspect_status
  local attempt
  [[ -n "$reference" ]] || return 0
  # A daemon can briefly reject rm/inspect while it is restarting.
  for attempt in 1 2 3; do
    docker_control rm -f "$reference" >/dev/null 2>&1 || true
    inspect_output="$(docker_control container inspect "$reference" 2>&1)"
    inspect_status=$?
    if [[ "$inspect_status" -ne 0 &&
          ( "$inspect_output" == *"No such container" ||
            "$inspect_output" == *"No such object" ||
            "$inspect_output" == *"no such container" ||
            "$inspect_output" == *"no such object" ) ]]; then
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then
      sleep 0.2
    fi
  done
  return 1
}

remove_network_and_prove_absent() {
  local network="$1"
  local inspect_output
  local inspect_status
  local attempt
  [[ -n "$network" ]] || return 0
  for attempt in 1 2 3; do
    docker_control network rm "$network" >/dev/null 2>&1 || true
    inspect_output="$(docker_control network inspect "$network" 2>&1)"
    inspect_status=$?
    if [[ "$inspect_status" -ne 0 &&
          ( "$inspect_output" == *"No such network" ||
            "$inspect_output" == *"no such network" ) ]]; then
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then
      sleep 0.2
    fi
  done
  return 1
}

cleanup() {
  local status=$?
  local workspace_cleanup_failed=0
  local container_cleanup_failed=0
  trap - EXIT INT TERM
  set +e
  if ! cleanup_container_by_id_name_or_cidfile "$test_id" "$test_name" "$test_cidfile"; then
    container_cleanup_failed=1
  fi
  if ! cleanup_container_by_id_name_or_cidfile "" "$runtime_name" "$runtime_cidfile"; then
    container_cleanup_failed=1
  fi
  if ! cleanup_container_by_id_name_or_cidfile "$relay_id" "$relay_name" "$relay_cidfile"; then
    container_cleanup_failed=1
  fi
  if ! cleanup_container_by_id_name_or_cidfile "$gateway_id" "$gateway_name" "$gateway_cidfile"; then
    container_cleanup_failed=1
  fi
  if ! remove_network_and_prove_absent "$network_name"; then
    container_cleanup_failed=1
  fi
  if [[ -n "$trusted_git_dir" ]]; then
    rm -rf "$trusted_git_dir"
  fi
  if [[ -n "$trusted_git_home" ]]; then
    rm -rf "$trusted_git_home"
  fi
  rm -rf "$workspace" >/dev/null 2>&1
  if [[ -d "$workspace" ]]; then
    docker_control run --rm --network none --user 0:0 \
      --label opencollab.owner=claude-code-probe \
      --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
      --mount "type=bind,src=$workspace,dst=/cleanup" \
      --entrypoint find "$actual_runtime_id" /cleanup -mindepth 1 -delete \
      >/dev/null 2>&1
    rm -rf "$workspace" >/dev/null 2>&1
  fi
  if [[ -e "$workspace" ]]; then
    workspace_cleanup_failed=125
  fi
  if [[ "$status" -eq 0 && "$workspace_cleanup_failed" -ne 0 ]]; then
    status="$workspace_cleanup_failed"
  fi
  if [[ "$status" -eq 0 && "$container_cleanup_failed" -ne 0 ]]; then
    status=125
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker_control cp "$container_id:/testbed/." "$workspace"
if [[ ! -d "$workspace/.git" || -L "$workspace/.git" ]]; then
  echo "task repository Git metadata is unavailable" >&2
  exit 2
fi
trusted_git_dir="$(mktemp -d "$output_dir/claude-trusted-git.XXXXXX")"
trusted_git_home="$(mktemp -d "$output_dir/claude-trusted-home.XXXXXX")"
cp -R "$workspace/.git/." "$trusted_git_dir/"
mkdir -p "$trusted_git_home/xdg"
run_clean_git() {
  local index_file="${GIT_INDEX_FILE:-$trusted_git_dir/index}"
  env -i PATH="$PATH" HOME="$trusted_git_home" XDG_CONFIG_HOME="$trusted_git_home/xdg" \
    LC_ALL=C GIT_ATTR_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
    GIT_INDEX_FILE="$index_file" \
    git --git-dir="$trusted_git_dir" --work-tree="$workspace" \
    -c core.fsmonitor=false -c core.hooksPath=/dev/null \
    -c core.attributesFile=/dev/null -c diff.external= "$@"
}
anonymous_head="$(run_clean_git rev-parse HEAD)"
base_tree="$(run_clean_git rev-parse HEAD^{tree})"
baseline_sha256="$(run_clean_git archive --format=tar "$anonymous_head" | sha256_stdin)"
gitlink_repository_dir="$output_dir/claude-gitlink-baselines"
gitlink_baseline_manifest="$output_dir/claude-gitlink-baseline.json"
gitlink_projection_manifest="$output_dir/claude-gitlink-projections.json"
"$python_bin" -m opencollab_eval.generation.candidate_gitlinks_cli capture \
  --git-dir "$trusted_git_dir" --work-tree "$workspace" \
  --base "$anonymous_head" --base-tree "$base_tree" \
  --baseline-sha256 "$baseline_sha256" \
  --repository-directory "$gitlink_repository_dir" \
  --output "$gitlink_baseline_manifest"
test_name="oc-claude-${container_id:0:12}-$$"
solver_global_config="/tmp/opencollab-solver-global-$$.gitconfig"
solver_system_config="/tmp/opencollab-solver-system-$$.gitconfig"
test_id="$(docker_control run -d --network none --name "$test_name" --cidfile "$test_cidfile" \
  --label opencollab.owner=claude-code-external \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  -e GIT_ATTR_NOSYSTEM=1 -e "GIT_CONFIG_GLOBAL=$solver_global_config" \
  -e GIT_CONFIG_NOSYSTEM=1 -e "GIT_CONFIG_SYSTEM=$solver_system_config" \
  -e GIT_NO_REPLACE_OBJECTS=1 \
  --mount "type=bind,src=$workspace,dst=/testbed" \
  --mount type=bind,src=/dev/null,dst=/dev/null,readonly \
  --entrypoint '' "$task_image_id" tail -f /dev/null)"
if [[ "$(<"$test_cidfile")" != "$test_id" ]]; then
  echo "test container cidfile identity mismatch" >&2
  exit 2
fi
docker_control exec "$test_id" sh -c 'umask 077; : > "$1"; : > "$2"; chmod 600 "$1"; chmod 400 "$2"' -- \
  "$solver_global_config" "$solver_system_config"

docker_control network create --internal \
  --label opencollab.owner=claude-code-network \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  "$network_name" >/dev/null
relay_script="$output_dir/claude_api_relay.py"
rm -f "$relay_script"
cp "$(dirname "$0")/claude_api_relay.py" "$relay_script"
chmod 500 "$relay_script"
relay_name="oc-claude-relay-${container_id:0:12}-$$"
relay_socket="$($python_bin -m opencollab_eval.generation.claude_code_sidecar \
  relay-socket --base-url "$LLM_BASE_URL")"
relay_id="$(docker_control run -d --name "$relay_name" --cidfile "$relay_cidfile" \
  --label opencollab.owner=claude-code-relay \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --network "$network_name" --network-alias claude-api \
  -e CLAUDE_RELAY_UPSTREAM_UNIX=/control/upstream.sock \
  -e "CLAUDE_RELAY_UPSTREAM_TIMEOUT=$relay_upstream_timeout" \
  --mount "type=bind,src=$relay_socket,dst=/control/upstream.sock" \
  --mount "type=bind,src=$relay_script,dst=/control/claude_api_relay.py,readonly" \
  --entrypoint python3 "$runtime_image" /control/claude_api_relay.py)"
relay_health_probe='import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1).read()'
wait_for_docker_health "$relay_id" "$relay_health_probe"
docker_control run --rm --network "$network_name" \
  --label opencollab.owner=claude-code-probe \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --entrypoint python3 "$runtime_image" -c '
import socket, urllib.request
assert urllib.request.urlopen("http://claude-api:8080/health", timeout=3).read() == b"ok\n"
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1)
except OSError:
    print("api_relay_reachable=true direct_external_blocked=true")
else:
    raise SystemExit("isolated runtime reached an external address")
' > "$output_dir/runtime-network-isolation.proof"

gateway_client="$output_dir/claude_gateway_client.py"
rm -f "$gateway_client"
cp "$(dirname "$0")/claude_gateway_client.py" "$gateway_client"
chmod 500 "$gateway_client"
gateway_server="$output_dir/claude_container_gateway.py"
rm -f "$gateway_server"
cp "$(dirname "$0")/../generation/claude_container_gateway.py" "$gateway_server"
chmod 500 "$gateway_server"
gateway_name="oc-claude-gateway-${container_id:0:12}-$$"
gateway_id="$(docker_control run -d --name "$gateway_name" --cidfile "$gateway_cidfile" \
  --label opencollab.owner=claude-code-gateway \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --network "$network_name" --network-alias command-gateway \
  --mount "type=bind,src=$docker_socket,dst=/var/run/docker.sock" \
  --mount "type=bind,src=$gateway_server,dst=/control/claude_container_gateway.py,readonly" \
  --entrypoint python3 "$runtime_image" /control/claude_container_gateway.py \
  --listen 0.0.0.0:8090 --container "$test_id")"
gateway_health_probe='import socket; socket.create_connection(("127.0.0.1", 8090), timeout=1).close()'
wait_for_docker_health "$gateway_id" "$gateway_health_probe"

run_wrapper="$output_dir/run_in_container"
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
  'exec python3 /control/claude_gateway_client.py command-gateway:8090 "$@"' \
  > "$run_wrapper"
chmod 700 "$run_wrapper"

settings_file="$output_dir/claude.settings.json"
rendered_prompt="$output_dir/claude.prompt.md"
stream_file="$output_dir/claude.stream.jsonl"
sidecar_file="$output_dir/external_solver.sidecar.json"
patch_file="$output_dir/claude.patch"
"$python_bin" -m opencollab_eval.generation.claude_code_sidecar settings \
  --base-url "http://claude-api:8080" --output "$settings_file"
"$python_bin" -m opencollab_eval.generation.claude_code_sidecar prompt \
  --source "$prompt_file" --workspace /workspace --wrapper /control/run_in_container \
  --output "$rendered_prompt"

host_sentinel="$output_dir/host-isolation-sentinel"
printf 'must remain outside Claude runtime\n' > "$host_sentinel"
docker_control run --rm --network none \
  --label opencollab.owner=claude-code-probe \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --mount "type=bind,src=$run_wrapper,dst=/control/run_in_container,readonly" \
  --mount "type=bind,src=$gateway_client,dst=/control/claude_gateway_client.py,readonly" \
  --mount "type=bind,src=$settings_file,dst=/control/claude.settings.json,readonly" \
  --entrypoint bash "$runtime_image" -lc '
    test ! -e "$1"
    test ! -e /output
    if (printf x >> /control/run_in_container) 2>/dev/null; then exit 31; fi
    if (printf x >> /control/claude.settings.json) 2>/dev/null; then exit 32; fi
    test ! -S /var/run/docker.sock
    printf "host_unmounted=true control_read_only=true docker_socket_unmounted=true\n"
  ' -- "$host_sentinel" > "$output_dir/runtime-isolation.proof"
rm -f "$host_sentinel"

runtime_identity="$(docker_control run --rm \
  --label opencollab.owner=claude-code-probe \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --entrypoint bash "$runtime_image" -lc \
  'p=$(command -v claude); printf "%s\n" "$p"; python3 -c "import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],\"rb\"); [h.update(c) for c in iter(lambda: f.read(1048576), b\"\")]; f.close(); print(h.hexdigest())" "$p"; claude --version')"
claude_path="$(printf '%s\n' "$runtime_identity" | sed -n '1p')"
claude_sha256="$(printf '%s\n' "$runtime_identity" | sed -n '2p')"
cli_version_output="$(printf '%s\n' "$runtime_identity" | sed -n '3,$p')"
if [[ "$cli_version_output" != *"$expected_version"* ]]; then
  echo "Claude Code version must be $expected_version" >&2
  exit 2
fi
allowed_tools="Read Edit Write Glob Grep TaskOutput TaskStop Bash(/control/run_in_container *)"
printf '%q ' claude -p --bare --verbose --settings /control/claude.settings.json \
  --model sonnet --effort max --output-format stream-json \
  --permission-mode dontAsk --tools Read,Edit,Write,Glob,Grep,Bash,TaskOutput,TaskStop \
  --allowedTools "$allowed_tools" --disallowedTools "WebFetch WebSearch" \
  --no-session-persistence > "$output_dir/claude.command.txt"
printf '\n' >> "$output_dir/claude.command.txt"

export ANTHROPIC_AUTH_TOKEN="$LLM_API_KEY"
export ANTHROPIC_API_KEY="$LLM_API_KEY"
export ANTHROPIC_BASE_URL="http://claude-api:8080"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
runtime_name="oc-claude-runtime-${container_id:0:12}-$$"
set +e
docker run --rm -i --name "$runtime_name" --cidfile "$runtime_cidfile" --network "$network_name" \
  --label opencollab.owner=claude-code-runtime \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  --mount "type=bind,src=$run_wrapper,dst=/control/run_in_container,readonly" \
  --mount "type=bind,src=$gateway_client,dst=/control/claude_gateway_client.py,readonly" \
  --mount "type=bind,src=$settings_file,dst=/control/claude.settings.json,readonly" \
  --workdir /workspace \
  -e ANTHROPIC_AUTH_TOKEN -e ANTHROPIC_API_KEY -e ANTHROPIC_BASE_URL \
  -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC \
  --entrypoint claude "$runtime_image" \
  -p --bare --verbose --settings /control/claude.settings.json \
  --model sonnet --effort max --output-format stream-json \
  --permission-mode dontAsk \
  --tools Read,Edit,Write,Glob,Grep,Bash,TaskOutput,TaskStop \
  --allowedTools "$allowed_tools" --disallowedTools "WebFetch WebSearch" \
  --no-session-persistence < "$rendered_prompt" > "$stream_file"
claude_returncode=$?
set -e

container_cleanup_failed=0
# The fallback helper subsumes the old remove_container_and_prove_absent "$test_id"
# call while also recovering IDs from cidfiles/names after a daemon timeout.
if cleanup_container_by_id_name_or_cidfile "$relay_id" "$relay_name" "$relay_cidfile"; then
  relay_id=""
else
  container_cleanup_failed=1
fi
if cleanup_container_by_id_name_or_cidfile "$gateway_id" "$gateway_name" "$gateway_cidfile"; then
  gateway_id=""
else
  container_cleanup_failed=1
fi
if cleanup_container_by_id_name_or_cidfile "$test_id" "$test_name" "$test_cidfile"; then
  test_id=""
else
  container_cleanup_failed=1
fi
if remove_network_and_prove_absent "$network_name"; then
  :
else
  container_cleanup_failed=1
fi
if [[ "$claude_returncode" -eq 0 && "$container_cleanup_failed" -ne 0 ]]; then
  echo "external solver container/network cleanup could not be proven" >&2
  exit 125
fi

prompt_sha256="$(sha256_file "$rendered_prompt")"
build_claude_sidecar() {
  "$python_bin" -m opencollab_eval.generation.claude_code_sidecar build \
    --stream "$stream_file" --settings "$settings_file" \
    --executable-path "$claude_path" --executable-sha256 "$claude_sha256" \
    --cli-version-output "$cli_version_output" \
    --process-returncode "$claude_returncode" \
    --runtime-image "$runtime_image" --runtime-image-id "$actual_runtime_id" \
    --expected-runtime-image-id "$expected_runtime_id" \
    --task-image-id "$task_image_id" \
    --solver-task-id "${OPENHANDS_INSTANCE_ID:?missing anonymous solver task id}" \
    --prompt-sha256 "$prompt_sha256" --anonymous-head "$anonymous_head" \
    --base-tree "$base_tree" --raw-patch-sha256 "$1" \
    --candidate-tree "$2" --output "$sidecar_file"
}
set +e
build_claude_sidecar "" ""
pre_candidate_sidecar_returncode=$?
set -e
if [[ ! -s "$sidecar_file" ]]; then
  if [[ "$pre_candidate_sidecar_returncode" -ne 0 ]]; then
    exit "$pre_candidate_sidecar_returncode"
  fi
  exit 2
fi
"$python_bin" -m opencollab_eval.generation.candidate_gitlinks_cli project \
  --manifest "$gitlink_baseline_manifest" --work-tree "$workspace" \
  --git-dir "$trusted_git_dir" \
  --repository-directory "$gitlink_repository_dir" \
  --output "$gitlink_projection_manifest"
candidate_manifest="$output_dir/claude-candidate.json"
"$python_bin" -m opencollab_eval.generation.candidate_patch_cli \
  --git-dir "$trusted_git_dir" --work-tree "$workspace" \
  --base "$anonymous_head" --base-tree "$base_tree" \
  --baseline-sha256 "$baseline_sha256" \
  --gitlink-projections "$gitlink_projection_manifest" \
  --max-patch-bytes "$max_patch_bytes" \
  --max-file-bytes "$max_file_bytes" \
  --max-census-bytes "$max_census_bytes" \
  --max-census-entries "$max_census_entries" \
  --patch-output "$patch_file" --manifest-output "$candidate_manifest" \
  --status-output "$output_dir/claude.git-status.txt"
candidate_tree="$("$python_bin" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["candidate_tree"])' \
  "$candidate_manifest")"
raw_patch_sha256="$(sha256_file "$patch_file")"

set +e
build_claude_sidecar "$raw_patch_sha256" "$candidate_tree"
sidecar_returncode=$?
set -e
if [[ "$sidecar_returncode" -ne 0 ]]; then
  exit "$sidecar_returncode"
fi

if [[ -s "$patch_file" ]]; then
  gitlink_replay_paths="$output_dir/claude-gitlink-replay-paths.bin"
  "$python_bin" -m opencollab_eval.generation.candidate_gitlinks_cli replay-paths \
    --manifest "$gitlink_projection_manifest" --output "$gitlink_replay_paths"
  while IFS= read -r -d '' gitlink_path; do
    docker_control exec "$container_id" rm -rf -- "/testbed/$gitlink_path"
    docker_control exec "$container_id" mkdir -p -- "/testbed/$gitlink_path"
  done < "$gitlink_replay_paths"
  docker_control exec -i -w /testbed "$container_id" git apply --binary --whitespace=nowarn - \
    < "$patch_file"
fi
docker_control exec -w /testbed "$container_id" git \
  -c core.fsmonitor=false -c core.hooksPath=/dev/null \
  -c core.attributesFile=/dev/null -c diff.external= \
  status --short \
  > "$output_dir/container.git-status.txt"
