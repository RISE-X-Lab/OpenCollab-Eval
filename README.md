<a id="english"></a>

<h1 align="center">OpenCollab-Eval</h1>

<p align="center"><strong>Run and verify SWE-bench evaluations for OpenCollab agents</strong></p>

<p align="center"><strong>English</strong> · <a href="#simplified-chinese">简体中文</a></p>

<p align="center">
  <a href="#supported-environment">Quick start</a> ·
  <a href="#command-overview">Commands</a> ·
  <a href="docs/evaluation-integrity.md">Evaluation integrity</a> ·
  <a href="#documentation">Documentation</a>
</p>

OpenCollab-Eval runs software-agent benchmarks on top of OpenCollab. For a full
SWE-bench run, it gives the Solver an isolated checkout, records the resulting
patch, and runs the official tests. OpenCollab provides the agents and
workflows. This repository evaluates them.

A task failure and an evaluation failure mean different things. `resolved`
means that the named target tests ran and passed on the patch extracted from
this run. If no tests were collected, the evidence is incomplete, the patch
identity changed, or the workspace was still being modified, the run ends as a
technical failure.

## Evaluation flow

```text
benchmark task
        |
        v
private judge data + public Solver task
        |
        v
temporary Solver checkout
        |
        v
patch built from the evaluator's baseline
        |
        v
clean checkout for official tests
        |
        v
test logs + final report
```

The Solver sees the public problem statement and a disposable checkout. The
evaluator keeps the base commit, test patch, target lists, image ID, and run ID
outside that checkout. After the Solver exits, the evaluator builds the patch
with its own Git state. The official test run checks the patch SHA-256 before
it starts.

## Supported environment

Use Python 3.10 or later with OpenCollab 0.4.x. SWE-bench evaluation also needs
Docker and the optional `swebench` dependencies. OpenHands needs Python 3.12.
For a remote Pro-Lite run, provide a Linux machine reachable through SSH. It
must have Python, the task images, and a writable directory for each run.

Install the two wheel files.

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

Keep run data outside the source checkout. This includes credentials, datasets,
predictions, trajectories, patches, reports, PDFs, and runtime logs.

## Command overview

| Command | Purpose | Produces an official result |
| --- | --- | --- |
| `oc-eval inspect` | Validate and anonymize a SWE-Batch Pro dataset census | No |
| `oc-eval run` | Run the generic evaluation engine and produce candidate eligibility records | No |
| `oc-eval swe-v1-prolite` | Generate and evaluate a selected remote Pro-Lite slice | Yes |
| `oc-eval final-report` | Validate and publish a comparison from two completed fact reports | Consumes existing verdicts |
| `python -m opencollab_eval.commands.swe_eval_run` | Select a Solver and run a chosen Pro-Lite batch | Yes |

Use `oc-eval run` when you only need candidate-generation records. It never
reports a SWE-bench task as resolved. Use `oc-eval swe-v1-prolite` or the
multi-Solver coordinator when the run must include official tests.

Use `oc-eval --help` and the subcommand help for all available options.

## Dataset inspection

Create a 32-byte identity key and keep it with the private files for the run.
Reuse the same key when retrying a batch so its public task IDs do not change.

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

The command checks the JSONL input and prints a task list with anonymized IDs.
It does not start a Solver or print private judge fields.

## Generic candidate generation

`oc-eval run` reads one JSON object per line. Each row requires `task_id` and
`description`. Optional fields are `repo_path`, `docker_image`, `timeout`,
`max_tokens`, and an `extras` object.

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

OpenCollab loads the provider configuration through its public API. Put API
keys in a secret store or a protected environment file instead of the command
line. The command writes `results.jsonl` and reports how many candidates are
eligible for evaluation.

## Official SWE Pro-Lite evaluation

`oc-eval swe-v1-prolite` copies the current OpenCollab and OpenCollab-Eval code
to the worker and checks the source hashes before starting the Solver. When the
Solver command returns, the evaluator stops any remaining Solver-owned
processes and verifies that the workspace is quiet. It then freezes the
workspace, constructs the patch, projects that patch into a clean official
checkout, and checks the resulting tree before running the declared tests. The
command writes JSON and Markdown reports.

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

Use `--dry-run` once when setting up a new worker. It checks the configuration,
paths, and task selection without creating a task result. The
[Pro-Lite operations guide](docs/swe-prolite-operations.md) covers provider
access, Solver selection, remote directories, retries, reports, and failures.

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

The command above runs K3 with a 1048576-token context and
`reasoning_effort=high`. The coordinator fills in the remaining defaults for
the selected Solver. OpenHands and Claude Code must be installed separately.
The shell adapters in this repository only invoke those runtimes.

## Results and evidence

The pipeline tracks up to three milestones before the terminal outcome.

| Milestone | Meaning |
| --- | --- |
| Candidate produced | A nonempty patch was extracted |
| Submission eligible | Candidate and lifecycle evidence passed generation checks |
| Eval done | Official evaluation produced a bound report |

After official evaluation, a task has one of three terminal outcomes.

| Outcome | Meaning |
| --- | --- |
| Resolved | The declared targets ran and passed for the bound candidate |
| Unresolved | The declared targets ran and at least one target failed |
| Technical failed | The evaluation did not produce a verifiable task result |

Each report stores the task, run, and record IDs, the full patch SHA-256, the
runtime, the target plan, the commands that ran, the cleanup result, and the
official report. These fields show which patch the tests actually used. A task
is unresolved only after the intended target ran and failed. Import errors,
collection errors, unsupported plans, missing logs, and identity mismatches are
technical failures.

The evaluator records one structured event for each Python test node. Go tests
are checked through `go test -json`. JavaScript results are read by parsers for
the supported test frameworks. Unknown target syntax is a technical failure.

## Documentation

Start with the [documentation index](docs/README.md). For a first run, use the
[getting started guide](docs/getting-started.md) and
[task format reference](docs/task-formats.md). Remote Pro-Lite work is covered
by the [operations guide](docs/swe-prolite-operations.md). The
[architecture guide](docs/architecture.md),
[evaluation integrity guide](docs/evaluation-integrity.md),
[CLI reference](docs/cli-reference.md), and
[troubleshooting guide](docs/troubleshooting.md) explain the rest of the
system.

The [final report contract](docs/final-report.md) defines the input needed to
publish a 100-task comparison. [MIGRATION.md](MIGRATION.md) explains which code
belongs in OpenCollab and which belongs here.
[CONTRIBUTING.md](CONTRIBUTING.md) covers development and review.
[SECURITY.md](SECURITY.md) gives the private vulnerability reporting process.

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

The wheel check installs both distributions in a clean environment and runs
the Eval tests against the packaged files. The deterministic E2E starts a local
fake model service and a temporary SSH server, copies the code with real
`rsync`, and runs the task in Docker. It then extracts the patch, runs the
official SWE-bench harness, checks the final report, and verifies cleanup of
the resources it created. It does not call an external model.

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing evaluation behavior.

---

<a id="simplified-chinese"></a>

<h1 align="center">OpenCollab-Eval</h1>

<p align="center"><strong>运行并核验 OpenCollab 智能体的 SWE-bench 评测</strong></p>

<p align="center"><a href="#english">English</a> · <strong>简体中文</strong></p>

<p align="center">
  <a href="#支持的环境">快速开始</a> ·
  <a href="#命令概览">命令</a> ·
  <a href="docs/zh-CN/evaluation-integrity.md">评测完整性</a> ·
  <a href="#文档">文档</a>
</p>

OpenCollab-Eval 用来评测基于 OpenCollab 构建的软件工程智能体。运行完整 SWE-bench 评测时，它会为 Solver 准备隔离的工作副本，记录最终补丁，再运行官方测试。智能体和工作流由 OpenCollab 提供，这个仓库负责评测。

题目没有做对和评测没有跑成是两回事。只有本次运行提取出的补丁通过指定目标测试，系统才会写入 `resolved`。没有收集到测试、证据不完整、补丁身份发生变化或工作区仍在被修改时，本次运行会被记为技术失败。

## 评测流程

```text
benchmark task
        |
        v
private judge data + public Solver task
        |
        v
temporary Solver checkout
        |
        v
patch built from the evaluator's baseline
        |
        v
clean checkout for official tests
        |
        v
test logs + final report
```

Solver 只能看到公开题面和一次性工作副本。基准提交、测试补丁、目标列表、镜像 ID 和运行 ID 由评测器保管，不会进入这个副本。Solver 退出后，评测器用自己的 Git 状态生成补丁。官方测试启动前还会核对补丁的 SHA-256。

## 支持的环境

OpenCollab-Eval 支持 Python 3.10 及以上版本，并与 OpenCollab 0.4.x 配套使用。运行 SWE-bench 还需要 Docker 和可选的 `swebench` 依赖。OpenHands 需要 Python 3.12。远程运行 Pro-Lite 时，还要准备一台可以通过 SSH 访问的 Linux 机器。机器上需要有 Python、任务镜像，以及每次运行独立使用的可写目录。

安装两个 wheel 文件。

```bash
python -m pip install /path/to/opencollab-0.4.x-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.1.0-py3-none-any.whl
```

需要官方评测时，安装 SWE-bench 集成。

```bash
python -m pip install '/path/to/opencollab_eval-0.1.0-py3-none-any.whl[swebench]'
```

使用源码检出时，先安装与之匹配的 OpenCollab 仓库。

```bash
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
```

运行数据应放在源码目录之外，其中包括凭据、数据集、预测、轨迹、补丁、报告、PDF 和运行日志。

## 命令概览

| 命令 | 用途 | 是否产生官方结果 |
| --- | --- | --- |
| `oc-eval inspect` | 验证 SWE-Batch Pro 数据集清单并进行匿名化 | 无 |
| `oc-eval run` | 运行通用评测引擎并生成候选资格记录 | 无 |
| `oc-eval swe-v1-prolite` | 生成并评测一组指定的远程 Pro-Lite 题目 | 有 |
| `oc-eval final-report` | 验证并发布由两份已完成事实报告构成的比较结果 | 使用现有判定 |
| `python -m opencollab_eval.commands.swe_eval_run` | 选择 Solver 并运行一组指定的 Pro-Lite 题目 | 有 |

只需要生成候选记录时，使用 `oc-eval run`。这个命令不会把 SWE-bench 任务判为 resolved。需要运行官方测试时，使用 `oc-eval swe-v1-prolite` 或多 Solver 协调器。

使用 `oc-eval --help` 和各子命令的帮助信息查看当前完整选项集。

## 数据集检查

生成一个 32 字节的身份密钥，并把它和本次运行的私有文件放在一起。同一批次重试时继续使用这个密钥，这样公开任务 ID 不会改变。

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

这个命令检查 JSONL 输入，并输出使用匿名 ID 的任务清单。它不会启动 Solver，也不会打印私有评测字段。

## 通用候选生成

`oc-eval run` 按行读取 JSON 对象。每行必须包含 `task_id` 和 `description`，还可以包含 `repo_path`、`docker_image`、`timeout`、`max_tokens` 和一个 `extras` 对象。

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

OpenCollab 通过公开 API 读取模型配置。API 密钥应放在密钥存储或受保护的环境文件中，避免写进命令行。命令会生成 `results.jsonl`，并报告有多少候选可以继续评测。

## 官方 SWE Pro-Lite 评测

`oc-eval swe-v1-prolite` 会把当前版本的 OpenCollab 和 OpenCollab-Eval 复制到工作节点，并在启动 Solver 前检查源码哈希。Solver 命令结束后，评测器会停止遗留进程，确认工作区已经静止，然后冻结工作区并构造补丁。补丁会被投影到干净的官方评测副本，生成的源码树通过核对后才会开始测试。命令最后写出 JSON 和 Markdown 报告。

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

第一次配置工作节点时，先运行一次 `--dry-run`。它会检查配置、路径和任务选择，但不会生成任务结果。[Pro-Lite 运维指南](docs/zh-CN/swe-prolite-operations.md)介绍模型访问、Solver 选择、远程目录、重试、报告和失败处理。

## Solver 选择

统一协调器支持 `g11`、`g1.1`、`baseTeam`、`TeamPro`、`openhands` 和 `claude-code`。

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

上面的命令使用 K3，上下文窗口为 1,048,576 token，并设置 `reasoning_effort=high`。协调器会补齐所选 Solver 的其余默认参数。OpenHands 和 Claude Code 需要单独安装，这个仓库中的 shell 适配器只负责调用它们。

## 结果与证据

系统会跟踪最多三个阶段性状态。

| 阶段 | 含义 |
| --- | --- |
| 候选已生成 | 已提取一个非空补丁 |
| 具备提交资格 | 候选和生命周期证据已通过生成检查 |
| 评测完成 | 官方评测已生成绑定报告 |

官方评测结束后，每道题只会有三个终态之一。

| 终态 | 含义 |
| --- | --- |
| Resolved | 指定目标针对绑定候选运行并全部通过 |
| Unresolved | 指定目标已经运行，且至少有一个目标失败 |
| 技术失败 | 评测没有产生可核验的题目结果 |

每份报告都保存任务、运行和记录 ID，以及完整的补丁 SHA-256、运行时、目标计划、实际命令、清理结果和官方报告。这些信息用于确认测试使用的确实是本次生成的补丁。只有目标确实运行并失败，任务才会被记为 unresolved。导入错误、收集错误、不支持的计划、日志缺失和身份不匹配都属于技术失败。

Python 测试由评测控制器启动，并按 Pytest 节点记录结构化事件。Go 测试通过 `go test -json` 检查。JavaScript 结果由对应测试框架的解析器读取。无法识别的目标语法会被记为技术失败。

## 文档

先从[文档索引](docs/zh-CN/README.md)开始。第一次运行可以看[入门指南](docs/zh-CN/getting-started.md)和[任务格式参考](docs/zh-CN/task-formats.md)。远程 Pro-Lite 运行见[运维指南](docs/zh-CN/swe-prolite-operations.md)。其余细节分别写在[架构指南](docs/zh-CN/architecture.md)、[评测完整性指南](docs/zh-CN/evaluation-integrity.md)、[CLI 参考](docs/zh-CN/cli-reference.md)和[故障排除指南](docs/zh-CN/troubleshooting.md)中。

[最终报告契约](docs/zh-CN/final-report.md)规定发布 100 题对比结果时需要哪些输入。[MIGRATION.zh-CN.md](MIGRATION.zh-CN.md)解释哪些代码属于 OpenCollab，哪些属于这里。[CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)介绍开发和审查流程。[SECURITY.zh-CN.md](SECURITY.zh-CN.md)说明如何私下报告安全漏洞。

OpenCollab-Eval 依据 [木兰宽松许可证第 2 版](LICENSE) 发行。依赖项和署名详情记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中。

## 开发验证

```bash
ruff check .
pytest -q
scripts/verify_wheel_contract.sh \
  /path/to/opencollab-0.4.x-py3-none-any.whl \
  /path/to/opencollab_eval-0.1.0-py3-none-any.whl
scripts/run_deterministic_swe_e2e.sh --output /tmp/oce-e2e --runs 1
```

wheel 检查会在干净环境中安装两个发行包，并针对打包后的文件运行 Eval 测试。确定性 E2E 会启动本地伪模型服务和临时 SSH 服务，用真实 `rsync` 复制代码，并在 Docker 中运行任务。随后，它会提取补丁，运行官方 SWE-bench harness，检查最终报告，并确认自己创建的资源已经清理。整个过程不会调用外部模型。

更改评测行为前，请阅读 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。
