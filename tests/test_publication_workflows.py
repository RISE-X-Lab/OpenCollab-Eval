"""Static contracts for publication-critical GitHub workflows."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_hygiene_runs_for_pull_requests_and_main_pushes() -> None:
    workflow = _workflow("hygiene.yml")

    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "BASE_SHA: ${{ github.event.before }}" in workflow
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
    assert 'git show "$BASE_SHA:scripts/check_conventional_title.py"' in workflow
    assert 'python3 - --title "$TITLE"' in workflow
    assert 'python3 scripts/check_conventional_title.py --title "$TITLE"' in workflow
    assert 'python3 - --range "$BASE_SHA" "$HEAD_SHA"' in workflow
    assert "git ls-tree --name-only" in workflow
    assert "persist-credentials: false" in workflow


def test_security_uses_the_trusted_base_and_scans_every_proposed_commit() -> None:
    workflow = _workflow("security.yml")

    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "BASE_SHA: ${{ github.event.before }}" in workflow
    assert "HEAD_SHA: ${{ github.sha }}" in workflow
    assert 'git show "$BASE_SHA:scripts/check_secret_history.py"' in workflow
    assert 'python3 scripts/check_secret_history.py "$BASE_SHA" "$HEAD_SHA"' in workflow
    assert "check_secret_history.py" in workflow
    assert "git ls-tree --name-only" in workflow
    assert ".secrets.baseline" not in workflow
    assert "detect-secrets" not in workflow
    assert 'elif [ -z "$trusted_checker" ]; then' in workflow
    assert "persist-credentials: false" in workflow


def test_ci_uses_verified_action_release_commits() -> None:
    workflow = _workflow("ci.yml")

    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@83679a892e2d95755f2dac6acb0bfd1e9ac5d548" in workflow
    assert "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f" in workflow
    assert workflow.count("token: ${{ secrets.OPENCOLLAB_READ_TOKEN }}") == 2
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
