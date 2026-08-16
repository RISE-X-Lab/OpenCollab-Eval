from __future__ import annotations

import hashlib
import json

from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
    pytest,
)

_IDENTITY_FIELDS = (
    "model",
    "workflow",
    "invocation_id",
    "run_id",
    "runtime_tree_sha256",
    "budget",
    "max_steps",
    "llm_base_url_sha256",
    "workflow_env",
    "llm_model",
    "llm_provider",
    "context_window",
    "temperature",
    "top_p",
    "max_output_tokens",
    "wire_protocol",
    "reasoning_effort",
)


def _namespace(tmp_path):
    return _remote_namespace(
        tmp_path,
        run_id="task25-run",
        runtime_tree_sha256="a" * 64,
        workflow_env={
            "OPENCOLLAB_WIRE_PROTOCOL": "responses",
            "OPENCOLLAB_REASONING_EFFORT": "max",
        },
        llm_model="deepseek-v4-flash-0731",
        llm_provider="openai",
        context_window=1_048_576,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32_768,
    )


def _blocked_candidate(namespace, task, **overrides):
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="blocked",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[],
        provider_failure=False,
        submission_eligible=True,
        container_execution_quiesced=True,
        workflow_result={
            "status": "blocked",
            "blocker": "Execution environment has been aborted during verification.",
            "attempts": [
                {
                    "final_verdict": {
                        "verdict": "BLOCKED",
                        "findings": "Execution environment has been aborted during verification.",
                    }
                }
            ],
        },
    )
    metrics[0].update(overrides)
    _write_jsonl(metrics_path, metrics)
    predictions_path = namespace["base_run_dir"] / task / "predictions.jsonl"
    predictions = namespace["read_jsonl"](predictions_path)
    predictions[0]["workflow_metric"] = dict(metrics[0])
    _write_jsonl(predictions_path, predictions)
    return namespace["latest_pair"](namespace["base_run_dir"] / task, task)


def _eval_only_status(namespace, prediction, metric, task, attempts=0):
    return namespace["eval_only_generation_identity_status"](
        prediction,
        metric,
        task,
        matching_official_eval_attempts=attempts,
    )


def _candidate_failure_blocker():
    return (
        "Executable verification could not be completed because go: command not found; "
        "only /usr/local/go/bin/go was located after an explicit PATH export, and the "
        "prior build command returned no usable output."
    )


def _candidate_failure_rows(blocker):
    return [
        {
            "type": "tool_exec",
            "payload": {
                "tool": "bash",
                "args": {"command": "cd /testbed && go build ./..."},
                "result": "Exit code: 127\nstdout:\ngo: command not found\n",
            },
        },
        {
            "type": "tool_exec",
            "payload": {
                "tool": "bash",
                "args": {
                    "command": (
                        "export PATH=$PATH:/usr/local/go/bin && cd /testbed "
                        "&& go version && go build ./... 2>&1 | head -50"
                    )
                },
                "result": (
                    "Exit code: 0\nstdout:\ngo version go1.24.3 linux/amd64\n"
                    "src/a.go:167:20: cannot use api.GetShares as handler\n"
                ),
            },
        },
        {
            "type": "llm_call",
            "payload": {
                "tool_calls": [
                    {
                        "name": "structured_output",
                        "arguments": json.dumps(
                            {"verdict": "BLOCKED", "findings": blocker}
                        ),
                    }
                ]
            },
        },
    ]


def _attach_candidate_failure_trajectory(
    namespace,
    prediction,
    metric,
    task,
    *,
    rows=None,
    blocker=None,
):
    patch = "diff --git a/src/a.go b/src/a.go\n+current\n"
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    prediction["model_patch"] = patch
    prediction["patch_sha256"] = patch_sha
    metric["patch_sha256"] = patch_sha
    extraction = metric["trusted_patch_extraction"]
    extraction["patch_sha256"] = patch_sha
    extraction["patch_bytes"] = len(patch.encode())
    extraction["changed_paths"] = ["src/a.go"]
    extraction["path_modes"] = [
        {"path": "src/a.go", "old_mode": "100644", "new_mode": "100644"}
    ]
    blocker = blocker or _candidate_failure_blocker()
    rows = rows if rows is not None else _candidate_failure_rows(blocker)
    trajectory = (
        namespace["base_run_dir"]
        / task
        / "workflow_logs"
        / "trajectories"
        / "solver-test"
        / "runtime-test"
        / "orchestration.jsonl"
    )
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(
        json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n"
        for row in rows
    )
    trajectory.write_bytes(raw)
    metric.update(
        trajectory_path=str(trajectory),
        trajectory_sha256=hashlib.sha256(raw).hexdigest(),
        patch_path_audit={"actual_paths": ["src/a.go"]},
        workflow_result={
            "status": "blocked",
            "blocker": blocker,
            "attempts": [
                {
                    "final_verdict": {
                        "verdict": "BLOCKED",
                        "findings": blocker,
                    }
                }
            ],
        },
    )
    prediction["workflow_metric"] = dict(metric)
    return trajectory


def test_eval_only_accepts_blocked_candidate_with_bound_compile_failure(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _attach_candidate_failure_trajectory(namespace, prediction, metric, task)

    assert (
        _eval_only_status(namespace, prediction, metric, task)
        == "blocked_candidate_failure_verified"
    )


@pytest.mark.parametrize(
    "rows",
    [
        _candidate_failure_rows(_candidate_failure_blocker())[1:],
        [
            _candidate_failure_rows(_candidate_failure_blocker())[0],
            {
                "type": "tool_exec",
                "payload": {
                    "tool": "bash",
                    "args": {
                        "command": (
                            "export PATH=$PATH:/usr/local/go/bin && go build ./..."
                        )
                    },
                    "result": "",
                },
            },
            _candidate_failure_rows(_candidate_failure_blocker())[2],
        ],
        [
            _candidate_failure_rows(_candidate_failure_blocker())[0],
            {
                "type": "tool_exec",
                "payload": {
                    "tool": "bash",
                    "args": {
                        "command": (
                            "export PATH=$PATH:/usr/local/go/bin && go build ./..."
                        )
                    },
                    "result": "Exit code: 0\nstdout:\n",
                },
            },
            _candidate_failure_rows(_candidate_failure_blocker())[2],
        ],
        [
            _candidate_failure_rows(_candidate_failure_blocker())[0],
            {
                "type": "tool_exec",
                "payload": {
                    "tool": "bash",
                    "args": {
                        "command": (
                            "export PATH=$PATH:/usr/local/go/bin && go build ./..."
                        )
                    },
                    "result": (
                        "other/package/file.go:10:2: undefined: candidateSymbol\n"
                    ),
                },
            },
            _candidate_failure_rows(_candidate_failure_blocker())[2],
        ],
        [
            _candidate_failure_rows(_candidate_failure_blocker())[0],
            {
                "type": "tool_exec",
                "payload": {
                    "tool": "bash",
                    "args": {
                        "command": (
                            "export PATH=$PATH:/usr/local/go/bin && go build ./..."
                        )
                    },
                    "result": (
                        "src/a.go:167:20: warning: deprecated\n"
                    ),
                },
            },
            _candidate_failure_rows(_candidate_failure_blocker())[2],
        ],
        _candidate_failure_rows(_candidate_failure_blocker())[:2],
    ],
)
def test_eval_only_rejects_blocked_candidate_without_complete_compile_trace(
    tmp_path,
    rows,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _attach_candidate_failure_trajectory(
        namespace,
        prediction,
        metric,
        task,
        rows=rows,
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def test_eval_only_rejects_compile_trace_with_semantic_blocker(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    blocker = "Candidate semantics do not satisfy the requested behavior."
    _attach_candidate_failure_trajectory(
        namespace, prediction, metric, task, blocker=blocker, rows=_candidate_failure_rows(blocker)
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def test_eval_only_rejects_compile_trace_with_mismatched_digest(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _attach_candidate_failure_trajectory(namespace, prediction, metric, task)
    metric["trajectory_sha256"] = "0" * 64
    prediction["workflow_metric"] = dict(metric)

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda prediction, metric: prediction["workflow_metric"].__setitem__("trajectory_sha256", "0" * 64),
        lambda prediction, metric: prediction["workflow_metric"].__setitem__(
            "patch_path_audit", {"actual_paths": ["other/file.go"]}),
        lambda prediction, metric: prediction["workflow_metric"].__setitem__(
            "trusted_patch_extraction", {"changed_paths": ["other/file.go"]}),
        lambda prediction, metric: metric.__setitem__("execution_quiesced", False),
        lambda prediction, metric: metric.__setitem__("container_execution_quiesced", False),
        lambda prediction, metric: metric["workflow_result"].__setitem__("status", "completed"),
        lambda prediction, metric: (
            prediction["workflow_metric"].__setitem__("patch_path_audit", {"actual_paths": [{}]}),
            metric.__setitem__("patch_path_audit", {"actual_paths": [{}]})),
    ],
)
def test_eval_only_rejects_candidate_failure_with_divergent_or_live_identity(
    tmp_path,
    mutation,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _attach_candidate_failure_trajectory(namespace, prediction, metric, task)
    mutation(prediction, metric)

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    ("command", "result"),
    [
        (
            "echo /usr/local/go/bin && cd /testbed && go build ./...",
            "Exit code: 1\nstdout:\nsrc/a.go:167:20: cannot use candidate\n",
        ),
        (
            "export PATH=$PATH:/usr/local/go/bin && cd /testbed && go build ./... || true",
            "Exit code: 0\nstdout:\nsrc/a.go:167:20: cannot use candidate\n",
        ),
        (
            "export PATH=$PATH:/usr/local/go/bin && cd /testbed && go build ./...",
            "Exit code: 0\nstdout:\nsrc/a.go:167:20: cannot use candidate\n",
        ),
        (
            "export PATH=$PATH:/usr/local/go/bin && cd /testbed && "
            "go build ./... || printf 'src/a.go:1:1: forged\\n'; false",
            "Exit code: 1\nstdout:\nsrc/a.go:1:1: forged\n",
        ),
    ],
)
def test_eval_only_rejects_untrusted_corrected_probe(tmp_path, command, result):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    blocker = _candidate_failure_blocker()
    rows = _candidate_failure_rows(blocker)
    rows[1]["payload"]["args"]["command"] = command
    rows[1]["payload"]["result"] = result
    _attach_candidate_failure_trajectory(namespace, prediction, metric, task, rows=rows)

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def test_eval_only_rejects_compile_trace_after_official_attempt(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _attach_candidate_failure_trajectory(namespace, prediction, metric, task)

    assert _eval_only_status(namespace, prediction, metric, task, attempts=1) == "invalid"


def test_eval_only_accepts_proven_blocked_candidate_with_no_provider_failure(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)

    assert namespace["_normalized_historical_llm_transport"](
        prediction["workflow_metric"], metric
    ) == "reverse_proxy"
    assert namespace["historical_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"
    assert _eval_only_status(namespace, prediction, metric, task) == "blocked_technical_verified"
    assert namespace["generation_done"](
        namespace["base_run_dir"] / task, task, require_identity=False
    )[0] is False
    assert namespace["generation_done_for_mode"](
        namespace["base_run_dir"] / task, task, eval_only=True
    )[0] is True


def test_eval_only_accepts_proven_blocked_candidate_with_agent_failure_evidence(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(
        namespace,
        task,
        agent_failures=[
            {
                "exception_type": "ResponsesProtocolError",
                "label": "baseline-triage",
                "provider_error_type": None,
                "status_code": None,
            }
        ],
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "blocked_technical_verified"
    assert metric["agent_failures"][0]["exception_type"] == "ResponsesProtocolError"


def test_eval_only_rejects_blocked_candidate_after_official_attempt(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    _write_jsonl(
        namespace["base_run_dir"] / task / "eval_attempts.jsonl",
        [
            {
                "phase": "eval_attempt_started",
                "task": task,
                "record_id": "different-record",
            }
        ],
    )

    assert _eval_only_status(namespace, prediction, metric, task, attempts=1) == "invalid"
    assert (
        namespace["generation_done_for_mode"](
            namespace["base_run_dir"] / task,
            task,
            eval_only=True,
        )[0]
        is False
    )


@pytest.mark.parametrize(
    "workflow_result",
    [
        None,
        {},
        {"status": "failed"},
        {
            "status": "blocked",
            "blocker": "Verifier returned no evidence.",
            "attempts": [],
        },
        {
            "status": "blocked",
            "blocker": "Execution environment has been aborted during verification.",
            "attempts": [],
        },
        {
            "status": "blocked",
            "blocker": "Execution environment has been aborted during verification.",
            "attempts": [
                {
                    "final_verdict": {
                        "verdict": "FAIL",
                        "findings": "Execution environment has been aborted during verification.",
                    }
                }
            ],
        },
        {
            "status": "blocked",
            "blocker": "Execution environment has been aborted during verification.",
            "attempts": [
                {
                    "final_verdict": {
                        "verdict": "BLOCKED",
                        "findings": "different evidence",
                    }
                }
            ],
        },
    ],
)
def test_eval_only_rejects_blocked_candidate_without_causal_technical_evidence(
    tmp_path,
    workflow_result,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(
        namespace,
        task,
        workflow_result=workflow_result,
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_status", "failed"),
        ("runner_returncode", 0),
        ("runtime_status", "failed"),
        ("error", "different error"),
        ("submission_eligible", False),
        ("execution_quiesced", False),
        ("container_execution_quiesced", False),
        (
            "workflow_result",
            {
                "status": "blocked",
                "blocker": "Verifier rejected the candidate semantics.",
                "attempts": [
                    {
                        "final_verdict": {
                            "verdict": "BLOCKED",
                            "findings": "Verifier rejected the candidate semantics.",
                        }
                    }
                ],
            },
        ),
    ],
)
def test_eval_only_rejects_mismatched_embedded_causal_evidence(tmp_path, field, value):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"][field] = value

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    "field",
    [
        "workflow_status",
        "runner_returncode",
        "runtime_status",
        "error",
        "submission_eligible",
        "execution_quiesced",
        "container_execution_quiesced",
        "workflow_result",
    ],
)
def test_eval_only_rejects_one_sided_causal_evidence(tmp_path, field):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"].pop(field)

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def test_eval_only_accepts_legacy_blocked_candidate_without_provider_failure_fields(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"].pop("provider_failure")
    metric.pop("provider_failure")

    assert _eval_only_status(namespace, prediction, metric, task) == "blocked_technical_verified"


@pytest.mark.parametrize("document", ["metric", "embedded"])
def test_eval_only_rejects_one_sided_provider_failure_field(tmp_path, document):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    target = metric if document == "metric" else prediction["workflow_metric"]
    target.pop("provider_failure")

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    "agent_failures",
    [
        None,
        {},
        ["invalid"],
        [{}],
        [{"arbitrary": 1}],
        [{"label": "", "exception_type": "ResponsesProtocolError"}],
        [{"label": "baseline-triage", "exception_type": ""}],
        [
            {
                "label": "baseline-triage",
                "exception_type": "ResponsesProtocolError",
                "provider_error_type": None,
                "status_code": "503",
            }
        ],
        [
            {
                "label": "baseline-triage",
                "exception_type": "PermissionDeniedError",
                "provider_error_type": "access_terminated_error",
                "status_code": 403,
            }
        ],
        [{"label": "\ud800", "exception_type": "ResponsesProtocolError"}],
        [{"label": "baseline-triage", "exception_type": "\ud800"}],
        [
            {
                "label": "baseline-triage",
                "exception_type": "ResponsesProtocolError",
                "provider_error_type": "\ud800",
            }
        ],
    ],
)
def test_eval_only_rejects_malformed_agent_failure_evidence(tmp_path, agent_failures):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(
        namespace,
        task,
        agent_failures=agent_failures,
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize("field", ["provider_failure", "agent_failures"])
def test_eval_only_rejects_mismatched_embedded_failure_evidence(tmp_path, field):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    if field == "provider_failure":
        prediction["workflow_metric"][field] = True
    else:
        prediction["workflow_metric"][field] = [
            {
                "label": "other-agent",
                "exception_type": "ResponsesProtocolError",
                "provider_error_type": None,
                "status_code": None,
            }
        ]

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize("transport", ["direct", "reverse_proxy"])
def test_eval_only_accepts_matching_explicit_transport(tmp_path, transport):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"]["llm_transport"] = transport
    metric["llm_transport"] = transport

    assert namespace["_normalized_historical_llm_transport"](
        prediction["workflow_metric"], metric
    ) == transport
    assert _eval_only_status(namespace, prediction, metric, task) == "blocked_technical_verified"


@pytest.mark.parametrize("document", ["metric", "embedded"])
def test_eval_only_rejects_one_sided_transport(tmp_path, document):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    target = metric if document == "metric" else prediction["workflow_metric"]
    target["llm_transport"] = "direct"

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize("transport", ["", "proxy", "DIRECT", None, 1])
def test_eval_only_rejects_invalid_matching_transport(tmp_path, transport):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"]["llm_transport"] = transport
    metric["llm_transport"] = transport

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def test_eval_only_rejects_mismatched_transport(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"]["llm_transport"] = "reverse_proxy"
    metric["llm_transport"] = "direct"

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_failure", True),
        ("submission_eligible", False),
        ("execution_quiesced", False),
        ("container_execution_quiesced", False),
        ("patch_extraction_succeeded", False),
        ("trusted_patch_extraction", {}),
    ],
)
def test_eval_only_rejects_blocked_candidate_without_complete_proof(
    tmp_path,
    field,
    value,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(
        namespace, task, **{field: value}
    )

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


def _tampered(value):
    if isinstance(value, dict):
        return {**value, "tampered": "true"}
    if isinstance(value, (int, float)):
        return value + 1
    return f"{value}-tampered"


def _identity_key(document, field):
    if field != "model":
        return field
    return "model_name_or_path" if document == "prediction" else "model_name"


@pytest.mark.parametrize("field", _IDENTITY_FIELDS)
@pytest.mark.parametrize("document", ["metric", "embedded"])
@pytest.mark.parametrize("failure", ["missing", "mismatch"])
def test_eval_only_rejects_incomplete_or_tampered_identity_document(
    tmp_path,
    field,
    document,
    failure,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    target = metric if document == "metric" else prediction["workflow_metric"]
    key = _identity_key(document, field)
    if failure == "missing":
        target.pop(key)
    else:
        target[key] = _tampered(target[key])

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"


@pytest.mark.parametrize("field", ["model", "workflow"])
@pytest.mark.parametrize("failure", ["missing", "mismatch"])
def test_eval_only_rejects_prediction_identity_document_tampering(
    tmp_path,
    field,
    failure,
):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    key = _identity_key("prediction", field)
    if failure == "missing":
        prediction.pop(key)
    else:
        prediction[key] = _tampered(prediction[key])

    assert _eval_only_status(namespace, prediction, metric, task) == "invalid"
