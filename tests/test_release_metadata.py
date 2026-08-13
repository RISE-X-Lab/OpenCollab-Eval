"""Static release gates for distribution metadata and license notices."""

from __future__ import annotations

import hashlib
from pathlib import Path

import opencollab_eval

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "src" / "opencollab_eval"
_MULAN_PSL_2_SHA256 = (
    "eb7a1d713eb919b146787629e22e4c975cb701f529a65d4d7e0fcd417558bf1c"
)


def test_release_metadata_keeps_versions_aligned_and_includes_license() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    license_bytes = (_REPO_ROOT / "LICENSE").read_bytes()

    assert opencollab_eval.__version__
    assert opencollab_eval.__version__ == "0.5.0"
    assert 'requires = ["hatchling==1.31.0"]' in pyproject
    assert f'version = "{opencollab_eval.__version__}"' in pyproject
    assert 'dependencies = ["opencollab>=0.5.0,<0.6"]' in pyproject
    assert 'license = "MulanPSL-2.0"' in pyproject
    assert (
        'license-files = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]'
        in pyproject
    )
    assert hashlib.sha256(license_bytes).hexdigest() == _MULAN_PSL_2_SHA256
    assert not (_PACKAGE_ROOT / "LICENSE").exists()


def test_release_notices_preserve_historical_and_third_party_terms() -> None:
    notice = (_REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    normalized = " ".join(notice.split())
    third_party = (_REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(
        encoding="utf-8"
    )

    assert "OpenCollab-Eval was separated from evaluation code" in normalized
    assert "Rights already granted for those revisions are not withdrawn" in normalized
    assert "They do not determine or transfer ownership" in normalized
    assert "Permission is hereby granted, free of charge" in normalized
    assert "SWE-bench" in third_party
    assert "OpenHands" in third_party
    assert "Claude Code" in third_party


def test_distribution_does_not_claim_package_wide_typing() -> None:
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"Typing :: Typed"' not in pyproject
    assert not (_PACKAGE_ROOT / "py.typed").exists()


def test_release_documents_bind_the_050_pair() -> None:
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releasing = (_REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")

    assert "## [0.5.0] - 2026-08-13" in changelog
    assert "OpenCollab 0.5.0" in changelog
    assert "963585611ad2a1d0c1fc7f4ba0043af5a3d860bb" in changelog
    assert "signed annotated tag" in releasing
    assert "verify_wheel_contract.sh" in releasing
    assert "never move a published tag" in releasing
    assert 'rev-parse "v${release_version}^{}"' in releasing
    assert 'rev-parse HEAD' in releasing
    assert 'status --porcelain' in releasing


def test_wheel_contract_discovers_a_sibling_opencollab_checkout() -> None:
    script = (_REPO_ROOT / "scripts" / "verify_wheel_contract.sh").read_text(
        encoding="utf-8"
    )

    assert '[[ -f "$candidate/pyproject.toml"' in script
    assert '-d "$candidate/opencollab"' in script
    assert '$candidate/opencollab/pyproject.toml' not in script


def test_packaged_workflow_guides_match_the_050_runtime_boundary() -> None:
    package_readme = _PACKAGE_ROOT / "workflows" / "README.md"
    package_readme_zh = _PACKAGE_ROOT / "workflows" / "README.zh-CN.md"

    for path in (package_readme, package_readme_zh):
        text = path.read_text(encoding="utf-8")
        assert "OpenCollab 0.5.0" in text
        assert "OpenCollab 0.4" not in text
