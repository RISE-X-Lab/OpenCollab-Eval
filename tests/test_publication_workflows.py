"""Static contracts for publication-critical GitHub workflows."""

import os
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _push_script(workflow_name: str, step_name: str) -> str:
    workflow = yaml.safe_load(_workflow(workflow_name))
    steps = next(iter(workflow["jobs"].values()))["steps"]
    return next(step["run"] for step in steps if step.get("name") == step_name)


def _run_push_script(
    repository: Path,
    workflow_name: str,
    step_name: str,
    base: str,
    head: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(BASE_SHA=base, HEAD_SHA=head, RUNNER_TEMP=str(repository))
    return subprocess.run(
        ["bash", "-c", _push_script(workflow_name, step_name)],
        cwd=repository,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def _workflow(name: str) -> str:
    return (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_hygiene_runs_for_pull_requests_and_main_pushes() -> None:
    workflow = _workflow("hygiene.yml")

    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "BASE_SHA: ${{ github.event.before }}" in workflow
    assert 'effective_base="$BASE_SHA"' in workflow
    assert 'git cat-file -e "$effective_base^{commit}"' in workflow
    assert 'git merge-base --is-ancestor "$effective_base" "$HEAD_SHA"' in workflow
    assert 'effective_base="$zero_sha"' in workflow
    assert "EMPTY_TREE=\"$(git hash-object -t tree /dev/null)\"" in workflow
    assert '"$EMPTY_TREE" "$HEAD_SHA" --require-files' in workflow
    assert 'git show "$BASE_SHA:scripts/check_added_files.py"' in workflow
    assert 'python3 scripts/check_added_files.py "$BASE_SHA" "$HEAD_SHA"' in workflow
    assert 'elif [ -z "$trusted_checker" ]; then' in workflow
    assert "git ls-tree --name-only" in workflow
    assert "persist-credentials: false" in workflow


def test_conventional_title_checks_pr_title_and_pushed_commit_object() -> None:
    workflow = _workflow("lint-pr-title.yml")

    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "TITLE: ${{ github.event.pull_request.title }}" in workflow
    assert "BASE_SHA: ${{ github.event.before }}" in workflow
    assert "HEAD_SHA: ${{ github.sha }}" in workflow
    assert 'effective_base="$BASE_SHA"' in workflow
    assert 'git cat-file -e "$effective_base^{commit}"' in workflow
    assert 'git merge-base --is-ancestor "$effective_base" "$HEAD_SHA"' in workflow
    assert 'effective_base="$zero_sha"' in workflow
    assert 'git show "$BASE_SHA:scripts/check_conventional_title.py"' in workflow
    assert 'python3 - --title "$TITLE"' in workflow
    assert 'python3 scripts/check_conventional_title.py --title "$TITLE"' in workflow
    assert 'python3 - --range "$effective_base" "$HEAD_SHA"' in workflow
    assert "git ls-tree --name-only" in workflow
    assert "persist-credentials: false" in workflow


def test_security_uses_the_trusted_base_and_scans_every_proposed_commit() -> None:
    workflow = _workflow("security.yml")

    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "BASE_SHA: ${{ github.event.before }}" in workflow
    assert "HEAD_SHA: ${{ github.sha }}" in workflow
    assert 'effective_base="$BASE_SHA"' in workflow
    assert 'git cat-file -e "$effective_base^{commit}"' in workflow
    assert 'git merge-base --is-ancestor "$effective_base" "$HEAD_SHA"' in workflow
    assert 'effective_base="$zero_sha"' in workflow
    assert 'git show "$BASE_SHA:scripts/check_secret_history.py"' in workflow
    assert (
        'python3 scripts/check_secret_history.py "$effective_base" "$HEAD_SHA"'
        in workflow
    )
    assert "check_secret_history.py" in workflow
    assert "git ls-tree --name-only" in workflow
    assert ".secrets.baseline" not in workflow
    assert "detect-secrets" not in workflow
    assert 'elif [ -z "$trusted_checker" ]; then' in workflow
    assert "persist-credentials: false" in workflow


def test_push_gates_reject_an_unrelated_but_reachable_before_commit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Workflow Test")
    _git(repository, "config", "user.email", "workflow@example.invalid")
    scripts = (
        "check_added_files.py",
        "check_conventional_title.py",
        "check_secret_history.py",
    )
    for name in scripts:
        path = repository / "scripts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("raise SystemExit(0)\n", encoding="utf-8")
    _git(repository, "add", "scripts")
    _git(
        repository,
        "commit",
        "-m",
        "chore: \u5efa\u7acb\u65e7\u5386\u53f2",
    )
    old_head = _git(repository, "rev-parse", "HEAD")

    _git(repository, "switch", "--orphan", "replacement")
    for name in scripts:
        path = repository / "scripts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            (_ROOT / "scripts" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (repository / "credential.txt").write_text(
        "API_KEY=" + "A" * 24 + "\n",
        encoding="utf-8",
    )
    (repository / "oversized.bin").write_bytes(b"x" * 512_001)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "invalid replacement title")
    new_head = _git(repository, "rev-parse", "HEAD")

    cases = (
        (
            "lint-pr-title.yml",
            "Check every pushed commit title",
            "invalid replacement title",
        ),
        (
            "security.yml",
            "Scan every pushed commit with the trusted checker",
            "Potential assigned credential",
        ),
        (
            "hygiene.yml",
            "Check the complete main tree",
            "512001 bytes",
        ),
    )
    for workflow_name, step_name, evidence in cases:
        result = _run_push_script(
            repository,
            workflow_name,
            step_name,
            old_head,
            new_head,
        )
        assert result.returncode == 1, (workflow_name, result.stdout, result.stderr)
        assert evidence in result.stdout


def test_ci_uses_verified_action_release_commits() -> None:
    workflow = _workflow("ci.yml")
    opencollab_ref = "bdba30b7ff026cfa150737d6002f918824918efc"

    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert workflow.count(f"ref: {opencollab_ref}") == 2
    assert (
        workflow.count(
            "token: ${{ secrets.OPENCOLLAB_READ_TOKEN || github.token }}"
        )
        == 2
    )
    assert "if-no-files-found: error" in workflow
    assert workflow.count(
        "OPENCOLLAB_SOURCE_ROOT: ${{ github.workspace }}/opencollab-source"
    ) == 3
    assert workflow.count(
        "OPENCOLLAB_EVAL_SOURCE_ROOT: ${{ github.workspace }}/eval"
    ) == 2


def test_wheel_contract_excludes_only_source_repository_governance_tests() -> None:
    script = (_ROOT / "scripts" / "verify_wheel_contract.sh").read_text(
        encoding="utf-8"
    )
    source_only = {
        "test_conventional_title_check.py",
        "test_hygiene_check.py",
        "test_public_readiness.py",
        "test_publication_workflows.py",
        "test_release_metadata.py",
        "test_secret_history_check.py",
    }

    for filename in source_only:
        assert f'--ignore="$venv_dir/eval-tests/{filename}"' in script
    assert script.count('--ignore="$venv_dir/eval-tests/') == len(source_only)
