"""Reviewed fixture exceptions retain path, value and multiplicity checks."""

from __future__ import annotations

import pytest

from tests.packaging.test_secret_history_check import _commit, _repository, _run, _script_module

_PATHS = ("tests/test_model_fork_audit.py", "tests/experiment/test_model_fork_audit.py")


def _fixture() -> str:
    return 'sk-' + 'planted-secret-value'


def _place(repository, path, text):
    (repository / path).parent.mkdir(parents=True, exist_ok=True)
    _commit(repository, path, text)


@pytest.mark.parametrize("path", _PATHS)
def test_reviewed_fixture_accepts_two_appearances_in_its_exact_path(tmp_path, path):
    repository, base = _repository(tmp_path)
    _place(repository, path, _fixture() + "\n# " + _fixture() + "\n")
    result = _run(repository, base, "HEAD")
    assert result.returncode == 0


def test_reviewed_fixture_rejects_the_same_value_in_a_different_path(tmp_path):
    repository, base = _repository(tmp_path)
    _place(repository, "tests/another_fixture.py", _fixture() + "\n")
    result = _run(repository, base, "HEAD")
    assert result.returncode == 1
    assert "file=tests/another_fixture.py" in result.stdout


@pytest.mark.parametrize("path", _PATHS)
def test_reviewed_fixture_rejects_a_different_value(tmp_path, path):
    repository, base = _repository(tmp_path)
    _place(repository, path, _fixture() + "-other\n")
    result = _run(repository, base, "HEAD")
    assert result.returncode == 1
    assert "Potential OpenAI API key" in result.stdout


@pytest.mark.parametrize("path", _PATHS)
def test_reviewed_fixture_rejects_a_third_appearance(tmp_path, path):
    repository, base = _repository(tmp_path)
    _place(repository, path, (_fixture() + "\n") * 3)
    result = _run(repository, base, "HEAD")
    assert result.returncode == 1
    assert "line=3" in result.stdout


def test_reviewed_fixture_limits_do_not_add_to_a_trusted_base_count(tmp_path):
    repository, _ = _repository(tmp_path)
    path = _PATHS[0]
    _place(repository, path, (_fixture() + "\n") * 2)
    from tests.packaging.test_secret_history_check import _git
    base = _git(repository, "rev-parse", "HEAD")
    _place(repository, path, (_fixture() + "\n") * 3)
    assert _run(repository, base, "HEAD").returncode == 1


def test_reviewed_fixture_is_not_an_exception_for_another_detector(tmp_path):
    repository, base = _repository(tmp_path)
    _place(repository, _PATHS[0], "-----BEGIN " + "PRIVATE KEY-----\n")
    result = _run(repository, base, "HEAD")
    assert result.returncode == 1
    assert "Potential private key" in result.stdout


def test_reviewed_fixture_table_has_two_exact_limits():
    module = _script_module()
    assert len(module._AUDITED_TEST_FINDING_LIMITS) == 2
    assert set(module._AUDITED_TEST_FINDING_LIMITS.values()) == {2}
    assert {identity[0] for identity in module._AUDITED_TEST_FINDING_LIMITS} == {"OpenAI API key"}
    assert {identity[1] for identity in module._AUDITED_TEST_FINDING_LIMITS} == set(_PATHS)
