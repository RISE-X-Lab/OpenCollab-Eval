"""Generation state and ProLite test-command compatibility facade."""

# ruff: noqa: E501, F403, F405

from opencollab_eval.engine.swe_v1_go_failure_proof import *
from opencollab_eval.engine.swe_v1_remote_core import *
from opencollab_eval.engine.swe_v1_remote_go_targets import *
from opencollab_eval.engine.swe_v1_remote_javascript_proof import *
from opencollab_eval.engine.swe_v1_remote_pytest_proof import *
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *
from opencollab_eval.engine.swe_v1_remote_target_proof import *
from opencollab_eval.engine.swe_v1_remote_test_plan import *


def task_session(task):
    issue = task.split("__", 1)[1] if "__" in task else task
    issue = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue.replace("-", "_").replace("/", "_"))
    return f"{session_prefix}_{issue}"


def generation_state_path(run_dir):
    return run_dir / "generation.state.json"


def load_json(path):
    try:
        context = open_regular_binary(path)
        handle = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_size > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        raw = handle.read(MAX_JSON_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_JSON_DOCUMENT_BYTES:
            raise RecordInputLimitError(f"JSON document exceeds byte limit: {path}")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        context.__exit__(None, None, None)


def start_count(run_dir):
    state = load_json(generation_state_path(run_dir))
    if not isinstance(state, dict):
        return 0
    starts = state.get("starts")
    if isinstance(starts, list):
        current_runtime = generation_runtime_identity()
        return sum(
            1
            for event in starts
            if isinstance(event, dict)
            and (
                not {
                    "workflow",
                    "model_name",
                    "runtime_identity",
                }.issubset(event)
                or (
                    event.get("workflow") == workflow
                    and event.get("model_name") == model_name
                    and event.get("runtime_identity") == current_runtime
                )
            )
        )
    try:
        return int(state.get("start_count") or 0)
    except Exception:
        return 0


def write_start_state(run_dir, task, session):
    if RUNNER_LOCK_FD is None:
        raise RuntimeError("runner directory ownership lock is not held")
    with RUNNER_STATE_THREAD_LOCK:
        state = load_json(generation_state_path(run_dir))
        if not isinstance(state, dict):
            state = {}
        starts = state.get("starts") if isinstance(state.get("starts"), list) else []
        try:
            previous_count = int(state.get("start_count") or 0)
        except (TypeError, ValueError):
            previous_count = 0
        count = previous_count + 1
        event = {
            "started_at": now(),
            "session": session,
            "workflow": workflow,
            "model_name": model_name,
            "runtime_identity": generation_runtime_identity(),
        }
        starts.append(event)
        state.update(
            {
                "schema": "opencollab.generation_state.v1",
                "task": task,
                "start_count": count,
                "last_started_at": event["started_at"],
                "last_session": session,
                "workflow": workflow,
                "model_name": model_name,
                "runtime_identity": generation_runtime_identity(),
                "starts": starts[-20:],
            }
        )
        write_json(generation_state_path(run_dir), state)
        return state


def write_fifo_with_timeout(path, text, timeout=45):
    data = text.encode("utf-8")
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue
        try:
            offset = 0
            while offset < len(data):
                if time.time() >= deadline:
                    return {
                        "ok": False,
                        "error": "timed out while writing complete fifo payload",
                    }
                try:
                    written = os.write(fd, data[offset:])
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if written <= 0:
                    return {"ok": False, "error": "zero-byte fifo write"}
                offset += written
            return {"ok": True}
        except OSError as exc:
            last_error = str(exc)
        finally:
            os.close(fd)
    return {"ok": False, "error": last_error or "timed out waiting for fifo reader"}



def prolite_eval_spec_sha256(row, f2p_plan, p2p_plan):
    payload = {
        "schema": "opencollab.prolite_eval_spec.v2",
        "f2p_plan": f2p_plan,
        "p2p_plan": p2p_plan,
        "test_patch_sha256": hashlib.sha256(str(row.get("test_patch") or "").encode()).hexdigest(),
        "before_repo_sha256": hashlib.sha256(str(row.get("before_repo_set_cmd") or "").encode()).hexdigest(),
        "service_bootstrap_sha256": hashlib.sha256(prolite_service_bootstrap(row).encode()).hexdigest(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def prolite_service_bootstrap(row):
    repo = str(row.get("repo") or "").lower()
    hints = " ".join(
        str(row.get(key) or "") for key in ("database", "before_repo_set_cmd", "test_cmd", "eval_cmd")
    ).lower()
    needs_redis = repo == "nodebb/nodebb" or "redis" in hints
    if not needs_redis:
        return ""
    return r"""
redis_ready() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG && return 0
  fi
  (echo > /dev/tcp/127.0.0.1/6379) >/dev/null 2>&1 && return 0
  return 1
}

if redis_ready; then
  echo "redis already ready on 127.0.0.1:6379"
  exit 0
fi

if command -v redis-server >/dev/null 2>&1; then
  mkdir -p /tmp/opencollab-redis
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /tmp/opencollab-redis --save "" --appendonly no >/tmp/prolite_redis_server.log 2>&1 || true
elif command -v service >/dev/null 2>&1; then
  service redis-server start >/tmp/prolite_redis_server.log 2>&1 || service redis start >>/tmp/prolite_redis_server.log 2>&1 || true
else
  echo "redis-server not found and service command unavailable" >&2
  exit 42
fi

for _attempt in $(seq 1 100); do
  if redis_ready; then
    echo "redis ready on 127.0.0.1:6379"
    exit 0
  fi
  sleep 0.1
done

echo "redis did not become ready on 127.0.0.1:6379" >&2
cat /tmp/prolite_redis_server.log 2>/dev/null || true
exit 42
"""




__all__ = [name for name in globals() if not name.startswith("__")]
