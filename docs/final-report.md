# Final SWE comparison reports

**English** | [简体中文](https://rise-x-lab.github.io/OpenCollab-Eval/zh-CN/final-report/)

`oc-eval final-report` publishes one comparison from two completed 100-task
SWE-bench Pro-Lite runs. Every output format is rendered from one validated JSON
model. The command exits successfully only after the PDF and all source files
have been atomically published and the publication manifest has status `final`.

## Command

```bash
oc-eval final-report \
  --method-a-report /sealed/runs/g11/final_report.json \
  --method-a-audit-manifest /sealed/runs/g11/clean_run_manifest.json \
  --method-a-name G1.1 \
  --method-b-report /sealed/runs/openhands/final_report.json \
  --method-b-audit-manifest /sealed/runs/openhands/clean_run_manifest.json \
  --method-b-name OpenHands \
  --dataset-file /sealed/datasets/swe-batch-pro-lite.jsonl \
  --meeting-date 2026-07-15 \
  --author "Evaluation Team" \
  --labels-json /sealed/report_labels.json \
  --narrative-json /sealed/report_notes.json \
  --output-dir /sealed/publication
```

The output directory receives files with a common date-derived prefix: the
validated comparison model as JSON, Markdown, TeX, the compiled PDF, and a
publication manifest. A first failed validation or LaTeX build records manifest
status `failed` and returns a nonzero exit code. Once a complete `final`
publication exists, a later failed attempt leaves all five published files and
their hashes unchanged. Publication serializes writers for one prefix,
preflights every target, backs up the previous output set, rechecks every
published hash, and restores the complete previous set if any replacement or
manifest write fails.

## Fact report contract

The required `--dataset-file` is the trusted, bounded Pro-Lite JSON or JSONL
source. Its bytes must match the recorded Pro-Lite 1-100 snapshot SHA-256
`a1d473cb415ec0050eee023f373cdf71183436351216240f3f88c820a200c078`.
Its ordered 100-task census and every task's
`FAIL_TO_PASS` and `PASS_TO_PASS` targets are loaded before either method
report. Both fact reports must map indices 1 through 100 to those same task
identities. Both audit manifests must declare the exact computed dataset
SHA-256. Each official report must retain the immutable `sha256:...` Docker
image identity used for evaluation, and its two declared target lists must
exactly match the trusted dataset row before command evidence can establish a
terminal verdict. The dataset path and hash are recorded in the comparison
model and publication manifest.

Plans are independently derived from the trusted dataset row. Adapter,
coverage mode, target batches, commands, and proof bindings must equal that
derived plan. Python targets run through an evaluator-owned controller that
separates the trusted Pytest protocol from the candidate interpreter and emits
structured per-node evidence. Go targets use `go test -json`, and JavaScript
targets use framework-specific parser-backed evidence. Stored exit codes,
console text, or unbound plugin events cannot make a row publishable.

Each fact report uses schema
`opencollab.swe_eval_layer_final_report.v1`. It must contain the exact ordered
task census 1 through 100. Every row must have completed generation and official
evaluation, a Boolean verdict, zero pending or technical state, a stable record
identity, a full patch SHA-256, an official report path, and direct execution
proof. The declared aggregate counts must equal the values derived from the
rows. Missing, duplicate, reordered, ambiguous, or technically failed rows stop
publication.

## Clean-run audit manifest contract

Each audit manifest uses schema `opencollab.swe_clean_run_manifest.v1` and binds
to the exact fact report through `source_report_sha256`. It records the method
name; the full task census for clean trajectory, candidate identity, network
isolation, and direct execution; the exact resolved-task set with executable
proof; OpenCollab and OpenCollab-Eval commits; the dataset SHA-256; and one or
more structured evidence files. Both compared methods must use the same runtime
and dataset identities.

```json
{
  "schema": "opencollab.swe_clean_run_manifest.v1",
  "method": "G1.1",
  "source_report_sha256": "<64 lowercase hex characters>",
  "expected_indices": [1, 2, 3],
  "clean_trajectory_indices": [1, 2, 3],
  "candidate_identity_indices": [1, 2, 3],
  "network_isolation_indices": [1, 2, 3],
  "direct_execution_indices": [1, 2, 3],
  "resolved_execution_indices": [2],
  "runtime": {
    "opencollab_commit": "<40 or 64 lowercase hex characters>",
    "opencollab_eval_commit": "<40 or 64 lowercase hex characters>",
    "dataset_sha256": "<64 lowercase hex characters>"
  },
  "evidence_files": [
    {
      "path": "evidence/trajectory_audit.json",
      "sha256": "<64 lowercase hex characters>"
    }
  ]
}
```

The abbreviated arrays above illustrate field meaning. A publishable Pro-Lite
manifest contains all indices 1 through 100 in every full-census field.

Every listed evidence file uses schema
`opencollab.swe_clean_run_evidence.v1`. Its method, source report SHA-256, and
runtime object must equal the manifest. Its task rows collectively cover the
exact ordered 1 through 100 census without overlap. Each row binds the task ID,
record ID, and patch SHA-256 from the fact report and sets
`trajectory_clean`, `candidate_identity_verified`, `network_isolated`, and
`direct_execution_proven` to `true`. It also binds exactly four underlying
artifacts by path and SHA-256: the official evaluation report, trajectory
evidence, candidate-identity evidence, and network-isolation evidence. Relative
artifact paths are resolved from the structured evidence file. Every artifact
must be a nonempty, bounded, regular file whose bytes match the declared hash.
Supporting artifacts are hash-verified without retaining their bodies. At most
one bounded official-report body is retained while task records are checked,
so memory use does not scale with all referenced artifact sizes.
Per-file coverage arrays must equal the actual task rows, and
resolved-execution indices must equal the resolved subset derived from those
bound facts.

The official-report artifact path must exactly equal the task's `report_path`
in the fact report. The artifact must contain a structured
`opencollab.prolite_direct_eval.v2` record for the same task, record ID, patch
SHA-256, and verdict. The command independently recalculates executable proof
from the target-test plans, command evidence, exit statuses, cleanup evidence,
and container result. A matching audit Boolean cannot substitute for a missing,
changed, or internally incomplete official report. The evaluator-owned audit
document supplies the interpretation of the trajectory, identity, and network
artifacts; the final-report command pins that interpretation to the exact raw
artifact bytes that were reviewed.

```json
{
  "schema": "opencollab.swe_clean_run_evidence.v1",
  "method": "G1.1",
  "source_report_sha256": "<same fact report SHA-256>",
  "runtime": {
    "opencollab_commit": "<same commit>",
    "opencollab_eval_commit": "<same commit>",
    "dataset_sha256": "<same dataset SHA-256>"
  },
  "covered_indices": [1],
  "clean_trajectory_indices": [1],
  "candidate_identity_indices": [1],
  "network_isolation_indices": [1],
  "direct_execution_indices": [1],
  "resolved_execution_indices": [],
  "tasks": [
    {
      "index": 1,
      "task": "<fact report task ID>",
      "record_id": "<fact report record ID>",
      "patch_sha256": "<fact report patch SHA-256>",
      "trajectory_clean": true,
      "candidate_identity_verified": true,
      "network_isolated": true,
      "direct_execution_proven": true,
      "artifacts": {
        "official_report": {
          "path": "/sealed/eval/task-1/report.json",
          "sha256": "<official report SHA-256>"
        },
        "trajectory": {
          "path": "raw/task-1/trajectory.jsonl",
          "sha256": "<trajectory evidence SHA-256>"
        },
        "candidate_identity": {
          "path": "raw/task-1/candidate.json",
          "sha256": "<candidate evidence SHA-256>"
        },
        "network_isolation": {
          "path": "raw/task-1/network.json",
          "sha256": "<network evidence SHA-256>"
        }
      }
    }
  ]
}
```

## Optional presentation inputs

The labels document uses schema
`opencollab.swe_final_report_labels.v1` and overrides known presentation labels.
Resolved counts, comparison counts, terminal coverage, and evidence claims are
generated directly from the validated model and cannot be supplied by labels.
The narrative document uses schema
`opencollab.swe_final_report_narrative.v1`; it may add overview paragraphs and
task notes with task indices and evidence references. Narrative text cannot
change any verdict, count, comparison set, runtime identity, or evidence hash.
Narrative evidence references must name a file already verified by one of the
two audit manifests. All external text is escaped independently for Markdown
and TeX.

## Publication requirements and outputs

The default renderer requires `xelatex` on `PATH`. Select another compatible
engine with `--latex-engine`. The output directory must be outside the source
checkout and writable by the evaluator.

The command publishes one common prefix with `.json`, `.md`, `.tex`, `.pdf`,
and `.manifest.json` files. The publication manifest records every final
file name, SHA-256, byte size, validated runtime identity, dataset identity,
and final status.

Exit status 0 means the complete publication set was validated, rendered,
hashed, and committed. Input validation, evidence validation, file safety,
locking, rendering, or publication replacement failure returns nonzero and
records a failed manifest when the destination is safe to write.
