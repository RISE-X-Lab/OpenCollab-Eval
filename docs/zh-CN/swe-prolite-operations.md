# SWE Pro-Lite 操作指南

[English](../swe-prolite-operations.md) | **简体中文**

本指南说明如何在远程工作节点上运行候选生成与官方评测。示例命令中的 `evaluator@example-worker` 以及 `/srv`、`/results` 路径都是占位符。

## 运行时拓扑

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

## 运行一个有界切片

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

确定性冒烟测试应使用值 1。只有实验协议允许相应重试时才能提高限制。提供商配额失败、生成失败与官方评测技术失败会分别记录，且不会转为 unresolved。

并行运行器能够在共享压力出现后降低并发，并在任务顺利完成后恢复并发。若固定并发属于实验协议的一部分，请使用 `--no-adaptive-concurrency`。单项任务或单个镜像失败不会暂停其他任务。只有直接探测表明共享 Docker、存储、队列或运行时基础设施失败时，才会暂停整个批次。

## 运行时同步

同步后的运行时包含 OpenCollab 公开软件包、OpenCollab-Eval 软件包、选定的 shell 资源和一份清单。生成开始前，本地与远程源代码树的 SHA-256 必须一致。

`--no-sync-runtime` 仅能与 `--expected-runtime-tree-sha256` 一同使用。此组合会固定一个已经安装的运行时，并拒绝任何不匹配。只有操作人员已经同步并验证过这棵精确代码树时，才能使用该组合。

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

## 完成条件

成功的批次命令仍可能包含 unresolved 任务。请以 JSON 报告为准。一条可信的 resolved 记录应具有一个已绑定候选、一个新建的官方工作区、一份完整目标计划、精确执行证据、零项技术原因、静止的清理状态以及一份匹配的官方报告。

证据模型请继续阅读[评测完整性](evaluation-integrity.md)，失败诊断请参阅[故障排查](troubleshooting.md)。
