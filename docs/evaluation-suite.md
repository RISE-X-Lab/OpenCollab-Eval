# Reusable evaluation suite

**English** | [简体中文](zh-CN/evaluation-suite.md)

This development package combines the running evaluation fixes with the current OC 0.6 main API. Install the paired OC and OCE development revisions together. The OC package supplies candidate workspace isolation, explicit unbounded limits, configured reasoning inheritance, request lifecycle traces, and public model and snapshot inspection. OCE owns task delivery, public dependency preparation, trusted candidate extraction, official test execution, and result interpretation.

## Install and package

Run these commands in an OCE checkout with its paired OC checkout in the sibling `OpenCollab` directory. `package-runtime` copies the installed source modules into a fresh directory using the existing runtime manifest format. The resulting directory can be copied to the evaluation server. Provider credentials are read from operator-owned environment files.

```bash
python -m pip install -e '../OpenCollab[dev]' -e '.[dev,swebench]'
export OPENCOLLAB_SOURCE_ROOT="$(cd ../OpenCollab && pwd)"
export EVAL_ROOT="$(pwd)/evaluation-output"
export OPENCOLLAB_EVAL_OUTPUT_ROOT="$EVAL_ROOT/test-output"
mkdir -p "$OPENCOLLAB_EVAL_OUTPUT_ROOT"
oc-eval package-runtime --output "$EVAL_ROOT/runtime"
```

## Provider request capacity

The included direct gateway can share request slots across multiple processes and ports. Every gateway for one upstream uses the same provider policy and lock directory. A slot remains held until the upstream response closes. Cancellation while waiting and process exit release their ownership. Slot metadata write failures also release the descriptor.

`examples/evaluation-suite/provider-limits.json` illustrates separate limits of 45, 30, 100, and 100. Replace its example origins and storage directory for the deployment. Task-worker counts and actual provider request counts are separate. The expensive fourth provider is reserved for priority external solvers and Single. After those groups complete, setting its policy `enabled` to `false` prevents new requests while already open responses can finish. New remaining-group work uses the other configured endpoints.

The environment file contains `OPENCOLLAB_UPSTREAM_BASE_URL`, `OPENCOLLAB_UPSTREAM_API_KEY`, and a separate `OPENCOLLAB_PROXY_CLIENT_TOKEN` for local clients. Start the gateway under the server's service manager with these arguments.

```bash
python -m opencollab_eval.commands.llm_api_proxy   --env-file "$PROVIDER_ENV" --port "$PROVIDER_PORT"   --direct-upstream --timeout 46800   --provider-limits-file "$PROVIDER_LIMITS_FILE"
```

## Run a server-local queue

Set `TASK_INDICES` to the intended dataset rows and choose a fresh `RUN_ID`. `BENCHMARK_ROOT` identifies the existing benchmark data and public preparation assets. `IMAGE_REPOSITORY` identifies its prepared images. `MODEL`, `CONTEXT_WINDOW`, and the provider URL describe the actual model interface. The example below starts eight independent tasks and keeps model execution on the server. A service manager or a persistent server terminal owns the command so client-machine shutdown leaves the server process running.

```bash
python -m opencollab_eval.commands.swe_g11_parallel_runner   --runner-transport local --host localhost   --indices "$TASK_INDICES" --max-workers 8 --min-workers 8   --workflow base-team-single-pass-v1   --run-id "$RUN_ID" --session-prefix "$RUN_ID"   --output-dir "$EVAL_ROOT/$RUN_ID/controller"   --remote-base "$EVAL_ROOT/$RUN_ID/tasks"   --remote-runtime-repo "$EVAL_ROOT/runtime"   --remote-python "$(command -v python)" --remote-root "$BENCHMARK_ROOT"   --image-repository "$IMAGE_REPOSITORY"   --remote-proxy-base-url "$PROVIDER_BASE_URL"   --local-proxy-base-url "$PROVIDER_BASE_URL" --proxy-env-file "$PROVIDER_ENV"   --model-name "$MODEL" --llm-model "$MODEL" --llm-provider openai   --context-window "$CONTEXT_WINDOW" --max-output-tokens 65536   --temperature 1 --budget 1000000000000 --max-steps 1000000000000   --swe-timeout 1000000000000 --task-wall-timeout 1000000000300   --total-timeout 1000001000000 --llm-timeout 46800   --eval-container-bind-timeout 120 --max-task-starts 1   --workflow-env OPENCOLLAB_UNBOUNDED_LIMITS=true   --workflow-env OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1   --workflow-env OPENCOLLAB_THINKING=true   --workflow-env OPENCOLLAB_REASONING_EFFORT=max   --workflow-env OPENCOLLAB_WIRE_PROTOCOL=responses   --workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200
```

The explicit unbounded switch resolves workflow task and role token/step limits to `None`. Native Single uses the numeric `budget` and `max_steps` supplied to the public agent interface; use the same large values for its launch configuration. The large CLI values preserve compatibility with numeric command parsers. Per-response output and context sizes remain model parameters. The no-progress interval observes completed model and tool work. A wait that reaches an operational timeout retains its original cause and is reviewed as an evaluation interruption when responsibility is external or unresolved.

Single uses the normal OC `coding` profile and system prompt. Its public task contains the problem statement, requirements, and interface description. The named collaboration implementations are available directly through `--workflow`.

| Setting | Workflow entry |
| --- | --- |
| Base Team | `base-team-single-pass-v1` |
| G11 | `validation-council-solve` |
| G20 | `validation-council-wired-v1` |
| G21 | `validation-council-dual-coder-contract-v1` |
| Wired Dual G20 | `validation-council-wired-dual-g20-v1` |
| Triple | `validation-council-triple-coder-contract-v1` |
| Dual Contract | `validation-council-wired-dual-contract-v1` |
| G20 + Coder Contract | `validation-council-g20-coder-contract-v1` |
| Red-Green v2 | `validation-council-g20-coder-red-green-v2` |

Workflow selection and agent profile selection are independent. G21 with Single2 uses the existing G21 entry together with `--agent-profile single2`. The profile applies to every agent role, including the contract adjudicator. The workflow retains its role prompts, permitted tools, candidate workspaces, and selection policy. OC supplies the Single2 system prompt, context shaping, safety policy, and built-in tool defaults. OCE keeps its Bash evidence wrapper around the same native tool instance. For this profile, native Bash output uses the Single2 10,000-character limit.

```bash
python -m opencollab_eval.generation.gen_prediction_workflow \
  --instance-file "$INSTANCE_FILE" --image "$IMAGE" \
  --output "$PREDICTIONS_FILE" \
  --workflow validation-council-dual-coder-contract-v1 \
  --agent-profile single2 --model "$MODEL" --provider openai
```

The same `--agent-profile` option is accepted by the parallel runner and `oc-eval swe-v1-prolite`. It is recorded in the task configuration, generation metrics, and workflow manifest. Omitting it preserves the existing workflow driver. `--agent-profile single` selects that default explicitly. The standalone `--workflow single2` entry continues to select the native single-agent generator.

The `claude-code` solver profile uses the existing external CLI adapter and the same candidate and official-evaluation machinery. External mini-swe-agent and Native Harbor runs must provide the same public task and cleaned source view before candidate import. Their solver versions and external launch configuration remain part of each run's provenance.

## Environment and information isolation

Workflow container scratch storage follows the standard `TMPDIR` setting, using a fresh owned directory for each container. For server-local runs, choose an operator-owned directory on local NVMe storage and keep predictions and reports in the shared output directory. `OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS` controls public setup and dependency stash, restore, removal, and candidate-pool preparation through the actual Docker transport. An omitted preparation setting preserves the workspace archive allowance. Ordinary Docker tool calls and workspace archives retain their separate limits. Each preparation operation retains its configured finite allowance. An explicitly configured task wall-clock timeout also bounds preparation.

```bash
export TMPDIR="${EVAL_LOCAL_TMP_ROOT:?Set a local scratch directory}/opencollab-preparation/${RUN_ID:?Set a run name}"
mkdir -p "$TMPDIR"
chmod 700 "$TMPDIR"
# Include this setting in the existing parallel runner's arguments.
# --workflow-env OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS=43200
```

Generation starts from the requested base tree in a fresh anonymous Git repository. Original history, remote references, hidden test patches, reference solutions, and scoring logs remain outside the solver view. Public dependencies and public build output are captured before source cleanup and restored afterward. NodeBB public build artifacts and Redis readiness are prepared independently of hidden tests. Conda-backed images require successful `testbed` activation before solver commands run. Other language images retain their native toolchain.

Candidate sub-workspaces inherit the permitted runtime dependency view while keeping their source changes separate. Final extraction uses the existing workspace quiescence, ownership, path, and candidate identity checks. Server-local output storage is selected with `OPENCOLLAB_EVAL_OUTPUT_ROOT` and is writable by the official test container.

## Complete role evidence

Role handoffs retain complete public reports, structured fields, path lists, and test records. Validation decisions carry their accepted candidate specifications, including commands, setup, assertions, and contract references. The coder receives localization, requirements, test cartography, and prior findings together and can inspect definitions and run available public verification within its coding pass. The existing role graph, approval counts, and repair-round limits remain the workflow policy.

G11, the G20 variants, G21, Triple, Dual Contract, G20 + Coder Contract, Red-Green, and the evidence/tournament councils use complete handoff text. Long commands remain exact when comparing candidate test evidence. Existing private-field filtering, candidate path checks, patch validation, and official scoring proofs remain active. Base Team already carries full reports and has regression coverage for that behavior. Model context capacity is handled by the configured model runtime.

Completed trajectories are verified incrementally. Total file size can exceed 16 MiB while the existing per-record memory bound, stable-file checks, provider identity, reasoning configuration, and full-file digest are preserved. Historical results retain their original runtime revisions; runs using these handoff fixes identify the updated source revision.

## Recovery and results

The evaluator binds the actual trajectory directory before invoking the public workflow. Available exception chains are saved with credential redaction. Completed model events provide a known token and turn lower bound after a failed return. Started calls with missing responses remain visible as incomplete usage.

A failed capture retains its owned container and trusted base for recovery. The saved receipt provides the installed-module command. Recovery verifies the selected runtime and the original owner's exit, then uses the existing quiescent extraction path. A saved candidate is evaluated through the eval-only entry with its original instance, record, and patch identity. Healthy sessions and already accepted results keep their existing attempts.

Final outcomes are determined pass, determined capability failure, and evaluation failure. A trusted candidate passing the official targets counts as a pass. A valid completed attempt or a proven intrinsic solver failure can establish capability failure. External interruptions and insufficient evidence remain evaluation failures. Running and queued tasks are execution states. A completed review of an existing candidate contributes to completed progress once, without another generation attempt. Inherited earlier-configuration passes are reported separately from newly measured passes.

## Validation

The package contains regression tests for public task secrecy, candidate identity and workspace isolation, successful and failed Conda activation, artifact recovery, official target parsing, request cancellation, and shared provider capacity. The provider tests use three real local processes to hold 45 and 30 slots, exercise a 100-slot provider, and verify release on cancellation, metadata failure, response closure, and process exit. These tests use no model requests. Run `ruff check .` and `pytest -q` in each repository with `OPENCOLLAB_SOURCE_ROOT` pointing to the paired OC checkout.

Use the receipt recovery_environment with recovery_argv so the interpreter loads the selected runtime. Active gateway slots cover connection establishment and response reading; supplier-side processing concurrency is recorded separately.

For a server-side egress proxy, set HTTPS_PROXY and omit the direct-upstream option. Shared request limits cover that route as well.

## Runtime and recovery configuration

An explicit `--context-window` reaches both the native agent and workflow
runtime, and the effective value remains part of generation identity. G1.1
reads `OPENCOLLAB_VALIDATION_COUNCIL_ROLE_BUDGET` for each role invocation and
accepts a positive integer or `unbounded`. The older `OPENCOLLAB_G11_ROLE_BUDGET`
remains an alias. `OPENCOLLAB_VALIDATION_COUNCIL_MAX_CODER_ROUNDS` changes the
repair-round allowance while retaining the default of three. Provider recovery
time is added to the outer role wait without extending normal model-call time.

An eval-only queue job may set `source_base_run_dir` separately from
`base_run_dir`; omission preserves the original same-directory behavior. The
queue owns the source option and forwards it to the single-instance runner.
Read-only health checks and repeated transfer of one runtime archive can retry
transport timeouts within their existing attempt and overall time bounds.

Candidate capture interprets trusted ignore rules in a controller-owned path
view, so read-only Solver directories and cache control files remain untouched.
The Gitlink census uses the existing complete-tree scan allowance, independently
of the much smaller resulting Gitlink manifest. Verified candidates stopped by
known intrinsic OC failures can proceed to scoring without another generation;
unknown or provider failures retain the existing rejection behavior.

Pytest collection failures require a fixed-test callsite and matching candidate
module before they can establish candidate failure. JavaScript missing-module
failures require the missing import to have been introduced by the candidate.
An optional trusted test-patch digest lets offline reconciliation recover the
same binding from saved evaluator inputs. Unbound failures remain technical.

Saving, restoring, and removing prepared image dependencies, including preparing
candidate copies, use `OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT` with its default of
900 seconds. These operations can transfer large dependency trees. Ordinary
Docker control operations use `OPENCOLLAB_DOCKER_TIMEOUT`.
