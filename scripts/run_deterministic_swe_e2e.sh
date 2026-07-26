#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --output DIRECTORY [--runs COUNT]"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
opencollab_root="${OPENCOLLAB_SOURCE_ROOT:-$repo_root/../OpenCollab}"
if [[ -f "$opencollab_root/opencollab/pyproject.toml" ]]; then
  opencollab_project="$opencollab_root/opencollab"
elif [[ -f "$opencollab_root/pyproject.toml" ]]; then
  opencollab_project="$opencollab_root"
else
  echo "OpenCollab SDK source checkout is required" >&2
  exit 2
fi

output=""
runs=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --runs) runs="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$output" || ! "$runs" =~ ^[1-9][0-9]*$ ]]; then
  usage >&2
  exit 2
fi

output="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$output")"
mkdir -p "$output"
if [[ -n "$(find "$output" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "output directory must be empty: $output" >&2
  exit 2
fi
tmp_root="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/opencollab-deterministic-e2e.XXXXXX")"
sshd_pid=""
active_run_id=""
cleanup() {
  status=$?
  if [[ -n "$active_run_id" ]]; then
    artifact="$output/$active_run_id"
    if [[ -f "$artifact/fake-model.pid" ]]; then
      fake_pid="$(tr -cd '0-9' < "$artifact/fake-model.pid")"
      if [[ -n "$fake_pid" ]]; then kill -TERM "$fake_pid" 2>/dev/null || true; fi
    fi
    while read -r container_id; do
      if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null 2>&1 || true; fi
    done < <(docker ps -aq --filter "label=opencollab.eval.deterministic-e2e=$active_run_id" 2>/dev/null || true)
    while read -r image_id; do
      if [[ -n "$image_id" ]]; then docker image rm -f "$image_id" >/dev/null 2>&1 || true; fi
    done < <(docker image ls -q --filter "label=opencollab.eval.deterministic-e2e=$active_run_id" 2>/dev/null || true)
    if [[ -d "$artifact/work" ]]; then rm -rf -- "$artifact/work"; fi
  fi
  if [[ -n "$sshd_pid" ]] && kill -0 "$sshd_pid" 2>/dev/null; then
    kill "$sshd_pid" 2>/dev/null || true
    wait "$sshd_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_root"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

dist="$tmp_root/dist"
venv="$tmp_root/venv"
mkdir -p "$dist"
if [[ -n "${OPENCOLLAB_E2E_OPENCOLLAB_WHEEL:-}" && -n "${OPENCOLLAB_E2E_EVAL_WHEEL:-}" ]]; then
  opencollab_wheel="$OPENCOLLAB_E2E_OPENCOLLAB_WHEEL"
  eval_wheel="$OPENCOLLAB_E2E_EVAL_WHEEL"
else
  UV_CACHE_DIR="${UV_CACHE_DIR:-$tmp_root/uv-cache}" uv build --wheel --out-dir "$dist" "$opencollab_project"
  UV_CACHE_DIR="${UV_CACHE_DIR:-$tmp_root/uv-cache}" uv build --wheel --out-dir "$dist" "$repo_root"
  opencollab_wheel="$(find "$dist" -maxdepth 1 -name 'opencollab-*.whl' ! -name 'opencollab_eval-*' -print -quit)"
  eval_wheel="$(find "$dist" -maxdepth 1 -name 'opencollab_eval-*.whl' -print -quit)"
fi
python3 -m venv "$venv"
if [[ -z "$opencollab_wheel" || -z "$eval_wheel" ]]; then
  echo "wheel build did not produce both distributions" >&2
  exit 1
fi
"$venv/bin/pip" install --quiet "$opencollab_wheel" "${eval_wheel}[swebench]"

ssh_dir="$tmp_root/ssh"
mkdir -p "$ssh_dir"
chmod 700 "$ssh_dir"
ssh-keygen -q -t ed25519 -N '' -f "$ssh_dir/host_key"
ssh-keygen -q -t ed25519 -N '' -f "$ssh_dir/client_key"
cp "$ssh_dir/client_key.pub" "$ssh_dir/authorized_keys"
chmod 600 "$ssh_dir/authorized_keys" "$ssh_dir/client_key"
ssh_port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
sshd_config="$ssh_dir/sshd_config"
python3 -c 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])' \
  "$sshd_config" \
  "Port $ssh_port
ListenAddress 127.0.0.1
HostKey $ssh_dir/host_key
PidFile $ssh_dir/sshd.pid
AuthorizedKeysFile $ssh_dir/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM no
StrictModes no
AllowTcpForwarding yes
LogLevel VERBOSE
"
sshd_bin="$(command -v sshd)"
"$sshd_bin" -D -e -f "$sshd_config" >"$output/sshd.log" 2>&1 &
sshd_pid=$!
ssh_command="ssh -p $ssh_port -i $ssh_dir/client_key -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
ssh_host="$(id -un)@127.0.0.1"
ready=0
for _ in $(seq 1 100); do
  if $ssh_command "$ssh_host" true >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$sshd_pid" 2>/dev/null; then
    break
  fi
  sleep 0.05
done
if [[ "$ready" -ne 1 ]]; then
  echo "ephemeral sshd did not become ready" >&2
  exit 1
fi

cd "$repo_root/tests"
"$venv/bin/python" -m e2e.process_watchdog --timeout 300 --grace 30 -- \
  "$venv/bin/python" -m e2e.integrity_docker_smoke \
  --output "$output/integrity-docker-smoke.json"
for index in $(seq 1 "$runs"); do
  run_id="det-e2e-$(date +%s)-$$-$index"
  active_run_id="$run_id"
  env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u DASHSCOPE_API_KEY \
    -u GLM_PROXY_CLIENT_TOKEN -u KIMI_API_KEY -u MOONSHOT_API_KEY -u OPENCOLLAB_API_KEY \
    -u OPENCOLLAB_PROXY_CLIENT_TOKEN -u OPENCOLLAB_READ_TOKEN -u OPENAI_API_KEY \
    "$venv/bin/python" -m e2e.process_watchdog --timeout 600 --grace 30 -- \
      "$venv/bin/python" \
      -m e2e.deterministic_swe_driver \
      --output "$output" --python "$venv/bin/python" --ssh-command "$ssh_command" \
      --ssh-host "$ssh_host" --run-id "$run_id"
  active_run_id=""
done

python3 -c '
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
reports = [json.loads(path.read_text()) for path in sorted(root.glob("det-e2e-*/report.json"))]
validations = [json.loads(path.read_text()) for path in sorted(root.glob("det-e2e-*/validation.json"))]
official = [json.loads(path.read_text()) for path in sorted(root.glob("det-e2e-*/official-eval-proof.json"))]
cleanups = [json.loads(path.read_text()) for path in sorted(root.glob("det-e2e-*/cleanup.json"))]
expected = int(sys.argv[2])
if not all(len(values) == expected for values in (reports, validations, official, cleanups)):
    raise SystemExit("deterministic E2E report set is incomplete")
if any(row.get("counts", {}).get("resolved") != 1 or row.get("counts", {}).get("technical_failed") != 0 for row in reports):
    raise SystemExit("deterministic E2E did not resolve every run")
if any(row.get("resolved") is not True or row.get("collected_tests") != 1 for row in official):
    raise SystemExit("official E2E proof is incomplete")
required_cleanup = ("fake_model_stopped", "owned_containers_removed", "owned_images_removed", "temporary_work_removed", "provider_environment_clean")
if any(not all(row.get(key) is True for key in required_cleanup) for row in cleanups):
    raise SystemExit("deterministic E2E cleanup proof is incomplete")
if len({row["run_id"] for row in validations}) != expected:
    raise SystemExit("deterministic E2E reused a run identity")
if len({row["patch_sha256"] for row in validations}) != 1:
    raise SystemExit("deterministic E2E produced different candidate patches")
(root / "validation.json").write_text(json.dumps({"runs": validations}, indent=2, sort_keys=True) + "\n")
' "$output" "$runs"
