# Evaluation runtime map

**English** | [简体中文](zh-CN/evaluation-runtime.md)

OpenCollab-Eval has several execution layers. The table maps each result to its
intended entrypoint.

| Entry | Input | Output | Official verdict |
| --- | --- | --- | --- |
| `oc-eval inspect` | SWE-Batch Pro dataset and identity key | Anonymous census | No |
| `oc-eval run` | Generic evaluator task JSONL | Candidate eligibility records | No |
| `oc-eval duo` | Duo JSON configuration and selected dataset rows | Candidate and official test reports | Yes |
| `oc-eval rejudge-queue` | Existing verified candidates and a queue plan | New official reports and queue state | Yes |
| `oc-eval package-runtime` | Installed OC and OCE sources | Worker source package and manifest | No |
| `oc-eval swe-v1-prolite` | One bounded remote Pro-Lite slice | Generation and official reports | Yes |
| `opencollab_eval.commands.swe_eval_run` | Solver name and task indices | Coordinated Pro-Lite batch | Yes |
| `oc-eval final-report` | Two terminal fact reports and audit manifests | Validated publication set | Consumes verdicts |

The same worker stages run under both transports. Local transport launches directly on the Linux machine that owns Docker and benchmark storage. SSH transport launches those stages from a separate controller. The names of worker options retain the `remote_` prefix in local configuration.

Follow [the Duo operations tutorial](swe-prolite-operations.md#run-duo-on-one-linux-worker) for an end-to-end run. `oc-eval run` uses prepared generic tasks and stores candidate eligibility. The official benchmark entries also load the evaluator-owned target specification and create a separate scoring workspace.

The remote production runner synchronizes complete declared source trees for
OpenCollab's public package and OpenCollab-Eval. It writes a runtime manifest,
verifies the local and remote tree SHA-256 values, probes the selected remote
Python interpreter, and repeats the identity check immediately before
generation.

Generation starts from a verified task image and a disposable Solver-visible
repository. Candidate construction uses controller-owned Git state after
process quiescence. Official evaluation applies the bound patch to a fresh
workspace and records exact target execution proof.

Candidate capture and environment disposal are recorded separately. A disposal
failure retains an already captured patch and its extraction result, while the
cleanup error and unproven final quiescence keep automatic submission disabled.
The default teardown wait is 60 seconds. When a successful diff command exceeds
its output limit, Eval captures a new diff after quiescence into an owned
file, excludes that file from the diff, and transfers complete byte chunks.
The existing result-record size limit still applies. A failed or incomplete
transfer cannot produce a submission.

Standalone Base currently selects Single2 and uses its OC built-ins and Bash
for project-native tests. Research
workflows with exact-target requirements use the Eval-owned verification tool
and preserve its executable-evidence checks. See the
[workflow tool documentation](../src/opencollab_eval/workflows/README.md).

Single-instance generator modules remain available for operators and tests.

```bash
python -m opencollab_eval.generation.gen_prediction --help
python -m opencollab_eval.generation.gen_prediction_workflow --help
python -m opencollab_eval.generation.gen_prediction_openhands --help
```

The packaged `run_team_batch.sh` and `start_team_run.sh` resources are legacy
gates. They return technical status 125 before Solver launch because their
historical mount design cannot provide the current isolation and trusted
candidate evidence. Use `oc-eval duo`, `oc-eval swe-v1-prolite`, or the Solver coordinator.

See [SWE Pro-Lite operations](swe-prolite-operations.md) for runnable commands,
[CLI reference](cli-reference.md) for command selection, and
[Evaluation integrity](evaluation-integrity.md) for result semantics.
