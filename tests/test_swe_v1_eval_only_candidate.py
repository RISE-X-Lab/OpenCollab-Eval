from __future__ import annotations

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
    )
    metrics[0].update(overrides)
    _write_jsonl(metrics_path, metrics)
    predictions_path = namespace["base_run_dir"] / task / "predictions.jsonl"
    predictions = namespace["read_jsonl"](predictions_path)
    predictions[0]["workflow_metric"] = dict(metrics[0])
    _write_jsonl(predictions_path, predictions)
    return namespace["latest_pair"](namespace["base_run_dir"] / task, task)


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
    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "verified"
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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "verified"
    assert metric["agent_failures"][0]["exception_type"] == "ResponsesProtocolError"


def test_eval_only_accepts_legacy_blocked_candidate_without_provider_failure_fields(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"].pop("provider_failure")
    metric.pop("provider_failure")

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "verified"


@pytest.mark.parametrize("document", ["metric", "embedded"])
def test_eval_only_rejects_one_sided_provider_failure_field(tmp_path, document):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    target = metric if document == "metric" else prediction["workflow_metric"]
    target.pop("provider_failure")

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


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
    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "verified"


@pytest.mark.parametrize("document", ["metric", "embedded"])
def test_eval_only_rejects_one_sided_transport(tmp_path, document):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    target = metric if document == "metric" else prediction["workflow_metric"]
    target["llm_transport"] = "direct"

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


@pytest.mark.parametrize("transport", ["", "proxy", "DIRECT", None, 1])
def test_eval_only_rejects_invalid_matching_transport(tmp_path, transport):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"]["llm_transport"] = transport
    metric["llm_transport"] = transport

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


def test_eval_only_rejects_mismatched_transport(tmp_path):
    namespace = _namespace(tmp_path)
    task = "task-1"
    prediction, metric, _pairing = _blocked_candidate(namespace, task)
    prediction["workflow_metric"]["llm_transport"] = "reverse_proxy"
    metric["llm_transport"] = "direct"

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_failure", True),
        ("submission_eligible", False),
        ("execution_quiesced", False),
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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"


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

    assert namespace["eval_only_generation_identity_status"](
        prediction, metric, task
    ) == "invalid"
