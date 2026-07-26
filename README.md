# OpenCollab-Eval

**English** | [简体中文](README.zh-CN.md)

OpenCollab-Eval is the evaluation system for OpenCollab-based software
engineering agents. It owns benchmark normalization, Solver isolation, trusted
candidate construction, official test execution, evidence validation, remote
batch coordination, and report publication. OpenCollab supplies the agent
framework and its public Python API.

The repository is designed for experiments where an incorrect `resolved` value
is more damaging than a technical failure. A task becomes resolved only when
the declared target tests execute and pass against the same candidate patch
that was produced by the Solver. Empty plans, zero collected tests, missing
proof, candidate identity drift, and a workspace that is still changing remain
technical failures.

## Evaluation flow

```text
trusted benchmark row
        |
        v
sealed judge data + anonymous Solver task
        |
        v
disposable Solver workspace
        |
        v
controller-owned candidate projection
        |
        v
fresh official evaluation workspace
        |
        v
target execution evidence + terminal report
```

The Solver receives the public problem statement and a disposable repository.
The evaluator retains the base commit, test patch, target lists, image identity,
and run identity. Candidate extraction uses evaluator-owned Git state, and the
official evaluator verifies the candidate patch SHA-256 again before running
the declared tests.

## Supported environment

OpenCollab-Eval requires Python 3.10 or newer and OpenCollab 0.4.x. SWE-bench
evaluation requires Docker and the optional `swebench` dependencies. OpenHands
integration requires Python 3.12. Remote Pro-Lite runs additionally require a
Linux worker reachable through SSH, an installed Python runtime, the required
task images, and writable run-scoped storage.

Install the core package from built distributions.

```bash
python -m pip install /path/to/opencollab-0.4.x-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.1.0-py3-none-any.whl
```

Install the SWE-bench integration when official evaluation is needed.

```bash
python -m pip install '/path/to/opencollab_eval-0.1.0-py3-none-any.whl[swebench]'
```

For a source checkout, install the matching OpenCollab repository first.

```bash
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
```

Credentials, datasets, predictions, trajectories, patches, reports, PDFs, and
runtime logs belong outside the source checkout.

## Command overview

| Command | Purpose | Official terminal verdict |
| --- | --- | --- |
| `oc-eval inspect` | Validate and anonymize a SWE-Batch Pro dataset census | No |
| `oc-eval run` | Run the generic evaluation engine and produce candidate eligibility records | No |
| `oc-eval swe-v1-prolite` | Generate and officially evaluate one bounded remote Pro-Lite slice | Yes |
| `oc-eval final-report` | Validate and publish a comparison from two completed fact reports | Consumes existing verdicts |
| `python -m opencollab_eval.commands.swe_eval_run` | Select a Solver and coordinate a bounded Pro-Lite batch | Yes |

`oc-eval run` reports whether a candidate was produced and remains eligible for
submission. It does not turn a candidate into a SWE-bench resolved result.
`oc-eval swe-v1-prolite` and the multi-Solver coordinator include the official
evaluation stage.

Use `oc-eval --help` and the subcommand help for the complete current option
set.

## Dataset inspection

The identity key is an evaluator-owned file containing exactly 32 random bytes.
Keep it with sealed run state. Reuse it for retries of the same batch so public
task IDs stay stable.

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

The command validates the bounded JSONL input and prints the anonymous public
task census. It does not start a Solver or expose sealed judge fields.

## Generic candidate generation

The generic engine accepts one JSON object per line. Each row requires
`task_id` and `description`, and may provide `repo_path`, `docker_image`,
`timeout`, `max_tokens`, and an `extras` object.

```json
{"task_id":"example-1","description":"Fix the failing calculator test","repo_path":"/work/calculator"}
```

```bash
export OPENCOLLAB_MODEL=example-model
export OPENCOLLAB_PROVIDER=openai
read -r OPENCOLLAB_API_KEY < /run/secrets/model-api-key
export OPENCOLLAB_API_KEY
oc-eval run /data/tasks.jsonl \
  --output /results/candidate-run \
  --concurrency 1
```

Provider configuration is resolved through the OpenCollab public API. Prefer
an external secret store or a protected environment file over command-line
credentials. The result summary counts eligible and ineligible candidates, and
the output directory receives `results.jsonl`.

## Official SWE Pro-Lite evaluation

The production remote entrypoint synchronizes the current OpenCollab public
runtime and OpenCollab-Eval runtime, verifies the source-tree identity on the
worker, generates a candidate, waits for process quiescence, projects the
candidate into a fresh official workspace, runs the declared target tests, and
writes JSON and Markdown reports.

```bash
oc-eval swe-v1-prolite \
  --host evaluator@example-worker \
  --remote-root /srv/opencollab-eval \
  --remote-runtime-repo /srv/opencollab-eval/runtime \
  --base-run-dir /srv/opencollab-eval/runs/example \
  --run-id example-001 \
  --session-prefix example-001 \
  --start-index 1 \
  --limit 1 \
  --model-name kimi-for-coding \
  --llm-model kimi-for-coding \
  --llm-provider openai \
  --context-window 262144 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --workflow-env OPENCOLLAB_THINKING=true \
  --workflow-env 'OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"all"}}' \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1 \
  --json-output /results/example-001.json \
  --markdown-output /results/example-001.md
```

Run `--dry-run` first when preparing a new worker. A dry run validates
configuration and planned work, and it does not represent a terminal task
result. See [the Pro-Lite operations guide](docs/swe-prolite-operations.md) for
provider transport, Solver selection, remote layout, retries, reports, and
failure handling.

## Solver selection

The unified coordinator supports `g11`, `g1.1`, `baseTeam`, `TeamPro`,
`openhands`, and `claude-code`.

```bash
python -m opencollab_eval.commands.swe_eval_run \
  --indices 1 \
  --solver g11 \
  --workers 1 \
  --run-id example-g11-001 \
  --output-dir /results/example-g11-001 \
  --host evaluator@example-worker \
  --remote-root /srv/opencollab-eval \
  --remote-eval-work-root /srv/opencollab-eval/runs \
  --session-prefix example-g11-001 \
  --remote-python /srv/opencollab-eval/venv/bin/python \
  --model-name kimi-k3-g11 \
  --llm-model k3 \
  --llm-provider openai \
  --context-window 1048576 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1
```

This example selects the validated K3 profile with a 1048576-token context and
`reasoning_effort=high`. Solver-specific defaults are applied by the
coordinator. OpenHands and Claude Code also require their external runtimes.
The supplied shell resources are adapters around those runtimes and do not
distribute either product.

## Results and evidence

OpenCollab-Eval distinguishes five states.

| State | Meaning |
| --- | --- |
| Candidate produced | A nonempty patch was extracted |
| Submission eligible | Candidate and lifecycle evidence passed generation checks |
| Eval done | Official evaluation produced a bound report |
| Resolved or unresolved | Declared targets executed for the bound candidate and produced a semantic verdict |
| Technical failed | The system could not establish a trustworthy semantic verdict |

Every publishable result binds the task identity, run identity, record ID,
complete patch SHA-256, runtime identity, fresh evaluation workspace, target
plan, command evidence, process cleanup, and official report. A failed target
is unresolved only when execution evidence proves that the intended target ran.
Import failures, collection failures, unsupported plans, missing logs, and
identity mismatches remain technical failures.

Python targets use an evaluator-owned controller and structured per-node Pytest
events. Go targets use `go test -json` evidence. JavaScript targets use
framework-specific parser-backed evidence. Unsupported target syntax fails
closed.

## Documentation

The [documentation index](docs/README.md) routes readers by task. The most
important guides are the [getting started guide](docs/getting-started.md), the
[task format reference](docs/task-formats.md), the
[Pro-Lite operations guide](docs/swe-prolite-operations.md), the
[architecture guide](docs/architecture.md), the
[evaluation integrity guide](docs/evaluation-integrity.md), the
[CLI reference](docs/cli-reference.md), and the
[troubleshooting guide](docs/troubleshooting.md).

The [final report contract](docs/final-report.md) describes evidence-bound
100-task comparison publication. [MIGRATION.md](MIGRATION.md) defines repository
ownership and the OpenCollab public API boundary. [CONTRIBUTING.md](CONTRIBUTING.md)
describes development and review requirements. [SECURITY.md](SECURITY.md)
contains the private vulnerability reporting process.

OpenCollab-Eval is distributed under the
[Mulan Permissive Software License v2](LICENSE). Dependency and attribution
details are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Development verification

```bash
ruff check .
pytest -q
scripts/verify_wheel_contract.sh \
  /path/to/opencollab-0.4.x-py3-none-any.whl \
  /path/to/opencollab_eval-0.1.0-py3-none-any.whl
scripts/run_deterministic_swe_e2e.sh --output /tmp/oce-e2e --runs 1
```

The wheel contract installs both distributions in isolation and runs the Eval
suite against packaged artifacts. The deterministic E2E uses a local fake
OpenAI-compatible service, ephemeral SSH, real `rsync`, Docker, trusted
candidate extraction, and official target execution without using a provider
credential.

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing evaluation behavior.
