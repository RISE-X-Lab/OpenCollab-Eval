# ICLR research materials

The ICLR research collection preserves all 389 original files from OpenCollab-Eval
commit `0bcc65764d6233d3f7f49a12f987acaff9f1c3b9`. The collection includes 337
concrete batch descriptions, one batch template, the batch guide, four historical
host descriptions, ten suite files, one model declaration, and 35 analysis scripts,
tests, and synthetic fixture files. Batch descriptions retain their original
repository pins, sample slices, retries, replacements, and withdrawal reasons.

The parsed data values in all 337 batch descriptions retain the original research
conditions. Two Chinese source comments have English translations. Runtime Chinese
scanner output and matching phrases use Unicode escapes in the canonical Python
source, with their values preserved. Original Chinese sources remain in the history
files ending in `.zh-CN.md` and cite their canonical source records.

The host files describe the machines used by those historical batches. `gpu3.yaml`
uses `http://proxy.example:8888` as an operator-supplied connection placeholder.
The original host connection value remains in the ICLR Git commit referenced above.
Supply the actual proxy in a caller-maintained host file passed with `--host`.
New runs
should use a caller-maintained host file passed with `batch --host`. The batch
launcher resolves the host named by an explicitly supplied spec. It records the
actual resolved settings and checks the existing repository pins at the remote
launch boundary. Environment files containing model credentials remain external
inputs referenced by each spec.

The suite directory contains the Verified frame, stratified draw, and subsets,
plus both Pro mini36 samples. The suite README describes sampling provenance.
The Pro clean sample includes the final 36 IDs and the stated sampling procedure.
Its original eligible pool and environment scan are referenced by the historical
batch description and remain external research inputs.

The analysis directory contains the original script entry points. `scan_batch.py`
uses separate classification and measurement modules. `merged_preds.py` selects
predictions through the same retry, replacement, and withdrawal policy as the
batch report, then checks the selected attempt identity within each source file.
`think_scan.py` accepts explicit batch paths and keeps its original reasoning and
tool-use counters. Its original source is preserved under `history`.

The installed experiment package provides offline observations and reports.
The integrated observer uses explicit research limits and the current public
native agent profile. The original arm declaration is preserved under
`history/arm-registry-0bcc657.md`. Historical batch results keep the conditions
identified by their original repository commits. Current observer output describes
the integrated code running under the selected parameters.

Report `tokens_total` measures the selected attempts. `tokens_all_attempts` sums
recorded consumption across their attempts. The JSON document additionally records
consumption of excluded replacements and withdrawn stand-ins in
`recorded_tokens_including_excluded`. These values are recorded token counts.
Provider billing and missing usage records require the corresponding external
request and usage records.

`swe-outcome-join` reads official reports and their existing attempt sidecars.
An explicit record or patch identity stays bound to that identity during joins.
Instance-only fallback is labelled separately. The fail-to-pass fraction `y`,
its denominator, `f2p_graded`, and official `resolved` remain separate fields.
The original skipped-test rule uses SWE-bench 5.0.2. Historical reports carry
their own harness provenance and can be checked against the gold test denominator.
