# Native command evidence

OpenCollab owns native Bash evidence observation through its public
`opencollab.tools.evidence_tools` factory. It records the command, workspace,
exit status, and executed public test targets while preserving native Bash
behavior and model-visible output.

New workflow code imports the public observer directly.

```python
from opencollab.tools import BashEvidence, evidence_tools, has_pass_evidence
```

This package retains `evaluation_tools`, `EvaluationToolName`, and `BashEvidence`
as compatibility imports of `opencollab.tools.evidence_tools`,
`opencollab.tools.BuiltinToolName`, and `opencollab.tools.BashEvidence`. The
private compatibility parser module also forwards `has_pass_evidence`.
Public pytest, Go, and Django output parsers are maintained and tested in
OpenCollab. Exact pytest evidence uses `-rA`, Go uses `-json`, and Django source
tests use verbose native output. Passing evidence binds the actual command and
executed targets to the workspace. Official hidden-test scoring remains with
OpenCollab-Eval. See [evaluation integrity](../../../docs/evaluation-integrity.md)
for the official verdict requirements.
