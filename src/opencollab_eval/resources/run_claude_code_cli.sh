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

mkdir -p "$output_dir"
actual_runtime_id="$(docker image inspect --format '{{.Id}}' "$runtime_image")"
if [[ "$actual_runtime_id" != "$expected_runtime_id" ]]; then
  echo "Claude Code runtime image identity mismatch" >&2
  exit 2
fi
task_image_id="$(docker inspect --format '{{.Image}}' "$container_id")"
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
test_id=""
runtime_name=""
trusted_git_dir=""
trusted_git_home=""
gateway_id=""
gateway_cidfile="$output_dir/gateway-container.id"
relay_id=""
relay_cidfile="$output_dir/relay-container.id"
test_cidfile="$output_dir/test-container.id"
runtime_cidfile="$output_dir/runtime-container.id"

cleanup() {
  local status=$?
  local workspace_cleanup_failed=0
  trap - EXIT INT TERM
  set +e
  if [[ -n "$test_id" ]]; then
    docker rm -f "$test_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$runtime_name" ]]; then
    docker rm -f "$runtime_name" >/dev/null 2>&1 || true
  fi
  if [[ -n "$relay_id" ]]; then
    docker rm -f "$relay_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$gateway_id" ]]; then
    docker rm -f "$gateway_id" >/dev/null 2>&1 || true
  fi
  docker network rm "$network_name" >/dev/null 2>&1 || true
  if [[ -n "$trusted_git_dir" ]]; then
    rm -rf "$trusted_git_dir"
  fi
  if [[ -n "$trusted_git_home" ]]; then
    rm -rf "$trusted_git_home"
  fi
  rm -rf "$workspace" >/dev/null 2>&1
  if [[ -d "$workspace" ]]; then
    docker run --rm --network none --user 0:0 \
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
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker cp "$container_id:/testbed/." "$workspace"
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
baseline_sha256="$(run_clean_git archive --format=tar "$anonymous_head" | sha256sum | awk '{print $1}')"
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
test_id="$(docker run -d --network none --name "$test_name" --cidfile "$test_cidfile" \
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
docker exec "$test_id" sh -c 'umask 077; : > "$1"; : > "$2"; chmod 600 "$1"; chmod 400 "$2"' -- \
  "$solver_global_config" "$solver_system_config"

docker network create --internal \
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
relay_id="$(docker run -d --name "$relay_name" --cidfile "$relay_cidfile" \
  --label opencollab.owner=claude-code-relay \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --network "$network_name" --network-alias claude-api \
  -e CLAUDE_RELAY_UPSTREAM_UNIX=/control/upstream.sock \
  --mount "type=bind,src=$relay_socket,dst=/control/upstream.sock" \
  --mount "type=bind,src=$relay_script,dst=/control/claude_api_relay.py,readonly" \
  --entrypoint python3 "$runtime_image" /control/claude_api_relay.py)"
for _ in $(seq 1 100); do
  if docker exec "$relay_id" python3 -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1).read()' \
    >/dev/null 2>&1; then
    break
  fi
  sleep 0.05
done
docker exec "$relay_id" python3 -c \
  'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1).read()' \
  >/dev/null
docker run --rm --network "$network_name" \
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
docker_host="${DOCKER_HOST:-}"
docker_socket="${docker_host#unix://}"
if [[ "$docker_socket" == "$docker_host" || -z "$docker_host" ]]; then
  docker_socket="/var/run/docker.sock"
fi
gateway_name="oc-claude-gateway-${container_id:0:12}-$$"
gateway_id="$(docker run -d --name "$gateway_name" --cidfile "$gateway_cidfile" \
  --label opencollab.owner=claude-code-gateway \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --network "$network_name" --network-alias command-gateway \
  --mount "type=bind,src=$docker_socket,dst=/var/run/docker.sock" \
  --mount "type=bind,src=$gateway_server,dst=/control/claude_container_gateway.py,readonly" \
  --entrypoint python3 "$runtime_image" /control/claude_container_gateway.py \
  --listen 0.0.0.0:8090 --container "$test_id")"
for _ in $(seq 1 100); do
  if docker exec "$gateway_id" python3 -c \
    'import socket; socket.create_connection(("127.0.0.1", 8090), timeout=1).close()' \
    >/dev/null 2>&1; then
    break
  fi
  sleep 0.05
done
docker exec "$gateway_id" python3 -c \
  'import socket; socket.create_connection(("127.0.0.1", 8090), timeout=1).close()' \
  >/dev/null

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
docker run --rm --network none \
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

runtime_identity="$(docker run --rm \
  --label opencollab.owner=claude-code-probe \
  --label "opencollab.solver_task_id=$OPENHANDS_INSTANCE_ID" \
  --entrypoint bash "$runtime_image" -lc \
  'p=$(command -v claude); printf "%s\n" "$p"; sha256sum "$p" | cut -d" " -f1; claude --version')"
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
runtime_name=""
set -e

docker rm -f "$relay_id" >/dev/null
relay_id=""
docker rm -f "$gateway_id" >/dev/null
gateway_id=""
docker rm -f "$test_id" >/dev/null
if docker container inspect "$test_id" >/dev/null 2>&1; then
  echo "test container remained after bounded cleanup" >&2
  exit 125
fi
test_id=""
docker network rm "$network_name" >/dev/null

prompt_sha256="$(sha256sum "$rendered_prompt" | awk '{print $1}')"
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
raw_patch_sha256="$(sha256sum "$patch_file" | awk '{print $1}')"

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
    docker exec "$container_id" rm -rf -- "/testbed/$gitlink_path"
    docker exec "$container_id" mkdir -p -- "/testbed/$gitlink_path"
  done < "$gitlink_replay_paths"
  docker exec -i -w /testbed "$container_id" git apply --binary --whitespace=nowarn - \
    < "$patch_file"
fi
docker exec -w /testbed "$container_id" git status --short \
  > "$output_dir/container.git-status.txt"
