# Evaluation documentation

**English** | [简体中文](zh-CN/README.md)

This index covers the evaluation core's installation, task interfaces,
candidate construction, execution evidence and scoring.

| Reader goal | Document |
| --- | --- |
| Install the evaluator and inspect its first task | [Getting started](getting-started.md) |
| Understand components and dependency ownership | [Architecture](architecture.md) |
| Choose an installed core command | [CLI reference](cli-reference.md) |
| Prepare benchmark or generic task inputs | [Task formats](task-formats.md) |
| Interpret trusted results and failure states | [Evaluation integrity](evaluation-integrity.md) |
| Follow candidate isolation and construction | [Trusted candidate construction](design/trusted-candidate-construction.md) |
| Locate candidate and official execution entry points | [Evaluation runtime map](evaluation-runtime.md) |
| Configure independent scoring adaptations | [Scoring adapters](scoring-adapters.md) |
| Understand the synthetic integration fixture | [Deterministic SWE end-to-end test](testing/deterministic-swe-e2e.md) |
| Diagnose execution and evidence failures | [Troubleshooting](troubleshooting.md) |

Experiment methods, campaigns and deployment are described in
[OpenCollabExp](https://github.com/KaiEureka/OpenCollabExp).
