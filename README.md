<a id="english"></a>

<h1 align="center">OpenCollab-Eval</h1>

<p align="center"><strong>Generate software patches and verify them with official benchmark tests</strong></p>

<p align="center"><strong>English</strong> · <a href="#simplified-chinese">简体中文</a></p>

<p align="center"><a href="#g22-quick-start">G22 quick start</a> · <a href="#supported-environment">Installation</a></p>

OpenCollab-Eval owns candidate generation, isolated official evaluation, and
result evidence for [OpenCollab](https://github.com/RISE-X-Lab/OpenCollab).
The current source release is **0.7.0** and requires **OpenCollab 0.7.x**.

<a id="g22-quick-start"></a>

## Run G22 with Single2

G22 runs coder A, coder B, mechanical comparison, contract adjudication, and
adoption through the existing dual-coder workflow. Its selector checks the
public requirements against concrete changed paths and evidence for each
candidate. The evaluator then tests the adopted patch in a fresh official
workspace.

The exact workflow is `validation-council-dual-coder-selection-v2`. Set
`agent_profile` to `"single2"` in the JSON configuration. The lower-level
runners expose the same selection through `--agent-profile single2`, and Eval
passes `agent_profile="single2"` to the OpenCollab public workflow API. The
default Single profile selects a different agent configuration. Coding roles use
`bash`, `file_read`, `file_write`, `apply_patch`, `git_diff`, and `grep`.
Project tests run through native Bash, and Eval retains their observed command
and execution result. The adjudicator receives the role's restricted tool set.

The tutorial below runs on one Linux worker with Docker. The model is a
user-selected OpenAI-compatible **Responses** endpoint. Set the model name,
context size, output limit, and reasoning effort to values supported by that
endpoint. The examples use maximum reasoning and generous experimental
budgets.

<a id="supported-environment"></a>

## Install matching source versions

Use Python 3.10 or later. Python 3.12 is used below. On the Linux worker,
install Docker Engine and grant the evaluation account access to its daemon.
Then create an environment outside both source checkouts.

```bash
mkdir -p "$HOME/oc-evaluation"
cd "$HOME/oc-evaluation"
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./OpenCollab
python -m pip install -e './OpenCollab-Eval[swebench]'
python -c 'from importlib.metadata import version; print("OC", version("opencollab")); print("OCE", version("opencollab-eval"))'
cd OpenCollab-Eval
```

The printed package versions should both be 0.7.x. The editable installation
uses the code in the two clones. `oc-eval --version` reports the installed Eval
version. [CONTRIBUTING.md](CONTRIBUTING.md) covers development setup.

## Prepare the dataset and task images

The [official release instructions](https://github.com/scaleapi/SWE-bench_Pro-os)
use `ScaleAI/SWE-bench_Pro` with `split="test"` and publish task images in
`jefzda/sweap-images`. Use the
[task format reference](docs/task-formats.md) to check the fields. The official runner reads the dataset from
`<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl`; each index refers to
one nonempty row in that file's stable order.

A complete row carries `instance_id`, `repo`, `problem_statement`,
`requirements`, `interface`, `base_commit`, the task image tag, `test_patch`,
`FAIL_TO_PASS`, and `PASS_TO_PASS`. Keep the evaluator's judge fields in this
trusted file. Generation constructs the public task from the issue,
requirements, and interface, while the official test data stays with Eval.

The commands below create run storage outside the repositories. Replace the
model and API URL placeholders. The public image repository is supplied below;
a compatible image mirror can be configured through `IMAGE_REPOSITORY`. Set the
context and output limits to the endpoint's actual supported values.

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
export MODEL='your-responses-model'
export MODEL_API_BASE='https://api.example.com/v1'
export CONTEXT_WINDOW=200000
export MAX_OUTPUT_TOKENS=16000
export IMAGE_REPOSITORY='jefzda/sweap-images'
umask 077
mkdir -p "$EVAL_ROOT/secrets" "$EVAL_ROOT/datasets/swe-batch-pro-lite" "$EVAL_ROOT/results"
docker info
```

Download the official test split and export its rows in order. The installed
SWE-bench dependencies include the dataset client used by this command.

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" <<'PY_DATASET'
import json
import sys
from pathlib import Path
from datasets import load_dataset

dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
destination = Path(sys.argv[1])
destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open("w", encoding="utf-8") as stream:
    for row in dataset:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
destination.chmod(0o600)
print(len(dataset), destination)
PY_DATASET
```

If you already have an ordered task selection, copy that JSONL instead of
running the official export.

```bash
install -m 600 /path/to/instances.jsonl \
  "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl"
```

Task indices follow the file you use. Rows 1 through 50 of the official test
split are a different selection from a historical Mini B 50-task experiment.
To reproduce a reported subset, use that experiment's original ordered file.

Pull the first selected task's image for the smoke run. When rows carry only
`dockerhub_tag`, `IMAGE_REPOSITORY` must be the matching repository prefix
supplied with the benchmark. The official runner uses `dockerhub_tag` or
`image_tag`, and derives a tag from `instance_id` when neither is supplied. A
tag containing `/` is already a complete image reference. For a batch, pull
the images for all selected rows before starting it. Keep the image's public
language dependencies available. Official images carry the repository under
`/app`. During container preparation, Eval recognizes `/app` and other
supported checkout locations and creates the `/testbed` entry used by the
Solver. It then activates the task's prepared language environment.

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" "$IMAGE_REPOSITORY" <<'PY_IMAGES'
import json
import subprocess
import sys
from pathlib import Path
rows = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
row = rows[0]
tag = row.get("dockerhub_tag") or row.get("image_tag")
if not tag:
    identity = row["instance_id"]
    tag = identity.removeprefix("instance_")
image = tag if "/" in tag else sys.argv[2].rstrip(":/") + ":" + tag
subprocess.run(["docker", "pull", image], check=True)
print(image)
PY_IMAGES
```

## Configure the model and start the relay

Create a protected provider file. The prompt reads the API key without echoing
it. The relay reads the upstream credential from this file and gives the
Solver a separate client token.

```bash
python - "$EVAL_ROOT/secrets/provider.env" <<'PY_SECRET'
import getpass
import os
import secrets
import shlex
import sys
from pathlib import Path
values = {
    "OPENCOLLAB_UPSTREAM_BASE_URL": os.environ["MODEL_API_BASE"].strip(),
    "OPENCOLLAB_UPSTREAM_API_KEY": getpass.getpass("Provider API key > "),
    "OPENCOLLAB_PROXY_CLIENT_TOKEN": secrets.token_urlsafe(32),
}
path = Path(sys.argv[1])
path.write_text("".join(key + "=" + shlex.quote(value) + "\n" for key, value in values.items()))
path.chmod(0o600)
print(path)
PY_SECRET
```

From the activated environment on the Linux worker, start the loopback relay
in the background. Its PID and log stay in evaluator storage.

```bash
nohup python -m opencollab_eval.commands.llm_api_proxy \
  --env-file "$EVAL_ROOT/secrets/provider.env" \
  --host 127.0.0.1 --port 18080 \
  --timeout 3600 --direct-upstream \
  > "$EVAL_ROOT/relay.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$EVAL_ROOT/relay.pid"
cat "$EVAL_ROOT/relay.pid"
tail -n 20 "$EVAL_ROOT/relay.log"
```

The base URL should be the endpoint's API base, for example
`https://api.example.com/v1`; Eval appends the Responses request path. The
endpoint must support streamed Responses, function tools, and the selected
reasoning effort. The relay's `--direct-upstream` forwards the standard request
to that configured endpoint. Use your provider's request quota when choosing
batch workers. Worker count and actual upstream request concurrency describe
different resources.

## Execute a single task and its official tests

Use the same activated environment and run from the `OpenCollab-Eval`
checkout. Keep the variables from dataset setup available. Write the run configuration
once. Its paths point to evaluator storage outside the source checkouts.

```bash
python - "$EVAL_ROOT/g22.json" <<'PY_CONFIG'
import json
import os
import sys
from pathlib import Path
root = Path(os.environ["EVAL_ROOT"]).expanduser().resolve()
config = {
    "remote_root": str(root),
    "remote_python": sys.executable,
    "image_repository": os.environ["IMAGE_REPOSITORY"],
    "proxy_env_file": str(root / "secrets/provider.env"),
    "local_proxy_base_url": "http://127.0.0.1:18080/v1",
    "remote_proxy_base_url": "http://127.0.0.1:18080/v1",
    "llm_model": os.environ["MODEL"],
    "context_window": int(os.environ["CONTEXT_WINDOW"]),
    "max_output_tokens": int(os.environ["MAX_OUTPUT_TOKENS"]),
    "llm_timeout": 3600,
    "agent_profile": "single2",
    "workflow_env": {
        "OPENCOLLAB_WIRE_PROTOCOL": "responses",
        "OPENCOLLAB_REASONING_EFFORT": "max",
        "OPENCOLLAB_UNBOUNDED_LIMITS": "true",
        "OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT": "43200",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT": "900",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT": "900",
    },
}
Path(sys.argv[1]).write_text(json.dumps(config, indent=2) + "\n")
PY_CONFIG
```

`oc-eval g22` selects the G22 workflow, the Single2 role profile, and the
existing official parallel runner. The default configuration removes cumulative
token and step caps through `OPENCOLLAB_UNBOUNDED_LIMITS=true`. The wrapper
provides 1000000000000 token and step values as fallback settings. To enforce
explicit ceilings, set that workflow variable to `false` and configure
`budget` and `max_steps` in the JSON. The 43200-second no-progress policy
observes actual model content, model completion, and tool execution. Headers
and keepalive traffic do not extend it. Current
runtime supervision removes the legacy cumulative generation and controller
wall limits when this progress policy is selected. Explicit generation or
controller wall-limit settings can enable a cumulative limit again.

Run the command below to generate a real patch and execute the official
FAIL_TO_PASS and PASS_TO_PASS targets for the adopted candidate.

```bash
oc-eval g22 --config "$EVAL_ROOT/g22.json" \
  --indices 1 --workers 1 --run-id g22-smoke-001
```

The runner packages the installed OC and OCE sources, verifies the worker
runtime, prepares an isolated public source checkout, runs G22, captures the
quiet candidate, and applies it to a separate official workspace. The command
writes the resulting JSON and Markdown reports. `--help` and `--dry-run` can
check options or plans. A smoke result comes from this actual generation and
official execution command.

Read the official outcome and its report path.

```bash
python - "$EVAL_ROOT/results/g22-smoke-001/task_1_report.json" <<'PY_RESULT'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
for row in report.get("rows", []):
    result = row.get("task_result", {})
    evaluation = row.get("eval", {})
    print(row.get("index"), result)
    print("official report", evaluation.get("report_path"))
PY_RESULT
```

`resolved=true` is supported by the bound official target execution.
`oc_failure=true` identifies a Solver failure. `technical_failure=true`
identifies missing or invalid evaluation evidence and is tracked separately.
An unresolved functional result is a valid smoke outcome when the official
execution completed correctly. Check the bound report before starting a large
batch.

## Run a batch and read results

The same configuration can run the remaining 49 tasks through the existing
parallel runner. Keep the smoke outcome for row 1. This example uses four
task workers for rows 2 through 50 and starts the coordinator in the background.

```bash
nohup oc-eval g22 --config "$EVAL_ROOT/g22.json" \
  --indices 2-50 --workers 4 --run-id g22-batch-001 \
  > "$EVAL_ROOT/results/g22-batch-001.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$EVAL_ROOT/results/g22-batch-001.pid"
cat "$EVAL_ROOT/results/g22-batch-001.pid"
tail -n 20 "$EVAL_ROOT/results/g22-batch-001.log"
```

The relay and batch coordinator run on the worker after SSH disconnects or the
control computer shuts down. The stored PID identifies each process; logs and
reports remain available on the worker.

The smoke and batch have distinct run IDs and output directories. Keep both
sets of reports when presenting the complete 50-task result. Each selected row
appears once in this sequence. To start an independent full batch, select
`--indices 1-50` with a new run ID. Use the selected dataset's actual index range
for other sample sizes.

Reports default to `<config-directory>/results/<run-id>`. `--output-dir` changes
the report destination, and `--run-id` names an execution. Reusing a completed
run ID refers to that existing run. For an SSH worker, configure
`runner_transport`, `host`, and the corresponding worker paths through the
[operations guide](docs/swe-prolite-operations.md).

| Output | Use |
| --- | --- |
| `parallel_summary.json` | Task census, progress, terminal results, and failures |
| `task_<index>_report.json` | Candidate generation, official evaluation, and attribution |
| `final_eval_layer_report.json` | Completed batch fact report |
| Task `metrics.jsonl` and `predictions.jsonl` | Model usage and adopted patch record |
| Task `workflow_logs/` | Orchestration events and role snapshots with journals |
| `rows[].eval.report_path` | Exact official report for the scored candidate |

Preserve the complete run evidence. Reading a role snapshot includes its
`.json.journal`. For a recovered session, retain both the original and the
continuation traces. Cumulative usage must deduplicate copied prefixes through
the existing event identity. The report's record ID and patch identify the
candidate whose tests produced the outcome.

`oc-eval rejudge-queue` provides official evaluation-only maintenance for
existing verified candidates. It uses zero Solver starts and retains attempts
and bound outcomes. An external
[scoring adapter registry](docs/scoring-adapters.md) may be supplied through
`--scoring-adapter-registry /absolute/path/registry.json` when the benchmark's
published interface needs a registered test-fixture adaptation.

## Other entrypoints and documentation

| Entry | Purpose |
| --- | --- |
| `oc-eval g22 --config /path/g22.json` | G22 with Single2 and official scoring |
| `oc-eval inspect` | Inspect and anonymize a trusted dataset |
| `oc-eval run` | Generic task candidate generation |
| `oc-eval swe-v1-prolite` | Generation and official scoring for a slice |
| `oc-eval package-runtime` | Prepare installed public OC and OCE sources |
| `oc-eval rejudge-queue` | Officially score existing bound candidates |
| `oc-eval final-report` | Publish a comparison from completed fact reports |

The [operations guide](docs/swe-prolite-operations.md) covers separate control
and worker machines with SSH. The [documentation index](docs/README.md),
[CLI reference](docs/cli-reference.md),
[evaluation integrity guide](docs/evaluation-integrity.md), and
[troubleshooting guide](docs/troubleshooting.md) cover the remaining details.

## Advanced Solver and provider reference

<details>
<summary>Other bundled Solvers and complete Kimi examples</summary>

The [operations guide](docs/swe-prolite-operations.md) describes these existing
Solver profiles and the coordinator's transport setup. G22 continues to use
its own configuration entrypoint shown above.

| Solver | Workflow |
| --- | --- |
| `g11` | `validation-council-solve` |
| `g1.1` | `validation-council-solve` |
| `g20-exp1` | `evidence-action-council-v1` |
| `g20-exp2` | `candidate-tournament-council-v1` |
| `g11-wired` | `validation-council-wired-v1` |
| `baseTeam` | `base-team` |
| `TeamPro` | `team-pro` |
| `openhands` | `openhands-external` |
| `claude-code` | `openhands-external` |

The direct Kimi coding profile reads a mode-0600 worker environment file with
`KIMI_API_KEY` or `OPENAI_API_KEY`. Replace the worker and directory
placeholders in this complete slice example.

```bash
oc-eval swe-v1-prolite \
  --host evaluator@example-worker \
  --ssh-command ssh \
  --remote-root /srv/opencollab-eval \
  --remote-runtime-repo /srv/opencollab-eval/runtime \
  --base-run-dir /srv/opencollab-eval/runs/example-001 \
  --run-id example-001 \
  --session-prefix example-001 \
  --start-index 1 \
  --limit 1 \
  --workflow validation-council-solve \
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

The coordinator also supports the complete K3 G11 profile below. It applies
`reasoning_effort=high` together with its model, context, sampling, and output
settings. These Kimi examples use their respective provider profiles.

```bash
python -m opencollab_eval.commands.swe_eval_run \
  --indices 1-4 \
  --solver g11 \
  --workers 2 \
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
  --max-eval-attempts 1 \
  --runner-attempts 1
```

</details>

## Development

```bash
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

OpenCollab-Eval is licensed under [MulanPSL-2.0](LICENSE).
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records dependency notices.
[CHANGELOG.md](CHANGELOG.md) records release changes.

---

<a id="simplified-chinese"></a>

<h1 align="center">OpenCollab-Eval</h1>

**生成软件补丁，并用基准正式测试验证结果**

<p align="center"><a href="#english">English</a> · <strong>简体中文</strong></p>

<p align="center"><a href="#g22-quick-start-zh-cn">G22 快速开始</a> · <a href="#支持的环境">安装</a></p>

本文对应上方[英文原文](#english)。OpenCollab-Eval 负责
[OpenCollab](https://github.com/RISE-X-Lab/OpenCollab) 的候选生成、隔离正式评测与结果证据。
当前源码版本为 **0.7.0**，依赖 **OpenCollab 0.7.x**。

<a id="g22-quick-start-zh-cn"></a>

## 用 Single2 运行 G22

G22 沿已有双 coder 工作流依次执行 coder A、coder B、机械比较、公开要求裁决与候选采用。
选择者逐项核对公开要求，并引用对应候选的实际修改路径与证据。
评测器随后在新的正式工作区测试采用补丁。

准确的 workflow 名称为 `validation-council-dual-coder-selection-v2`。
请在 JSON 配置中将 `agent_profile` 显式设为 `"single2"`。底层 runner 使用
`--agent-profile single2` 选择相同配置，Eval 将 `agent_profile="single2"` 传入 OpenCollab 公开 workflow API。
默认 Single profile 使用另一套 agent 配置。
coder 可用的原生工具为 `bash`、`file_read`、`file_write`、`apply_patch`、`git_diff` 与 `grep`。
项目测试通过原生 Bash 执行，Eval 保留观察到的命令与执行结果。
裁决角色使用该角色受限的工具集合。

下方教程在一台有 Docker 的 Linux worker 上运行。
模型使用用户配置的 OpenAI 兼容 **Responses** 入口。
模型名称、上下文容量、输出上限与推理强度需要符合该入口的实际支持情况。
示例采用最大推理强度与充分实验预算。

<a id="supported-environment-zh-cn"></a>
<a id="支持的环境"></a>

## 安装匹配的源码版本

需要 Python 3.10 或更新版本，下方使用 Python 3.12。
先在 Linux worker 安装 Docker Engine，并允许评测账号访问 daemon。
随后在两个源码仓库之外创建环境。

```bash
mkdir -p "$HOME/oc-evaluation"
cd "$HOME/oc-evaluation"
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./OpenCollab
python -m pip install -e './OpenCollab-Eval[swebench]'
python -c 'from importlib.metadata import version; print("OC", version("opencollab")); print("OCE", version("opencollab-eval"))'
cd OpenCollab-Eval
```

输出的两个包版本均应为 0.7.x。editable 安装使用两个 clone 内的源码。
`oc-eval --version` 显示已安装的 Eval 版本。
开发环境说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 准备数据与题目镜像

[官方发布说明](https://github.com/scaleapi/SWE-bench_Pro-os)使用
`ScaleAI/SWE-bench_Pro` 的 `split="test"`，题目镜像发布在 `jefzda/sweap-images`。
字段说明见[任务格式参考](docs/zh-CN/task-formats.md)。正式 runner 固定读取
`<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl`。
索引对应文件中非空行的稳定顺序。

完整题目行包含 `instance_id`、`repo`、`problem_statement`、`requirements`、`interface`、
`base_commit`、题目镜像 tag、`test_patch`、`FAIL_TO_PASS` 与 `PASS_TO_PASS`。
评测器字段保存在可信数据中。生成时根据 issue、requirements 与 interface 构造公开题面，
正式测试数据由 Eval 保管。

以下命令在仓库之外创建运行目录。替换模型名称与 API 地址，示例已配置公开镜像 repository。
需要镜像站时，可通过 `IMAGE_REPOSITORY` 配置兼容的镜像 repository。
把上下文与输出上限改成入口实际支持的数值。

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
export MODEL='your-responses-model'
export MODEL_API_BASE='https://api.example.com/v1'
export CONTEXT_WINDOW=200000
export MAX_OUTPUT_TOKENS=16000
export IMAGE_REPOSITORY='jefzda/sweap-images'
umask 077
mkdir -p "$EVAL_ROOT/secrets" "$EVAL_ROOT/datasets/swe-batch-pro-lite" "$EVAL_ROOT/results"
docker info
```

下载官方 test split，并按原顺序导出各行。已安装的 SWE-bench 依赖包含该命令使用的数据集客户端。

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" <<'PY_DATASET'
import json
import sys
from pathlib import Path
from datasets import load_dataset

dataset = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
destination = Path(sys.argv[1])
destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open("w", encoding="utf-8") as stream:
    for row in dataset:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
destination.chmod(0o600)
print(len(dataset), destination)
PY_DATASET
```

已有按顺序选好的题单时，可复制该 JSONL 替代官方导出。

```bash
install -m 600 /path/to/instances.jsonl \
  "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl"
```

题目索引由实际使用的文件决定。官方 test split 的前 50 行与历史 Mini B 50 题实验使用不同选集。
复现已报告子集时，使用该实验的原有顺序文件。

先为单题执行拉取第一个选中题目的镜像。
数据仅带 `dockerhub_tag` 时，`IMAGE_REPOSITORY` 应为基准配套的镜像 repository 前缀。
正式 runner 使用 `dockerhub_tag` 或 `image_tag`，缺少两者时从 `instance_id` 派生 tag。
tag 含 `/` 时视为完整镜像引用。批量执行前准备全部选中题目的镜像，并保留镜像内公开的语言依赖。
官方镜像的 repository 位于 `/app`。容器准备时，Eval 识别 `/app` 等支持的 checkout 位置，
建立 Solver 使用的 `/testbed` 入口，随后激活题目已准备的语言环境。

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" "$IMAGE_REPOSITORY" <<'PY_IMAGES'
import json
import subprocess
import sys
from pathlib import Path
rows = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
row = rows[0]
tag = row.get("dockerhub_tag") or row.get("image_tag")
if not tag:
    identity = row["instance_id"]
    tag = identity.removeprefix("instance_")
image = tag if "/" in tag else sys.argv[2].rstrip(":/") + ":" + tag
subprocess.run(["docker", "pull", image], check=True)
print(image)
PY_IMAGES
```

## 配置模型并启动 relay

创建权限受限的 provider 文件，API key 通过隐藏输入读取。
relay 从文件读取上游凭据，并为 Solver 使用单独的 client token。

```bash
python - "$EVAL_ROOT/secrets/provider.env" <<'PY_SECRET'
import getpass
import os
import secrets
import shlex
import sys
from pathlib import Path
values = {
    "OPENCOLLAB_UPSTREAM_BASE_URL": os.environ["MODEL_API_BASE"].strip(),
    "OPENCOLLAB_UPSTREAM_API_KEY": getpass.getpass("Provider API key > "),
    "OPENCOLLAB_PROXY_CLIENT_TOKEN": secrets.token_urlsafe(32),
}
path = Path(sys.argv[1])
path.write_text("".join(key + "=" + shlex.quote(value) + "\n" for key, value in values.items()))
path.chmod(0o600)
print(path)
PY_SECRET
```

在 Linux worker 的已激活环境中启动后台 loopback relay，PID 与日志保存在评测目录中。

```bash
nohup python -m opencollab_eval.commands.llm_api_proxy \
  --env-file "$EVAL_ROOT/secrets/provider.env" \
  --host 127.0.0.1 --port 18080 \
  --timeout 3600 --direct-upstream \
  > "$EVAL_ROOT/relay.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$EVAL_ROOT/relay.pid"
cat "$EVAL_ROOT/relay.pid"
tail -n 20 "$EVAL_ROOT/relay.log"
```

base URL 使用入口的 API 根路径，例如 `https://api.example.com/v1`。
Eval 会追加 Responses 请求路径。入口需要支持流式 Responses、function tools 与选定推理强度。
relay 的 `--direct-upstream` 将标准请求转发到配置的入口。
批量任务位数应结合供应商请求额度选择，任务位数与实际上游请求并发描述不同资源。

## 执行单题与正式测试

在相同的已激活环境中，从 `OpenCollab-Eval` checkout 执行命令。
保留数据准备时设置的变量，并一次写入运行配置。配置中的路径指向源码仓库之外的评测目录。

```bash
python - "$EVAL_ROOT/g22.json" <<'PY_CONFIG'
import json
import os
import sys
from pathlib import Path
root = Path(os.environ["EVAL_ROOT"]).expanduser().resolve()
config = {
    "remote_root": str(root),
    "remote_python": sys.executable,
    "image_repository": os.environ["IMAGE_REPOSITORY"],
    "proxy_env_file": str(root / "secrets/provider.env"),
    "local_proxy_base_url": "http://127.0.0.1:18080/v1",
    "remote_proxy_base_url": "http://127.0.0.1:18080/v1",
    "llm_model": os.environ["MODEL"],
    "context_window": int(os.environ["CONTEXT_WINDOW"]),
    "max_output_tokens": int(os.environ["MAX_OUTPUT_TOKENS"]),
    "llm_timeout": 3600,
    "agent_profile": "single2",
    "workflow_env": {
        "OPENCOLLAB_WIRE_PROTOCOL": "responses",
        "OPENCOLLAB_REASONING_EFFORT": "max",
        "OPENCOLLAB_UNBOUNDED_LIMITS": "true",
        "OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT": "43200",
        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT": "900",
        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT": "900",
    },
}
Path(sys.argv[1]).write_text(json.dumps(config, indent=2) + "\n")
PY_CONFIG
```

`oc-eval g22` 会选择 G22 workflow、Single2 角色 profile 与已有正式并行 runner。
默认配置通过 `OPENCOLLAB_UNBOUNDED_LIMITS=true` 移除累计 token 与步数上限，
入口为 token 与步数提供 1000000000000 的回退值。
需要显式预算上限时，将该 workflow 变量设为 `false`，并在 JSON 中设置 `budget` 与 `max_steps`。
43200 秒无进展策略观察真实模型内容、模型完成与工具执行，HTTP 头和 keepalive 流量不会延长计时。
当前运行时选用进展策略后会移除旧的生成阶段与控制器累计 wall 限制。
显式设置生成或控制器 wall 上限可以重新启用累计限制。

以下命令会真实生成补丁，并为采用候选执行正式 FAIL_TO_PASS 与 PASS_TO_PASS 目标。

```bash
oc-eval g22 --config "$EVAL_ROOT/g22.json" \
  --indices 1 --workers 1 --run-id g22-smoke-001
```

runner 打包已安装的 OC 与 OCE 源码，验证 worker 运行包，准备隔离的公开源码 checkout，
执行 G22，捕获已停止写入的候选，并将其投影到独立正式工作区。
命令会写出 JSON 与 Markdown 报告。`--help` 与 `--dry-run` 用于查看选项或计划，
smoke 结果来自上述真实生成与正式执行命令。

读取正式结果与对应报告路径。

```bash
python - "$EVAL_ROOT/results/g22-smoke-001/task_1_report.json" <<'PY_RESULT'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
for row in report.get("rows", []):
    result = row.get("task_result", {})
    evaluation = row.get("eval", {})
    print(row.get("index"), result)
    print("official report", evaluation.get("report_path"))
PY_RESULT
```

`resolved=true` 有绑定候选的正式目标执行证据支持。
`oc_failure=true` 表示 Solver 自身失败，`technical_failure=true` 表示评测证据缺失或无效，单独统计。
正式执行完整且候选功能未通过时，unresolved 也是有效的 smoke 结果。
批量执行前检查该候选的绑定报告。

## 批量执行与结果阅读

相同配置可以通过已有并行 runner 执行其余 49 题，保留 smoke 第 1 题结果。
示例使用四个任务位执行第 2 至第 50 行，并将协调器放入后台。

```bash
nohup oc-eval g22 --config "$EVAL_ROOT/g22.json" \
  --indices 2-50 --workers 4 --run-id g22-batch-001 \
  > "$EVAL_ROOT/results/g22-batch-001.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$EVAL_ROOT/results/g22-batch-001.pid"
cat "$EVAL_ROOT/results/g22-batch-001.pid"
tail -n 20 "$EVAL_ROOT/results/g22-batch-001.log"
```

SSH 断开或控制电脑关机后，relay 与批次协调器仍在 worker 上执行。
保存的 PID 用于识别对应进程，日志与报告可以继续从 worker 查看。

smoke 与 batch 使用不同 run ID 和输出目录。展示完整 50 题结果时同时保留两份报告，
以上顺序使每个题目选入一次。独立执行新的完整批次时，使用 `--indices 1-50` 与新 run ID。
其他样本规模使用对应数据集的实际索引范围。

报告默认写入 `<config-directory>/results/<run-id>`。`--output-dir` 可改报告目录，
`--run-id` 命名一次执行。复用已完成的 run ID 会引用已有运行。
SSH worker 需要在配置中设置 `runner_transport`、`host` 与对应 worker 路径，具体见
[运维指南](docs/zh-CN/swe-prolite-operations.md)。

| 输出 | 用途 |
| --- | --- |
| `parallel_summary.json` | 题目全集、进度、终态与故障 |
| `task_<index>_report.json` | 候选生成、正式评测与责任归属 |
| `final_eval_layer_report.json` | 已完成批次的事实报告 |
| 题目 `metrics.jsonl` 与 `predictions.jsonl` | 模型用量与采用补丁记录 |
| 题目 `workflow_logs/` | 编排事件及角色快照与 journal |
| `rows[].eval.report_path` | 该候选实际计分的正式报告 |

保留完整运行证据。读取角色快照时应同时读取其 `.json.journal`。
恢复会话保留原始轨迹与后续轨迹，用量累计使用已有事件标识去重复制的前缀。
报告中的 record ID 与补丁确定产生该测试结果的候选。

`oc-eval rejudge-queue` 用于已验证候选的正式 eval-only 维护，Solver starts 为零，
并保留尝试记录与绑定结果。基准公开接口需要已注册的测试 fixture 适配时，
可以通过 `--scoring-adapter-registry /absolute/path/registry.json` 提供外部
[评分适配 registry](docs/zh-CN/scoring-adapters.md)。

## 其他入口与文档

| 入口 | 用途 |
| --- | --- |
| `oc-eval g22 --config /path/g22.json` | G22 Single2 与正式评分 |
| `oc-eval inspect` | 检查可信数据并匿名化 |
| `oc-eval run` | 通用题目的候选生成 |
| `oc-eval swe-v1-prolite` | 一个 slice 的生成与正式评分 |
| `oc-eval package-runtime` | 准备已安装的公开 OC 与 OCE 源码 |
| `oc-eval rejudge-queue` | 正式评分已有绑定候选 |
| `oc-eval final-report` | 从已完成事实报告发布对比 |

[运维指南](docs/zh-CN/swe-prolite-operations.md)介绍控制机与 worker 分离的 SSH 执行方式。
更多细节见[文档索引](docs/zh-CN/README.md)、[CLI 参考](docs/zh-CN/cli-reference.md)、
[评测完整性指南](docs/zh-CN/evaluation-integrity.md)与[故障排除指南](docs/zh-CN/troubleshooting.md)。

## 高级 Solver 与 provider 参考

<details>
<summary>其他内置 Solver 与完整 Kimi 示例</summary>

[运维指南](docs/zh-CN/swe-prolite-operations.md)介绍这些已有 Solver profile 与协调器的传输配置。
G22 继续使用上方独立配置入口。

| Solver | Workflow |
| --- | --- |
| `g11` | `validation-council-solve` |
| `g1.1` | `validation-council-solve` |
| `g20-exp1` | `evidence-action-council-v1` |
| `g20-exp2` | `candidate-tournament-council-v1` |
| `g11-wired` | `validation-council-wired-v1` |
| `baseTeam` | `base-team` |
| `TeamPro` | `team-pro` |
| `openhands` | `openhands-external` |
| `claude-code` | `openhands-external` |

直接 Kimi coding profile 从 worker 的 0600 权限环境文件读取 `KIMI_API_KEY` 或 `OPENAI_API_KEY`。
以下完整 slice 示例中的 worker 与目录为待替换值。

```bash
oc-eval swe-v1-prolite \
  --host evaluator@example-worker \
  --ssh-command ssh \
  --remote-root /srv/opencollab-eval \
  --remote-runtime-repo /srv/opencollab-eval/runtime \
  --base-run-dir /srv/opencollab-eval/runs/example-001 \
  --run-id example-001 \
  --session-prefix example-001 \
  --start-index 1 \
  --limit 1 \
  --workflow validation-council-solve \
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

协调器也支持下方完整 K3 G11 profile。
它会将 `reasoning_effort=high` 与对应模型、上下文、采样和输出配置一起应用。
两个 Kimi 示例使用各自的 provider profile。

```bash
python -m opencollab_eval.commands.swe_eval_run \
  --indices 1-4 \
  --solver g11 \
  --workers 2 \
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
  --max-eval-attempts 1 \
  --runner-attempts 1
```

</details>

## 开发

```bash
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

OpenCollab-Eval 依据[木兰宽松许可证第 2 版](LICENSE)发行。
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)记录依赖项声明，
[CHANGELOG.md](CHANGELOG.md)记录版本变化。
