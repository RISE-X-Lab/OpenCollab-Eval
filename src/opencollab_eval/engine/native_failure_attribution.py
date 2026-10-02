"""Classify observed native errors using explicit response or transport evidence."""

from __future__ import annotations

import errno

import httpx
from openai import APIConnectionError, APITimeoutError

_TRANSPORT_NAMES = {
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
    "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
}
_OC_NAMES = {
    "RunError", "ProgrammaticLifecycleError", "WorkflowLifecycleError", "AgentRuntimeLifecycleError",
    "RuntimeError", "ValueError", "TypeError", "AssertionError", "FileNotFoundError", "PermissionError",
    "KeyError", "IndexError",
}
_STORAGE_ERRNOS = {errno.ENOSPC, errno.EDQUOT}
_LIFECYCLE_NAMES = {"RunError", "ProgrammaticLifecycleError", "WorkflowLifecycleError", "AgentRuntimeLifecycleError"}


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _status_code(error):
    code = _integer(getattr(error, "status_code", None))
    return code if code is not None else _integer(getattr(getattr(error, "response", None), "status_code", None))


def _exceptions(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        children = getattr(current, "exceptions", ())
        if isinstance(children, (list, tuple)):
            pending.extend(reversed(children))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif not current.__suppress_context__:
            pending.append(current.__context__)


def exception_chain(error):
    """Preserve structured evidence from causes, contexts, and exception groups."""
    return [
        {"type": type(node).__name__, "module": type(node).__module__,
         **({"errno": code} if (code := _integer(getattr(node, "errno", None))) is not None else {}),
         **({"status_code": code} if (code := _status_code(node)) is not None else {})}
        for node in _exceptions(error)
    ]


def _record_nodes(record):
    yield {**record, "type": record.get("exception_type", record.get("type"))}
    for field in ("exception_chain", "failure_exception_chain", "error_chain"):
        chain = record.get(field)
        if isinstance(chain, (list, tuple)):
            yield from (node for node in chain if isinstance(node, dict))



def persistence_failure(metrics):
    """Recognize required native evidence failures from their owned diagnostics."""
    if not isinstance(metrics, dict):
        return False
    dropped = _integer(metrics.get("tracer_dropped_steps"))
    return bool(
        metrics.get("evidence_complete") is False
        or metrics.get("tracer_write_error")
        or metrics.get("trace_write_error")
        or metrics.get("persistence_errors")
        or dropped is not None and dropped > 0
    )

def classify_failure(error=None, *, record=None):
    if error is None and record is None:
        return {"origin": "none", "retryable": False, "basis": "no_error", "technical_failure": False}
    if record is None:
        code, kind = _status_code(error), type(error).__name__
        nodes = exception_chain(error)
        transport = isinstance(
            error, (APIConnectionError, APITimeoutError, httpx.NetworkError,
                    httpx.TimeoutException, httpx.RemoteProtocolError),
        )
        internal = kind in _OC_NAMES and type(error).__module__ in {"builtins", "opencollab.sdk.result"}
        internal = internal or (kind in _OC_NAMES and type(error).__module__.startswith("opencollab."))
    else:
        code, kind = _integer(record.get("status_code")), record.get("exception_type")
        nodes = list(_record_nodes(record))
        transport = kind in _TRANSPORT_NAMES
        internal = kind in _OC_NAMES
    result = {
        "origin": "unclassified", "retryable": False,
        "basis": "responsibility_requires_request_evidence", "status_code": code, "exception_type": kind,
    }
    if code in {401, 403}:
        result.update(origin="provider_transport", basis="authentication_or_permission_response")
    elif code in {408, 429} or (code is not None and 500 <= code <= 599):
        result.update(origin="provider_transport", retryable=True, basis="transient_http_response")
    elif code is not None:
        pass
    elif transport:
        result.update(origin="provider_transport", retryable=True, basis="transport_operation_failed")
    elif internal:
        result.update(origin="oc", basis="native_internal_exception")
    if kind in {"ExceptionGroup", "BaseExceptionGroup"}:
        members = [node for node in nodes if node.get("type") not in {"ExceptionGroup", "BaseExceptionGroup"}]
        if members and all(node.get("type") in _OC_NAMES for node in members):
            result.update(origin="oc", basis="native_internal_exception")
    result["technical_failure"] = result["origin"] not in {"none", "oc"}
    if kind in _LIFECYCLE_NAMES and all(node.get("type") in _LIFECYCLE_NAMES for node in nodes):
        result.update(technical_failure=True, basis="native_lifecycle_evidence_incomplete")
    nested_codes = [_integer(node.get("status_code")) for node in nodes]
    if code is None:
        nested_code = next((value for value in nested_codes if value is not None), None)
        if nested_code in {401, 403}:
            result.update(origin="provider_transport", technical_failure=True,
                          basis="authentication_or_permission_response")
        elif any(node.get("type") in _TRANSPORT_NAMES for node in nodes) or (
            nested_code in {408, 429} or nested_code is not None and 500 <= nested_code <= 599
        ):
            result.update(origin="provider_transport", technical_failure=True, retryable=True,
                          basis="transport_operation_failed")
        elif nested_code is not None:
            result.update(origin="unclassified", technical_failure=True,
                          basis="responsibility_requires_request_evidence")
    if any(node.get("type") == "OSError" for node in nodes):
        result.update(origin="evaluation_environment", technical_failure=True,
                      basis="system_operation_failed")
    storage = next((_integer(node.get("errno")) for node in nodes
                    if _integer(node.get("errno")) in _STORAGE_ERRNOS), None)
    if storage is not None:
        result.update(origin="evaluation_storage", technical_failure=True, retryable=True,
                      basis="storage_exhausted", errno=storage)
    return result
