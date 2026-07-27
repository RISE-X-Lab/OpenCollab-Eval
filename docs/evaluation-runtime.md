# Evaluation runtime map

**English** | [简体中文](https://rise-x-lab.github.io/OpenCollab-Eval/zh-CN/evaluation-runtime/)

OpenCollab-Eval exposes several execution layers. Choose the highest-level
entrypoint that matches the required result.

| Entry | Input | Output | Official verdict |
| --- | --- | --- | --- |
| `oc-eval inspect` | SWE-Batch Pro dataset and identity key | Anonymous census | No |
| `oc-eval run` | Generic evaluator task JSONL | Candidate eligibility records | No |
| `oc-eval swe-v1-prolite` | One bounded remote Pro-Lite slice | Generation and official reports | Yes |
| `opencollab_eval.commands.swe_eval_run` | Solver name and task indices | Coordinated Pro-Lite batch | Yes |
| `oc-eval final-report` | Two terminal fact reports and audit manifests | Validated publication set | Consumes verdicts |

The remote production runner synchronizes complete declared source trees for
OpenCollab's public package and OpenCollab-Eval. It writes a runtime manifest,
verifies the local and remote tree SHA-256 values, probes the selected remote
Python interpreter, and repeats the identity check immediately before
generation.

Generation starts from a verified task image and a disposable Solver-visible
repository. Candidate construction uses controller-owned Git state after
process quiescence. Official evaluation applies the bound patch to a fresh
workspace and records exact target execution proof.

Single-instance generator modules remain available for operators and tests.

```bash
python -m opencollab_eval.generation.gen_prediction --help
python -m opencollab_eval.generation.gen_prediction_workflow --help
python -m opencollab_eval.generation.gen_prediction_openhands --help
```

The packaged `run_team_batch.sh` and `start_team_run.sh` resources are legacy
gates. They return technical status 125 before Solver launch because their
historical mount design cannot provide the current isolation and trusted
candidate evidence. Use `oc-eval swe-v1-prolite` or the Solver coordinator.

See [SWE Pro-Lite operations](swe-prolite-operations.md) for runnable commands,
[CLI reference](cli-reference.md) for command selection, and
[Evaluation integrity](evaluation-integrity.md) for result semantics.
