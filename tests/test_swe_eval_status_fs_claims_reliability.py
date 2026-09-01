from __future__ import annotations

from swe_eval_status_support import importlib, json, os, pytest, time


def _driver_claim(tmp_path, **updates):
    driver = importlib.import_module("opencollab_eval.commands.swe_auto_eval_driver")
    claim = tmp_path / "claim.json"
    now_ns = time.time_ns()
    value = {
        "schema": "opencollab.swe_eval_claim.v1",
        "status": "started",
        "pid": os.getpid(),
        "owner_start_identity": "proc:expected",
        "started_at_ns": now_ns - 120_000_000_000,
        "heartbeat_at_ns": now_ns - 120_000_000_000,
        "lease_expires_at_ns": now_ns - 90_000_000_000,
        **updates,
    }
    claim.write_text(json.dumps(value), encoding="utf-8")
    return driver, claim, value


def test_auto_eval_retains_expired_owner_with_matching_identity(monkeypatch, tmp_path):
    driver, claim, existing = _driver_claim(tmp_path)
    monkeypatch.setattr(driver, "_pid_is_active", lambda _pid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda _pid: "proc:expected")

    acquired, observed = driver._acquire_claim(claim, {"pid": 0})

    assert acquired is False
    assert observed == existing

def test_auto_eval_retains_expired_owner_when_identity_probe_is_unknown(
    monkeypatch, tmp_path
):
    driver, claim, existing = _driver_claim(tmp_path)
    monkeypatch.setattr(driver, "_pid_is_active", lambda _pid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda _pid: "")

    acquired, observed = driver._acquire_claim(claim, {"pid": 0})

    assert acquired is False
    assert observed == existing


@pytest.mark.parametrize(
    ("group_exists", "current_identity"),
    [(False, "proc:expected"), (True, "proc:reused")],
    ids=["group-gone", "identity-mismatch"],
)
def test_auto_eval_stale_residual_group_is_reclaimable_when_unowned(
    monkeypatch, tmp_path, group_exists, current_identity
):
    driver, claim, _existing = _driver_claim(
        tmp_path,
        status="technical_eval_failed",
        pid=0,
        evaluator_pgid=424246,
        evaluator_start_identity="proc:expected",
    )
    monkeypatch.setattr(driver, "_process_group_exists", lambda _pgid: group_exists)
    monkeypatch.setattr(driver, "_process_start_identity", lambda _pid: current_identity)
    replacement = {"pid": 0}

    acquired, observed = driver._acquire_claim(claim, replacement)

    assert acquired is True
    assert observed == replacement


def test_auto_eval_retains_expired_matching_residual_group(monkeypatch, tmp_path):
    driver, claim, existing = _driver_claim(
        tmp_path,
        status="technical_eval_failed",
        pid=0,
        evaluator_pgid=424245,
        evaluator_start_identity="proc:expected",
    )
    monkeypatch.setattr(driver, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(driver, "_process_start_identity", lambda _pid: "proc:expected")

    acquired, observed = driver._acquire_claim(claim, {"pid": 0})

    assert acquired is False
    assert observed == existing
