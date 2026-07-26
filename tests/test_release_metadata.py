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
    assert f'version = "{opencollab_eval.__version__}"' in pyproject
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
