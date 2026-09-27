# Duo v3 file evidence

**English** | [简体中文](zh-CN/g22-file-evidence.md)

`duo-v3` is owned by OpenCollab and keeps the Duo coder prompts, A/B
sequence, mechanical choice, structured decision validation, default-A rule,
and candidate adoption. Its adjudicator receives references to complete
evidence files and the `read_candidate_evidence` tool instead of inline diffs.
The original `validation-council-dual-coder-selection-v2` and
`validation-council-dual-coder-selection-v3` names remain compatible with their
existing workflow identities. Import `duo_v3` from `opencollab.builtin_workflows`.

Use the optional workflow argument `candidate_evidence_dir` to place retained
evidence beneath the run's artifact directory. Each adjudication gets its own
child directory. Without the argument, a retained system temporary directory is
created. Its location is written to the workflow log. Archive that directory
along with the workflow trace.

Each candidate has an exact UTF-8 `candidate.diff`, a JSONL index, and its public
command/test records. Index rows identify original paths, text/binary kind, and
character ranges within the diff. Full binary patches, permission changes,
deletions, and missing final newlines remain in the saved evidence. Public
records shared by A and B have a separate file.

The tool reads registered evidence on the workflow host. It works with an
isolated judge environment without mounting either candidate into the judge.
It has no command execution or write operation and accepts only paths published
by this particular evidence store. Candidate-controlled filenames are index
data, never host read paths.

Reads use Unicode character offsets and return content, `next_offset`, and
`eof`. Each response is limited to 32,768 characters to keep an individual tool
message manageable, including files with enormous single lines. The judge can
continue reading without a total-read limit. Cite original changed paths from
the index in the decision schema, rather than artifact storage paths.

This is an explicit selector-input version change. Preserve old results and
traces when using v3 to inspect existing candidates. A new selector decision is
not a verifier result; score the adopted candidate with the original tests and
resources before reporting a benchmark outcome.
