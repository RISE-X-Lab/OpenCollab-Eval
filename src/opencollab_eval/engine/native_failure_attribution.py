"""Classify observed native errors using explicit response or transport evidence."""

from __future__ import annotations

import httpx
from openai import APIConnectionError, APITimeoutError

_TRANSPORT_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "ReadTimeout",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
}
_OC_NAMES = {
    "RunError",
    "ProgrammaticLifecycleError",
    "WorkflowLifecycleError",
    "AgentRuntimeLifecycleError",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "AssertionError",
    "FileNotFoundError",
    "PermissionError",
    "KeyError",
    "IndexError",
}


def classify_failure(error=None, *, record=None):
    if error is None and record is None:
        return {"origin": "none", "retryable": False, "basis": "no_error"}
    if record is None:
        code = getattr(error, "status_code", None)
        if isinstance(code, bool) or not isinstance(code, int):
            code = getattr(getattr(error, "response", None), "status_code", None)
        kind = type(error).__name__
        transport = isinstance(
            error,
            (
                APIConnectionError,
                APITimeoutError,
                httpx.NetworkError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
            ),
        )
        internal = kind in _OC_NAMES and type(error).__module__ in {"builtins", "opencollab.sdk.result"}
        internal = internal or (kind in _OC_NAMES and type(error).__module__.startswith("opencollab."))
    else:
        code, kind = record.get("status_code"), record.get("exception_type")
        # These are serialized by the unchanged OC _record_agent_failure method.
        transport = kind in _TRANSPORT_NAMES
        internal = kind in _OC_NAMES
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    result = {
        "origin": "unclassified",
        "retryable": False,
        "basis": "responsibility_requires_request_evidence",
        "status_code": code,
        "exception_type": kind,
    }
    if code in {401, 403}:
        result.update(origin="provider_transport", basis="authentication_or_permission_response")
    elif code in {408, 429} or (code is not None and 500 <= code <= 599):
        result.update(origin="provider_transport", retryable=True, basis="transient_http_response")
    elif code is not None:
        # A 400 can result from either caller construction or provider handling.
        return result
    elif transport:
        result.update(origin="provider_transport", retryable=True, basis="transport_operation_failed")
    elif internal:
        result.update(origin="oc", basis="native_internal_exception")
    return result
