# Reusable evaluation suite

**English** | [简体中文](zh-CN/evaluation-suite.md)

This development package combines the running evaluation fixes with the maintained OC 0.5 API. Install the paired OC and OCE development branches together. The OC package supplies candidate workspace isolation, explicit unbounded limits, configured reasoning inheritance, request lifecycle traces, and public model and snapshot inspection. OCE owns task delivery, public dependency preparation, trusted candidate extraction, official test execution, and result interpretation.

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
python -m opencollab_eval.commands.swe_g11_parallel_runner   --runner-transport local --host localhost   --indices "$TASK_INDICES" --max-workers 8 --min-workers 8   --workflow base-team-single-pass-v1   --run-id "$RUN_ID" --session-prefix "$RUN_ID"   --output-dir "$EVAL_ROOT/$RUN_ID/controller"   --remote-base "$EVAL_ROOT/$RUN_ID/tasks"   --remote-runtime-repo "$EVAL_ROOT/runtime"   --remote-python "$(command -v python)" --remote-root "$BENCHMARK_ROOT"   --image-repository "$IMAGE_REPOSITORY"   --remote-proxy-base-url "$PROVIDER_BASE_URL"   --local-proxy-base-url "$PROVIDER_BASE_URL" --proxy-env-file "$PROVIDER_ENV"   --model-name "$MODEL" --llm-model "$MODEL" --llm-provider openai   --context-window "$CONTEXT_WINDOW" --max-output-tokens 65536   --temperature 1 --top-p 0.95 --budget 1000000000000 --max-steps 1000000000000   --swe-timeout 1000000000000 --task-wall-timeout 1000000000300   --total-timeout 1000001000000 --llm-timeout 46800   --eval-container-bind-timeout 120 --max-task-starts 1   --workflow-env OPENCOLLAB_UNBOUNDED_LIMITS=true   --workflow-env OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1   --workflow-env OPENCOLLAB_THINKING=true   --workflow-env OPENCOLLAB_REASONING_EFFORT=max   --workflow-env OPENCOLLAB_WIRE_PROTOCOL=responses   --workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200
```

The explicit unbounded switch resolves task and role token/step limits to `None`. The large CLI values preserve compatibility with numeric command parsers. Per-response output and context sizes remain model parameters. The no-progress interval observes completed model and tool work. A wait that reaches an operational timeout retains its original cause and is reviewed as an evaluation interruption when responsibility is external or unresolved.

Single uses the normal OC `coding` profile and system prompt. Its public task contains the problem statement, requirements, and interface description. The named collaboration implementations are available directly through `--workflow`.

| Setting | Workflow entry |
| --- | --- |
| Base Team | `base-team-single-pass-v1` |
| G11 | `validation-council-solve` |
| G20 | `validation-council-wired-v1` |
| G21 | `validation-council-wired-dual-g20-v1` |
| Triple | `validation-council-triple-coder-contract-v1` |
| Dual Contract | `validation-council-wired-dual-contract-v1` |
| G20 + Coder Contract | `validation-council-g20-coder-contract-v1` |
| Red-Green v2 | `validation-council-g20-coder-red-green-v2` |

The `claude-code` solver profile uses the existing external CLI adapter and the same candidate and official-evaluation machinery. External mini-swe-agent and Native Harbor runs must provide the same public task and cleaned source view before candidate import. Their solver versions and external launch configuration remain part of each run's provenance.

## Environment and information isolation

Generation starts from the requested base tree in a fresh anonymous Git repository. Original history, remote references, hidden test patches, reference solutions, and scoring logs remain outside the solver view. Public dependencies and public build output are captured before source cleanup and restored afterward. NodeBB public build artifacts and Redis readiness are prepared independently of hidden tests. Conda-backed images require successful `testbed` activation before solver commands run. Other language images retain their native toolchain.

Candidate sub-workspaces inherit the permitted runtime dependency view while keeping their source changes separate. Final extraction uses the existing workspace quiescence, ownership, path, and candidate identity checks. Server-local output storage is selected with `OPENCOLLAB_EVAL_OUTPUT_ROOT` and is writable by the official test container.

## Recovery and results

The evaluator binds the actual trajectory directory before invoking the public workflow. Available exception chains are saved with credential redaction. Completed model events provide a known token and turn lower bound after a failed return. Started calls with missing responses remain visible as incomplete usage.

A failed capture retains its owned container and trusted base for recovery. The saved receipt provides the installed-module command. Recovery verifies the selected runtime and the original owner's exit, then uses the existing quiescent extraction path. A saved candidate is evaluated through the eval-only entry with its original instance, record, and patch identity. Healthy sessions and already accepted results keep their existing attempts.

Final outcomes are determined pass, determined capability failure, and evaluation failure. A trusted candidate passing the official targets counts as a pass. A valid completed attempt or a proven intrinsic solver failure can establish capability failure. External interruptions and insufficient evidence remain evaluation failures. Running and queued tasks are execution states. A completed review of an existing candidate contributes to completed progress once, without another generation attempt. Inherited earlier-configuration passes are reported separately from newly measured passes.

## Validation

The package contains regression tests for public task secrecy, candidate identity and workspace isolation, successful and failed Conda activation, artifact recovery, official target parsing, request cancellation, and shared provider capacity. The provider tests use three real local processes to hold 45 and 30 slots, exercise a 100-slot provider, and verify release on cancellation, metadata failure, response closure, and process exit. These tests use no model requests. Run `ruff check .` and `pytest -q` in each repository with `OPENCOLLAB_SOURCE_ROOT` pointing to the paired OC checkout.

Use the receipt recovery_environment with recovery_argv so the interpreter loads the selected runtime. Active gateway slots cover connection establishment and response reading; supplier-side processing concurrency is recorded separately.

For a server-side egress proxy, set HTTPS_PROXY and omit the direct-upstream option. Shared request limits cover that route as well.
