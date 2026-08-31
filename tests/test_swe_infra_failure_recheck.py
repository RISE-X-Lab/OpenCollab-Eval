from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.engine.swe_infra_failure_recheck import (
    instance_has_parsed_grading,
    load_instance_reports_for_arm,
    recompute_arm_failure_reasons,
)

# Shapes below are drawn from real gpu3 tri15 report.json files, not invented:
# marshmallow-code__marshmallow-1702 (self-collaboration arm) and
# pyvista__pyvista-4329 (single arm), trimmed to the fields these functions read.

_REAL_F2P_FAILURE_INSTANCE = {
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": True,
    "resolved": False,
    "infra_failure": False,
    "tests_status": {
        "FAIL_TO_PASS": {
            "success": ["tests/test_fields.py::TestMetadata::test_a"],
            "failure": [
                "tests/test_fields.py::TestMetadata::test_field_metadata_added_in_deprecated_style_warns[String]",
                "tests/test_fields.py::TestMetadata::test_field_metadata_added_in_deprecated_style_warns[Integer]",
            ],
        },
        "PASS_TO_PASS": {"success": [], "failure": []},
    },
}

# Collection crashed before any test ran (real shape of pyvista-4329, single arm):
# patch_successfully_applied is False and there is no tests_status at all.
_UNPARSED_COLLECTION_CRASH_INSTANCE = {
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": False,
    "resolved": False,
    "infra_failure": True,
    "infra_failure_reason": "network_unreachable",
}


def test_instance_has_parsed_grading_true_when_f2p_results_are_real() -> None:
    assert instance_has_parsed_grading(_REAL_F2P_FAILURE_INSTANCE) is True


def test_instance_has_parsed_grading_false_when_collection_never_ran() -> None:
    assert instance_has_parsed_grading(_UNPARSED_COLLECTION_CRASH_INSTANCE) is False


def test_instance_has_parsed_grading_false_for_missing_or_malformed_report() -> None:
    assert instance_has_parsed_grading(None) is False
    assert instance_has_parsed_grading({}) is False
    assert instance_has_parsed_grading({"patch_successfully_applied": True}) is False


def test_recompute_drops_false_positive_that_has_real_f2p_failures() -> None:
    arm_report = {
        "resolved_instances": 8,
        "unresolved_instances": 7,
        "failure_reasons": {
            "marshmallow-code__marshmallow-1702": "network_unreachable",
            "pylint-dev__pylint-4604": "no_tests_collected",
        },
        "infra_failure_ids": ["marshmallow-code__marshmallow-1702"],
        "ambiguous_failure_ids": ["pylint-dev__pylint-4604"],
        "infra_failure_instances": 1,
        "ambiguous_failure_instances": 1,
    }
    instance_reports = {
        "marshmallow-code__marshmallow-1702": _REAL_F2P_FAILURE_INSTANCE,
        "pylint-dev__pylint-4604": _REAL_F2P_FAILURE_INSTANCE,
    }

    result = recompute_arm_failure_reasons(arm_report, instance_reports)

    assert result["failure_reasons"] == {}
    assert result["infra_failure_ids"] == []
    assert result["ambiguous_failure_ids"] == []
    assert result["infra_failure_instances"] == 0
    assert result["ambiguous_failure_instances"] == 0
    assert result["corrected_false_infra_flags"] == {
        "marshmallow-code__marshmallow-1702": {
            "original_reason": "network_unreachable",
            "original_tier": "environment",
            "f2p_failure_count": 2,
            "f2p_success_count": 1,
        },
        "pylint-dev__pylint-4604": {
            "original_reason": "no_tests_collected",
            "original_tier": "ambiguous",
            "f2p_failure_count": 2,
            "f2p_success_count": 1,
        },
    }
    # Untouched fields pass through unchanged.
    assert result["resolved_instances"] == 8
    assert result["unresolved_instances"] == 7


def test_recompute_leaves_unparsed_instance_flagged() -> None:
    arm_report = {
        "failure_reasons": {"pyvista__pyvista-4329": "network_unreachable"},
        "infra_failure_ids": ["pyvista__pyvista-4329"],
        "ambiguous_failure_ids": [],
    }
    instance_reports = {"pyvista__pyvista-4329": _UNPARSED_COLLECTION_CRASH_INSTANCE}

    result = recompute_arm_failure_reasons(arm_report, instance_reports)

    assert result["failure_reasons"] == {"pyvista__pyvista-4329": "network_unreachable"}
    assert result["infra_failure_ids"] == ["pyvista__pyvista-4329"]
    assert result["corrected_false_infra_flags"] == {}


def test_recompute_treats_missing_instance_report_as_unresolved_evidence() -> None:
    arm_report = {
        "failure_reasons": {"some__instance-1": "network_unreachable"},
        "infra_failure_ids": ["some__instance-1"],
        "ambiguous_failure_ids": [],
    }

    result = recompute_arm_failure_reasons(arm_report, {"some__instance-1": None})

    assert result["failure_reasons"] == {"some__instance-1": "network_unreachable"}
    assert result["corrected_false_infra_flags"] == {}


def test_load_instance_reports_for_arm_reads_real_harness_layout(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs" / "run_evaluation"
    instance_dir = logs_root / "tri15-self-collaboration" / "opencollab-self-collaboration-deepseek-v4-flash" / "marshmallow-code__marshmallow-1702"
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text(
        json.dumps({"marshmallow-code__marshmallow-1702": _REAL_F2P_FAILURE_INSTANCE}),
        encoding="utf-8",
    )

    reports = load_instance_reports_for_arm(
        logs_root,
        "tri15-self-collaboration",
        "opencollab-self-collaboration-deepseek-v4-flash",
        ["marshmallow-code__marshmallow-1702", "missing__instance-1"],
    )

    assert reports["marshmallow-code__marshmallow-1702"] == _REAL_F2P_FAILURE_INSTANCE
    assert reports["missing__instance-1"] is None


def test_load_instance_reports_for_arm_treats_malformed_json_as_missing(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs" / "run_evaluation"
    instance_dir = logs_root / "run-1" / "model-a" / "broken__instance-1"
    instance_dir.mkdir(parents=True)
    (instance_dir / "report.json").write_text("{not json", encoding="utf-8")

    reports = load_instance_reports_for_arm(logs_root, "run-1", "model-a", ["broken__instance-1"])

    assert reports["broken__instance-1"] is None
