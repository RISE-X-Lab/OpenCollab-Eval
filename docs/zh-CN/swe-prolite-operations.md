# SWE Pro-Lite 操作指南

[English](../swe-prolite-operations.md) | **简体中文**

新的 OpenCollab 评测推荐使用 Duo。首个教程在一台 Linux worker 上运行控制器、relay、Docker、候选生成与正式评测。后续章节介绍 SSH worker 与仍可使用的研究工作流。示例模型地址和 worker 路径需要替换为实际设置。

## 在一台 Linux 工作机上运行 Duo

按[快速开始](getting-started.md)安装匹配的 OpenCollab 0.9.x 与带 SWE-bench extra 的 OpenCollab-Eval 0.9.1。在 Linux worker 上激活该 Python 环境，并允许评测账号访问 Docker Engine。配置支持流式 Responses、function tools 和所选推理设置的 OpenAI 兼容入口。后续命令始终在这个已激活环境中执行。

### 准备数据与题目镜像

[官方发布说明](https://github.com/scaleapi/SWE-bench_Pro-os)使用
保留 `ScaleAI/SWE-bench_Pro` 的 `v1` 配置及 `split="test"`，对应题目镜像发布在 `jefzda/sweap-images`。
字段说明见[任务格式参考](task-formats.md)。正式 runner 固定读取
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

数据集默认配置现为 V2 Harbor 任务。此 runner 与 Docker Hub 镜像使用显式选择的 V1。下载 V1 test split，并按原顺序导出各行。已安装的 SWE-bench 依赖包含该命令使用的数据集客户端。

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" <<'PY_DATASET'
import json
import sys
from pathlib import Path
from datasets import load_dataset

dataset = load_dataset("ScaleAI/SWE-bench_Pro", "v1", split="test")
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

### 配置模型并启动 relay

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

### 执行单题与正式测试

在相同的已激活环境中，从 `OpenCollab-Eval` checkout 执行命令。
保留数据准备时设置的变量，并一次写入运行配置。配置中的路径指向源码仓库之外的评测目录。

```bash
python - "$EVAL_ROOT/duo.json" <<'PY_CONFIG'
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
    "agent_profile": "base",
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

`oc-eval duo` 会选择 Duo workflow、解析为 Single2 的 Base 角色 profile 与已有正式并行 runner。
默认配置通过 `OPENCOLLAB_UNBOUNDED_LIMITS=true` 移除累计 token 与步数上限，
入口为 token 与步数提供 1000000000000 的回退值。
需要显式预算上限时，将该 workflow 变量设为 `false`，并在 JSON 中设置 `budget` 与 `max_steps`。
43200 秒无进展策略观察真实模型内容、模型完成与工具执行，HTTP 头和 keepalive 流量不会延长计时。
当前运行时选用进展策略后会移除旧的生成阶段与控制器累计 wall 限制。
显式设置生成或控制器 wall 上限可以重新启用累计限制。

以下命令会真实生成补丁，并为采用候选执行正式 FAIL_TO_PASS 与 PASS_TO_PASS 目标。

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 1 --workers 1 --run-id duo-smoke-001
```

runner 打包已安装的 OC 与 OCE 源码，验证 worker 运行包，准备隔离的公开源码 checkout，
执行 Duo，捕获已停止写入的候选，并将其投影到独立正式工作区。
命令会写出 JSON 与 Markdown 报告。`--help` 与 `--dry-run` 用于查看选项或计划，
smoke 结果来自上述真实生成与正式执行命令。

读取正式结果与对应报告路径。

```bash
python - "$EVAL_ROOT/results/duo-smoke-001/task_1_report.json" <<'PY_RESULT'
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
先读 `task_result.status`，终态为 `resolved`、`unresolved` 与 `technical_failure`。任务仍在等待评测时也可能显示 `resolved` 为 false。
正式执行完整且候选功能未通过时，unresolved 也是有效的 smoke 结果。
批量执行前检查该候选的绑定报告。

### 批量执行与结果阅读

使用同一份有序 V1 文件准备第 2 至第 50 行的镜像。选择其他批次时，相应调整切片。

```bash
python - "$EVAL_ROOT/datasets/swe-batch-pro-lite/instances.jsonl" "$IMAGE_REPOSITORY" <<'PY_BATCH_IMAGES'
import json
import subprocess
import sys
from pathlib import Path
rows = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
for row in rows[1:50]:
    tag = row.get("dockerhub_tag") or row.get("image_tag") or row["instance_id"].removeprefix("instance_")
    image = tag if "/" in tag else sys.argv[2].rstrip(":/") + ":" + tag
    subprocess.run(["docker", "pull", image], check=True)
PY_BATCH_IMAGES
```

相同配置可以通过已有并行 runner 执行其余 49 题，保留 smoke 第 1 题结果。
示例使用四个任务位执行第 2 至第 50 行，并将协调器放入后台。

```bash
nohup oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 2-50 --workers 4 --run-id duo-batch-001 \
  > "$EVAL_ROOT/results/duo-batch-001.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$EVAL_ROOT/results/duo-batch-001.pid"
cat "$EVAL_ROOT/results/duo-batch-001.pid"
tail -n 20 "$EVAL_ROOT/results/duo-batch-001.log"
```

SSH 断开或控制电脑关机后，relay 与批次协调器仍在 worker 上执行。
保存的 PID 用于识别对应进程，日志与报告可以继续从 worker 查看。

smoke 与 batch 使用不同 run ID 和输出目录。展示完整 50 题结果时同时保留两份报告，
以上顺序使每个题目选入一次。独立执行新的完整批次时，使用 `--indices 1-50` 与新 run ID。
其他样本规模使用对应数据集的实际索引范围。

报告默认写入 `<config-directory>/results/<run-id>`。`--output-dir` 可改报告目录，
`--run-id` 命名一次执行。复用已完成的 run ID 会引用已有运行。
SSH worker 需要在配置中设置 `runner_transport`、`host` 与对应 worker 路径，具体见
[运维指南](#高级-worker-拓扑)。

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
[评分适配 registry](scoring-adapters.md)。

### 继续中断的运行

确认前一个控制器已经退出后，检查 `parallel_summary.json` 与各题报告。从未启动的题目可以继续调度，已完成且证据相符的题目会复用保存报告。选中的题目处于这两种状态时，用相同配置、run ID 和输出目录再次执行下面的命令。处理其他选题范围时，将 `--indices` 设为准备继续处理的已完成或从未启动题号。

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 2-50 --workers 4 --run-id duo-batch-001
```

runner 启动后中断的题目会在 worker 题目目录留下所有权记录或 summary。即使原拥有进程已经退出，local transport 仍会拒绝在该目录再次启动。按照[评测套件](evaluation-suite.md#恢复与结果)中的说明，同时使用已保存回执的 `recovery_environment` 与 `recovery_argv` 恢复保留的候选。已经验证的候选通过[仅评测维护](#恢复运行与仅评测维护)评分，将原题目目录设为 `source_base_run_dir`，将新的独立目录设为 `base_run_dir`。

保存证据无法确认可信候选且 Solver 启动额度已耗尽时，将该次尝试保留为技术失败。实验协议允许再次生成后，使用新的 run ID 和输出目录。更换模型或实验设置时，也使用新的 run ID 和输出目录。恢复过程中保留原始报告与轨迹。

## 高级 worker 拓扑

操作人员在控制机上启动 OpenCollab-Eval。运行器通过 SSH 连接 Linux 工作节点，同步当前 OpenCollab 公开运行时与 OpenCollab-Eval 源代码树，验证两者的清单，随后启动一项或多项运行范围内任务。工作节点需要 Docker、Python、选定的 Solver 运行时、基准镜像和运行范围内可写存储。

可信 Pro-Lite 数据集必须已经位于 `<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl`。运行时同步不会上传这项由评测器持有的输入。`--start-index` 与 `--limit` 按照文件的稳定顺序选择数据行。

每次运行都应拥有唯一的运行 ID、输出目录、远程基准目录、会话前缀与容器所有权标签。凭据从同步源代码树之外的受保护文件挂载或读取。

## 准备工作节点

首次运行前请确认以下条件。

| 要求 | 验证方式 |
| --- | --- |
| SSH | 批处理模式 SSH 可以连接工作节点 |
| Python | `--remote-python` 能导入 OpenCollab-Eval 运行时依赖 |
| Docker | 评测账户执行 `docker info` 成功 |
| 存储 | 远程运行时目录与运行目录可写 |
| 数据集 | 可信 JSONL 存在于 `<remote-root>/datasets/swe-batch-pro-lite/instances.jsonl` |
| 镜像 | 数据集镜像名称能够解析为不可变本地镜像 |
| 凭据 | 所选传输方式能够读取受保护的环境文件 |

当工作节点的系统解释器缺少提供商依赖时，低层切片运行器和多 Solver 协调器都允许显式传入 `--remote-python`。选定的解释器会贯穿运行时同步、健康探测、候选生成和官方评测。

## 高级 Kimi 切片

直接 Kimi coding 配置是当前发行版支持的最小完整示例。远程环境文件包含 `KIMI_API_KEY` 或 `OPENAI_API_KEY`，权限模式为 `0600`，且不会从源码仓库同步。

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

低层运行器会显式接收 Kimi 身份值。多 Solver 协调器会把同样的 262144-token 上下文、温度 1、top-p 0.95、最大输出 32768 和保留的思考历史作为一套经过验证的配置，并在生成前拒绝冲突值。

使用相同参数并加上 `--dry-run` 可以验证配置与计划选择的任务。试运行只提供规划证据，不会产生语义任务判定。

## 通过 Solver 协调器运行

协调器为内置的 Solver 配置提供统一接口。

| Solver | 工作流或适配器 | 外部运行时 |
| --- | --- | --- |
| `g11` 和 `g1.1` | `validation-council-solve` | OpenCollab 工作流 |
| `baseTeam` | `base-team` | OpenCollab 工作流 |
| `TeamPro` | `team-pro` | OpenCollab 工作流 |
| `openhands` | `openhands-external` | OpenHands |
| `claude-code` | 外部打印模式适配器 | Claude Code 运行时 |

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

协调器示例使用经过验证的 K3 G11 配置。它会绑定精确的 `k3` 响应身份、1048576-token 上下文、温度 1、top-p 0.95、最大输出 32768、保留思考过程以及 `reasoning_effort=high`。协调器接受逗号分隔的索引列表与闭区间。也可以使用 `--start-index` 和 `--end-index`。Solver 默认值会先应用，剩余选项随后传递给并行运行器。

OpenHands 需要 Python 3.12 与打包的 `run_openhands_cli.sh` 资源。Claude Code 需要外部运行时镜像，以及适配器要求的精确模型身份。开始批次前，请运行每个外部运行时的聚焦冒烟测试。

## 提供商传输

直接 Kimi 模式读取工作节点上已经存在的凭据文件，并连接 `https://api.kimi.com/coding/v1`。它会绕过持久反向代理。

其他提供商配置使用经过身份验证的本地中继与 SSH 反向隧道。它们要求在协调器层提供 `--proxy-env-file`、`--local-proxy-base-url`、`--remote-proxy-base-url` 和 `--proxy-upstream-base-url`。远程 Solver 只会收到经过身份验证的中继端点，不会收到上游凭据。

提供商文件应是权限模式为 `0600`、大小受限的普通文件。请将这些文件置于源码检出、运行时同步根目录、任务工作区与输出目录之外。

## 尝试次数与并发

三个限制分别描述不同工作。

| 选项 | 含义 |
| --- | --- |
| `--max-task-starts` | 单项任务允许启动 Solver 的最大次数 |
| `--max-eval-attempts` | 单个候选允许接受官方评测的最大次数 |
| `--runner-attempts` | 结构化运行器失败后控制器允许尝试的最大次数 |
| `--eval-container-bind-timeout` | 等待 Docker 写出官方评测容器 ID 的秒数 |

确定性冒烟测试应使用值 1。只有实验协议允许相应重试时才能提高限制。提供商配额失败、生成失败与官方评测技术失败会分别记录，且不会转为 unresolved。

容器身份等待默认值为 30 秒，可配置范围为 1 至 300 秒。Docker 进程提前退出
时，运行器会立即失败。官方评测继续执行前仍需获得有效容器 ID，并精确匹配
所有权标签。

并行运行器能够在共享压力出现后降低并发，并在任务顺利完成后恢复并发。若固定并发属于实验协议的一部分，请使用 `--no-adaptive-concurrency`。单项任务或单个镜像失败不会暂停其他任务。只有直接探测表明共享 Docker、存储、队列或运行时基础设施失败时，才会暂停整个批次。

将 `OPENCOLLAB_EVAL_CAPACITY_CONTROL_FILE` 设置为控制器宿主机上的外部 JSON 文件，可以在任务运行期间调整生成并发。文件中的 `generation_workers` 字段表示并行运行的任务 worker 数，一个 worker 在解题期间可以发起多次提供商请求。有效并发值范围为 1 到 `--max-workers`，超过上限的正整数按上限执行。运行器在读到有效值前以 `--min-workers` 启动，每秒在普通队列和技术恢复队列中检查文件，读取失败或文件尚未写完整时沿用上次接受的值。调高数值会在已有任务仍运行时派发等待任务。调低数值后，已有任务继续完成，随后按较小的并发数量补派任务。容量变化会写入调度事件。

## 运行时同步

同步后的运行时包含 OpenCollab 公开软件包、OpenCollab-Eval 软件包、选定的 shell 资源和一份清单。生成开始前，本地与远程源代码树的 SHA-256 必须一致。

`--no-sync-runtime` 仅能与 `--expected-runtime-tree-sha256` 一同使用。此组合会固定一个已经安装的运行时，并拒绝任何不匹配。只有操作人员已经同步并验证过这棵精确代码树时，才能使用该组合。

镜像依赖按容器当前用户的权限选择暂存位置。实际工作区的父目录可写且位于同一文件系统时，大型忽略包目录使用工作区旁的暂存目录。父目录只读或工作区位于独立挂载点时，使用 `/tmp` 下的独立暂存目录。候选副本在容器的 `/tmp` 下准备。恢复到其他挂载点时，程序先在候选所在文件系统创建实际副本，再重命名到依赖路径。依赖准备传输沿用已配置的 `OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS` 时间预算，省略该设置时使用 `OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT`。

## 输出布局

本地输出目录包含并行摘要、健康与预检记录、每项任务的报告和日志。每个远程任务目录包含生成指标、候选证据、官方评测工作区、官方报告和清理证据。

最重要的记录如下。

| 记录 | 用途 |
| --- | --- |
| `parallel_summary.json` | 批次清单与终态计数 |
| `task_<index>_report.json` | 生成、候选、评测与失败详情 |
| `final_eval_layer_report.json` | 为所选任务集绑定的事实报告 |
| Generation metrics | 记录 ID、运行身份、模型身份与源补丁 SHA |
| Candidate projection | 基准树、候选树、路径、模式与补丁 SHA |
| Official report | 目标计划、命令、结构化证据、清理与判定 |

始终通过 `--json-output`、`--markdown-output` 和协调器的 `--output-dir` 传入明确的外部路径。历史默认值可能解析到当前工作树之下。完整的外部运行目录应作为一个证据单元保存，因为其中的记录通过身份与哈希互相引用。

## 恢复运行与仅评测维护

只有当前运行身份、记录 ID、运行时身份、补丁 SHA 与所需证据全部一致时，运行器才会复用结果。邻近的文件名或旧报告不足以支持复用。

`--eval-only` 是面向现有候选的低层单切片维护选项。统一 Solver 协调器会拒绝旧版仅评测选项，防止普通实验悄然跳过生成。每次获得授权的重新评测都应记录在实验协议中。

当多个已验证候选需要同一种维护操作时，使用 `oc-eval rejudge-queue`。队列只启动 `--eval-only` 子进程，把 `--max-task-starts` 和空补丁重试固定为零，接受终态报告前核对计划中的补丁 SHA-256，并自动刷新累计父报告。

### 准备仅评测队列

修复评分环境后，保留已经生成的候选，再执行此维护操作。计划存放在源码仓库外，将示例路径和身份占位值替换成原始任务报告中的实际值。`runner_args` 沿用原模型和传输设置，这些设置描述已有运行，队列会关闭每个子进程的模型生成。

计划中的 `index` 是原始的从 1 开始的题号。`parent_output_dir` 指向已有控制器报告。将 `source_base_run_dir` 设为原 worker 题目目录，将 `base_run_dir` 设为此次评测使用的全新独立目录，两者必须不同。运行器将绑定的候选记录从原目录复制到新的评测目录。`remote_runtime_repo` 指向所选源码运行目录。`task`、`record_id`、`source_patch_sha256` 与 `eval_patch_sha256` 绑定已保存候选。复评记录使用新的 `run_id` 与 `eval_dir_name`。

```json
{
  "schema": "opencollab.eval_only_queue.v1",
  "runner_args": [
    "--runner-transport", "local",
    "--remote-python", "/srv/oc-evaluation/.venv/bin/python",
    "--remote-root", "/srv/oc-evaluation/eval-data",
    "--image-repository", "jefzda/sweap-images",
    "--model-name", "original-model",
    "--llm-model", "original-model",
    "--llm-provider", "openai",
    "--session-prefix", "duo-rejudge-001",
    "--proxy-env-file", "/srv/oc-evaluation/eval-data/secrets/provider.env",
    "--local-proxy-base-url", "http://127.0.0.1:18080/v1",
    "--remote-proxy-base-url", "http://127.0.0.1:18080/v1",
    "--eval-timeout", "7200"
  ],
  "jobs": [
    {
      "index": 1,
      "parent_output_dir": "/srv/oc-evaluation/eval-data/results/duo-smoke-001",
      "source_base_run_dir": "/srv/oc-evaluation/eval-data/runs/duo-smoke-001/task_1",
      "base_run_dir": "/srv/oc-evaluation/eval-data/runs/duo-rejudge-001/task_1",
      "remote_runtime_repo": "/srv/oc-evaluation/eval-data/runs/duo-smoke-001/_runtime/repo",
      "run_id": "duo-rejudge-001",
      "eval_dir_name": "official_eval_rejudge_001",
      "task": "<task from the saved report>",
      "record_id": "<record ID from generation>",
      "source_patch_sha256": "<full source patch SHA-256 from generation>",
      "eval_patch_sha256": "<full evaluation patch SHA-256 from generation>"
    }
  ]
}
```

```bash
oc-eval rejudge-queue \
  --plan /srv/oc-evaluation/eval-data/rejudge-plan.json \
  --output-dir /srv/oc-evaluation/eval-data/rejudge-state \
  --workers 1
```

队列状态文件是输出目录下的 `rejudge_queue_<queue-id>.json`，记录每项任务的状态、子报告、日志与启动次数。沿用相同计划和输出目录再次执行时，已验证终态报告会被复用，从未启动的任务会继续调度。已启动后中断的 local 子进程会留下 worker 所有权记录。实验协议允许再次正式评测时，创建新计划并使用新的独立 `base_run_dir` 与 `run_id`，保留原候选来源和之前的队列状态。候选冲突与尝试额度耗尽需要依据记录处理对应原因。父级 `final_eval_layer_report.json` 会依据接受的结果刷新。

## 完成条件

成功的批次命令仍可能包含 unresolved 任务。请以 JSON 报告为准。一条可信的 resolved 记录应具有一个已绑定候选、一个新建的官方工作区、一份完整目标计划、精确执行证据、零项技术原因、静止的清理状态以及一份匹配的官方报告。

证据模型请继续阅读[评测完整性](evaluation-integrity.md)，失败诊断请参阅[故障排查](troubleshooting.md)。
