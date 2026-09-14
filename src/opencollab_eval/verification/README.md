# Native command evidence

All solver testing uses OpenCollab's native Bash command interface. This package
observes the environment result before native output formatting and records the
actual command, workspace, exit code and executed test targets. The command and
its output are passed through unchanged. Runner discovery, fallback execution,
flag injection and model-facing test verdicts have been removed.

Pure pytest, Go and Django output parsers retain existing proof requirements.
These parsers originated in OpenCollab under the same MulanPSL-2.0 license.
Pytest evidence uses named results (`-rA`), Go uses test JSON (`-json`), and Django
uses verbose unittest output. Empty execution, truncation, wrong targets,
conflicting summaries and shell output replay cannot certify a passing target.
An unsuccessful repeated test invalidates overlapping previous evidence.

Other native commands execute normally and retain native Bash output. A command
without a matching evidence parser cannot certify exact workflow targets.
Official hidden-test scoring remains owned by the existing separate evaluator.
