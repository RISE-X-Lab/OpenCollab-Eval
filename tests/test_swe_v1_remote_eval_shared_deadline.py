from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from opencollab_eval.engine import swe_v1_remote_eval_script
from opencollab_eval.engine import swe_v1_remote_test_plan as plans


def test_direct_eval_wires_one_deadline_to_both_test_phases() -> None:
    script = swe_v1_remote_eval_script.direct_eval_script()
    evaluation_source = Path(
        "src/opencollab_eval/engine/swe_v1_remote_evaluation.py"
    ).read_text(encoding="utf-8")

    assert "OPENCOLLAB_EVAL_TIMEOUT_SECONDS" in script
    assert "export OPENCOLLAB_EVAL_DEADLINE" in script
    assert script.index("export OPENCOLLAB_EVAL_DEADLINE") < script.index(
        "bash /eval_input/f2p.sh"
    )
    assert 'if [ "$f2p_status" -eq 125 ]; then' in script
    assert "p2p was not started" in script
    assert evaluation_source.count(
        'shared_deadline_env="OPENCOLLAB_EVAL_DEADLINE"'
    ) == 2
    assert 'OPENCOLLAB_EVAL_TIMEOUT_SECONDS={configured_eval_timeout}' in evaluation_source


def test_f2p_and_p2p_consume_one_external_deadline(
    monkeypatch, tmp_path: Path
) -> None:
    """Run both generated phase scripts so a second full timeout is impossible."""

    monkeypatch.setattr(
        plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic"
    )
    output = tmp_path / "eval-output"
    output.mkdir()
    scripts: dict[str, Path] = {}
    for phase in ("f2p", "p2p"):
        script = plans.prolite_test_plan_script(
            {
                "commands": ["python3 -c 'import time; time.sleep(0.45)'"],
                "proofs": [{}],
            },
            phase,
            "nonce",
            controller_timeout=0.8,
            shared_deadline_env="OPENCOLLAB_EVAL_DEADLINE",
        ).replace("/eval_output", str(output))
        path = tmp_path / f"{phase}.sh"
        path.write_text(script, encoding="utf-8")
        scripts[phase] = path

    orchestrator = "\n".join(
        [
            "set +e",
            "deadline=$(python3 -c 'import time; print(time.monotonic()+0.8)')",
            "export OPENCOLLAB_EVAL_DEADLINE=\"$deadline\"",
            f"bash {shlex.quote(str(scripts['f2p']))}",
            "f2p_status=$?",
            f"bash {shlex.quote(str(scripts['p2p']))}",
            "p2p_status=$?",
            'printf "f2p=%s p2p=%s\\n" "$f2p_status" "$p2p_status"',
        ]
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", orchestrator],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=3,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout
    assert "f2p=0 p2p=124" in result.stdout
    # A pair of independent 0.8-second budgets would take roughly 0.9 seconds
    # and let p2p pass; the shared deadline stops it at the first budget.
    assert elapsed < 1.5
    assert (output / "f2p.batch_001.exit").read_text(encoding="utf-8").strip() == "0"
    assert (output / "p2p.batch_001.exit").read_text(encoding="utf-8").strip() == "124"
