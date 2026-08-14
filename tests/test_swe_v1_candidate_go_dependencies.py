from __future__ import annotations

from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind
from opencollab_eval.engine.swe_v1_candidate_go_dependencies import (
    candidate_added_go_modules,
    valid_candidate_added_go_modules,
)
from opencollab_eval.engine.swe_v1_go_failure_proof import go_failure_proof_matches
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan

PATCH = """diff --git a/go.mod b/go.mod
index 1111111..2222222 100644
--- a/go.mod
+++ b/go.mod
@@ -3,6 +3,8 @@ module github.com/gravitational/teleport
 go 1.18
\x20
 require (
+\tgithub.com/vishvananda/netlink v1.1.0
+\tgithub.com/vishvananda/netns v0.0.0-20191106174202-0a2b9b5464df
 \tgolang.org/x/sys v0.0.0-20220811171246-fbc7d0a398ab
 )
diff --git a/lib/auditd/auditd_linux.go b/lib/auditd/auditd_linux.go
new file mode 100644
--- /dev/null
+++ b/lib/auditd/auditd_linux.go
@@ -0,0 +1,3 @@
+package auditd
+import "github.com/vishvananda/netlink"
+var _ = netlink.AuditStatus{}
"""

COMMAND = "python3 -I -c 'trusted dynamic go command'"
LOG = (
    'OPENCOLLAB_GO_TARGET_DISCOVERY {"package": "./lib/auditd", '
    '"test_files": ["lib/auditd/auditd_test.go"], "tests": ["TestSendEvent"]}\n'
    "go: downloading github.com/vishvananda/netlink v1.1.0\n"
    "lib/auditd/auditd_linux.go:17:2: github.com/vishvananda/netlink@v1.1.0: "
    'Get "https://proxy.golang.org/github.com/vishvananda/netlink/@v/v1.1.0.zip": '
    "dial tcp: network is unreachable\n"
)


def _proof() -> dict[str, object]:
    return {
        "kind": "go_json_test_pass",
        "tests": ["TestSendEvent"],
        "dynamic_discovery": True,
        "candidate_source_paths": ["go.mod", "lib/auditd/auditd_linux.go"],
        "candidate_added_go_modules": [
            {"module": "github.com/vishvananda/netlink", "version": "v1.1.0"},
            {
                "module": "github.com/vishvananda/netns",
                "version": "v0.0.0-20191106174202-0a2b9b5464df",
            },
        ],
    }


def test_candidate_go_module_additions_are_extracted_from_require_block() -> None:
    assert candidate_added_go_modules(PATCH) == [
        {"module": "github.com/vishvananda/netlink", "version": "v1.1.0"},
        {
            "module": "github.com/vishvananda/netns",
            "version": "v0.0.0-20191106174202-0a2b9b5464df",
        },
    ]


def test_candidate_go_module_additions_reject_other_directive_sections() -> None:
    patch = PATCH.replace("require (", "exclude (")

    assert candidate_added_go_modules(patch) == []


def test_candidate_go_module_additions_ignore_reordered_requirements() -> None:
    patch = """diff --git a/go.mod b/go.mod
--- a/go.mod
+++ b/go.mod
@@ -1,4 +1,4 @@
 require (
-\texample.com/module v1.2.3
 \texample.com/other v2.0.0
+\texample.com/module v1.2.3
 )
"""

    assert candidate_added_go_modules(patch) == []


def test_candidate_go_dependency_proof_is_part_of_the_plan_contract() -> None:
    modules = candidate_added_go_modules(PATCH)
    plan = prolite_test_plan(
        {"repo_language": "go"},
        ["TestSendEvent"],
        candidate_source_paths=["go.mod", "lib/auditd/auditd_linux.go"],
        candidate_added_go_modules=modules,
    )

    assert plan["proofs"][0]["candidate_added_go_modules"] == modules
    assert validated_test_plan_kind(plan, require_commands=True) == (
        "go-test-json-discovery"
    )


def test_candidate_added_dependency_setup_failure_is_candidate_failure() -> None:
    assert go_failure_proof_matches(
        _proof(),
        LOG,
        expected_command=COMMAND,
        observed_command=COMMAND,
    ) is True


def test_candidate_added_dependency_setup_failure_requires_exact_bindings() -> None:
    cases = []
    missing_module = _proof()
    missing_module["candidate_added_go_modules"] = [
        {"module": "example.com/other", "version": "v1.0.0"}
    ]
    cases.append((missing_module, LOG, COMMAND))
    missing_manifest = _proof()
    missing_manifest["candidate_source_paths"] = ["lib/auditd/auditd_linux.go"]
    cases.append((missing_manifest, LOG, COMMAND))
    cases.append((_proof(), LOG.replace("lib/auditd/auditd_linux.go", "lib/other/x.go"), COMMAND))
    cases.append((_proof(), LOG, COMMAND + " --changed"))

    assert all(
        go_failure_proof_matches(
            proof,
            log,
            expected_command=COMMAND,
            observed_command=observed,
        )
        is False
        for proof, log, observed in cases
    )


def test_candidate_added_go_module_proof_validation_is_strict() -> None:
    assert valid_candidate_added_go_modules(
        [{"module": "github.com/vishvananda/netlink", "version": "v1.1.0"}]
    ) is True
    assert valid_candidate_added_go_modules(
        [{"module": "github.com/vishvananda/netlink", "version": "latest"}]
    ) is False
