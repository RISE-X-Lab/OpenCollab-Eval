# Duo file evidence

**English** | [简体中文](zh-CN/g22-file-evidence.md)

Duo is owned by OpenCollab and exposes one workflow named `duo`. Its
task-oriented roles retain A/B generation, mechanical choice, validated
adjudication, conservative fallback and candidate adoption. The adjudicator
reads complete files through `read_candidate_evidence`.
Import `duo` from `opencollab.builtin_workflows`.

Use the optional workflow argument `candidate_evidence_dir` to place retained
evidence beneath the run's artifact directory. Each adjudication gets its own
child directory. Without the argument, a retained system temporary directory is
created. Its location is written to the workflow log. Archive that directory
along with the workflow trace.

Each candidate has an exact UTF-8 `candidate.diff`, a JSONL index, and its public
command/test records and a separately labeled model result report. Index rows identify original paths, text/binary kind, and
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

Preserve the run's evidence and trace with its actual adopted candidate.
A selector decision records candidate adoption. Benchmark outcomes come from
the evaluator's original tests and resources.
