# Native command evidence

OpenCollab owns native Bash evidence observation through its public
`opencollab.tools.evidence_tools` factory. It records the command, workspace,
exit status, and executed public test targets while preserving native Bash
behavior and model-visible output.

This package retains `evaluation_tools` and `BashEvidence` as compatibility
imports of `opencollab.tools.evidence_tools` and `opencollab.tools.BashEvidence`.
Public pytest, Go, and Django output parsers are maintained and tested in
OpenCollab. Official hidden-test scoring remains with OpenCollab-Eval.
