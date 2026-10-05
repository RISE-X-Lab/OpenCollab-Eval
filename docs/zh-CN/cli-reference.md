# CLI 参考

[English](../cli-reference.md) | **简体中文**

常见操作使用已安装命令，仓库操作人员与测试还可以使用高级模块入口。对已安装的版本运行 `--help` 可以查看完整选项。

## 选择命令

| 目标 | 入口 | 结果位置 |
| --- | --- | --- |
| 运行推荐工作流并执行正式测试 | `oc-eval duo` | `parallel_summary.json` 与 `task_<index>_report.json` |
| 为已准备的本地任务生成补丁 | `oc-eval run` | `results.jsonl` |
| 检查可信基准题目行 | `oc-eval inspect` | 标准输出中的 JSON |
| 再次评分已保存候选 | `oc-eval rejudge-queue` | 队列状态与更新后的父任务报告 |
| 将已安装源码复制到 worker | `oc-eval package-runtime` | `runtime-manifest.json` |
| 发布完成的 100 题对比 | `oc-eval final-report` | 发布目录 |

新的正式评测从 [Duo 教程](swe-prolite-operations.md#在一台-linux-工作机上运行-duo)开始。已有实验仍可使用底层切片 runner 与 Solver 协调器。

## 已安装命令

以下两种形式会调用同一软件包。

```bash
oc-eval --help
python -m opencollab_eval --help
```

### `oc-eval inspect`

```text
oc-eval inspect DATASET --identity-key-file KEY
                       [--image-repository REPOSITORY]
```

此命令验证大小受限的 SWE-Batch Pro JSONL 文件，分离公开字段与密封字段，并输出匿名公开任务 ID。处理会在检查完成后结束，生成和官方评测尚未开始。

### `oc-eval duo`

```text
oc-eval duo --config CONFIG [--indices INDICES] [--workers COUNT]
            [--run-id ID] [--output-dir DIRECTORY] [--dry-run]
```

该命令通过正式评测器运行统一的 OpenCollab Duo。`workflow` 为 `duo`，
`agent_profile` 默认为 `base`，解析后为 `single2`。裁决者通过只读工具读取完整证据文件。
`--dry-run` 输出有效配置，`oc-eval g22` 保留为命令别名。
模型参数、预算、超时与评测选项沿用已有并行 runner。

入口默认使用本机传输、第 1 行、一个 worker 和解析为 `single2` 的 `base` 角色。报告写入 `<config-directory>/results/<run-id>`，`--output-dir` 可以更换目录。保留同一 `--run-id` 可以复用已完成报告，并继续派发尚未启动的任务。已中断的任务按[恢复指南](swe-prolite-operations.md#恢复运行与仅评测维护)处理。JSON 键名使用并行 parser 参数名并以英文下划线连接。`workflow_env` 接受对象或 `KEY=VALUE` 字符串列表。CLI 覆盖值优先于同名 JSON 设置。

Duo 最终状态为 `done` 或 `--dry-run` 成功时，退出码为 0。其他最终状态返回 1，包括技术失败与设施停止。参数、配置错误和已报告的运行异常返回 2。KeyboardInterrupt 返回 130。

### `oc-eval run`

```text
oc-eval run TASKS_FILE --model MODEL --provider PROVIDER
            [--api-key KEY] [--base-url URL] [--output DIRECTORY]
            [--concurrency COUNT] [--max-tokens COUNT] [--timeout SECONDS]
            [--temperature VALUE] [--top-p VALUE] [--agent-profile PROFILE]
            [--no-progress-timeout SECONDS] [--generation-wall-timeout SECONDS]
```

此命令运行通用评测器并写入 `results.jsonl`。摘要包含任务数、具备资格的候选数和不具备资格的候选数。官方 SWE resolved 判定由 Pro-Lite 评测命令给出。

默认值为 `--output eval_results`、`--concurrency 4`、`--max-tokens 1000000`、`--timeout 600` 与 `--temperature 0.2`。通过 `--model` 与 `--provider` 指定模型和适配器，或设置 `OPENCOLLAB_MODEL` 与 `OPENCOLLAB_PROVIDER`。`OPENCOLLAB_API_KEY` 与 `OPENCOLLAB_BASE_URL` 提供凭据和入口。每道题可以覆盖 token 预算与超时。真实运行选择仓库外的绝对输出目录。

通过 `repo_path` 选择本地 Git 仓库时，仓库应已有提交，工作区干净且没有未跟踪文件。[快速入门](getting-started.md#运行本地仓库任务)给出只读 Git 检查。将自己的改动提交，或使用另一份干净的 checkout。

使用 `openai` provider 时，此命令采用 Chat Completions，base URL 需要支持这一 API。`OPENCOLLAB_WIRE_PROTOCOL` 无法将此命令切换为 Responses。[Duo 教程](swe-prolite-operations.md#在一台-linux-工作机上运行-duo)提供另一套 Responses 配置。

摘要字段为 `tasks`、`eligible_patches` 与 `ineligible`。任务失败后，命令也可能以退出码 0 结束。向评测器提交补丁前，逐行读取 `patch_produced`、`submission_eligible`、`error` 与 `execution_quiesced`。[任务格式](task-formats.md#通用评测器任务-jsonl)介绍输入文件的创建方法。

`--no-progress-timeout` 启用真实进展监督。完整的原生模型回合、模型内容或工具参数的实际增量，以及工具完成事件都会更新闲置计时。HTTP 成功、响应创建、保活、日志增长和快照修改时间维持原有计时。独立单 Agent 生成器与工作流生成器也接受这两个选项。

Pro-Lite 与并行运行器通过 `--workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200` 使用同一策略。启用后，默认的 Solver、任务和 controller 累计时限改为闲置计时。显式传入 `--workflow-env OPENCOLLAB_EVAL_GENERATION_WALL_TIMEOUT=SECONDS` 可保留生成累计时限。`OPENCOLLAB_EVAL_CONTROLLER_WALL_TIMEOUT` 可指定 controller 累计时限。官方评测继续使用自己的测试超时。已有进程保持启动时加载的设置。

网关流式观测通过 `--workflow-env OPENCOLLAB_EVAL_MODEL_PROGRESS_PATH=/absolute/path/model-progress.jsonl` 配置。每个生成器子进程会在原有 User-Agent 后追加既有的 `oce-progress/INVOCATION_ID` 标记。网关在紧凑事件的 `progress_id` 中记录该 ID，观察者按生成器 ID 精确匹配共享文件的活动。每个事件记录 `event`、`epoch`、`call` 和 `content_chars`。实际内容使用 `model_content` 或 `tool_arguments`，正常完成的模型回合使用 `model_completed`。通用并发评测器使用每任务的独立进展源，或具有显式归属的网关事件。一个网关进展归属标识对应一个活跃任务。

进展状态分别记录生成、工具活动、候选提取和评分阶段。生成时长在原生运行返回时结束，候选提取和报告写入在此后执行。协作式闲置停止会保留原生快照与 journal，然后通过已有的失败候选恢复流程选取经过验证的候选，进入官方评测。

### `oc-eval swe-v1-prolite`

此命令让一个有界远程 Pro-Lite 切片依次完成生成与官方评测。主要选项组如下。

| 分组 | 选项 |
| --- | --- |
| 远程运行时 | `--host`、`--ssh-command`、`--remote-python`、`--remote-root`、`--remote-runtime-repo` |
| 任务选择 | `--start-index`、`--limit`、`--run-id`、`--base-run-dir` |
| Solver | `--workflow`、`--agent-profile`、`--model-name`、`--llm-model`、`--llm-provider`、`--budget`、`--max-steps` |
| 模型身份 | `--context-window`、`--temperature`、`--top-p`、`--max-output-tokens` |
| 提供商传输 | `--remote-proxy-base-url`、`--local-proxy-base-url`、`--proxy-env-file`、`--remote-api-env-file` |
| 证据限制 | `--max-task-starts`、`--max-eval-attempts`、`--checkpoint-interval`、`--eval-container-bind-timeout` |
| 输出 | `--json-output`、`--markdown-output`、`--parent-output-dir` |
| 维护 | `--dry-run`、`--eval-only`、`--no-sync-runtime`、`--expected-runtime-tree-sha256` |
| 评分适配 | `--scoring-adapter-registry` |

`--scoring-adapter-registry` 指定已安装在 worker 绝对路径下的外部登记文件。
host 配置默认值为 `OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY`。
[外部正式评分适配说明](scoring-adapters.md) 给出了登记格式、实际入口、
runtime 打包方式与回执字段。

`--eval-container-bind-timeout` 控制运行器等待 Docker 写出官方评测容器 ID
的时长。默认值为 30 秒，可配置范围为 1 至 300 秒。该设置属于官方评测
运行身份。使用不同数值生成的报告不会被复用。

底层默认选择 `--start-index 26` 与 `--limit 10`，工作流为 `validation-council-solve`。显式填写实际选题范围。数值默认值为每题 16000000 token、60 步、生成 14400 秒、整题 15300 秒、正式评测 7200 秒、单次模型请求 900 秒。`--max-task-starts` 默认 3，`--max-eval-attempts` 默认 2。可信宿主机提取当前要求 `--checkpoint-interval 0`。报告路径显式设置到仓库外。

构建自动化前，请先运行已安装命令的帮助。

`--agent-profile single2` 为所选 OpenCollab 工作流的每个 Agent 角色启用 Single2 运行时。工作流分别选择，G21 使用 `--workflow validation-council-dual-coder-contract-v1`。工作流生成器与并行运行器都接受这两个参数。工作流省略 profile 时沿用原角色配置。显式的 `base`、`default` 和 `single` 都选择 Base，当前解析为 `single2`。单 Agent 生成器默认使用 Base，调用 `OpenCollab.agent(profile="single2")`。配置、metrics 与候选复用身份记录解析后的名称。历史记录中缺失的 profile 与新 Base 运行分别识别。

```bash
oc-eval swe-v1-prolite --help
```

### `oc-eval package-runtime`

```bash
oc-eval package-runtime --output /results/runtime-001
```

目标目录需要尚未存在。命令复制已安装的 OpenCollab 与 Eval 源码模块和配套资源，写入既有运行包清单，并输出目录和文件数。运行依赖、Docker 镜像、数据集和 provider 文件需要在 worker 上分别准备。具体见[评测套件](evaluation-suite.md#安装与打包)。

### `oc-eval final-report`

此命令会验证两份完整事实报告、对应的干净运行审计清单、规范数据集及所有引用证据，随后发布 JSON、Markdown、TeX、PDF 和最终清单。

```bash
oc-eval final-report \
  --method-a-report METHOD_A.json \
  --method-a-audit-manifest METHOD_A_AUDIT.json \
  --method-b-report METHOD_B.json \
  --method-b-audit-manifest METHOD_B_AUDIT.json \
  --dataset-file DATASET.jsonl \
  --meeting-date YYYY-MM-DD \
  --author AUTHOR \
  --output-dir DIRECTORY
```

完整证据契约请参阅 [final-report.md](final-report.md)。

### `oc-eval rejudge-queue`

此命令继续评测一组已经绑定证据的候选。队列计划把每个任务绑定到父运行、题号、运行 ID、评测目录与补丁 SHA-256。所有子进程均关闭模型生成。

```bash
oc-eval rejudge-queue \
  --plan /absolute/path/rejudge-plan.json \
  --output-dir /absolute/path/rejudge-state \
  --workers 2
```

只有任务、记录 ID、源补丁 SHA-256、评测补丁 SHA-256、候选投影和直接测试执行证据都匹配时，队列才会跳过已有终态报告。互相冲突的结论会直接失败。其余任务在配置的并发限制内运行，继续遵守父运行的评测次数预算，并自动刷新父事实报告。状态文件会在每次状态变化后更新，因此中断后可以使用同一计划再次启动。

队列默认使用两个 worker。[仅评测维护](swe-prolite-operations.md#准备仅评测队列)给出计划结构与字段来源。

## Solver 协调器

```bash
python -m opencollab_eval.commands.swe_eval_run --help
```

协调器会选择 `g11`、`g1.1`、`baseTeam`、`TeamPro`、`openhands` 或 `claude-code`，应用各自的固定默认值，并委托并行 Pro-Lite 运行器执行。协调器自身的选项用于选择数据集、索引、Solver、工作进程数、运行 ID、输出目录和分离进程模式。其他已识别的 Pro-Lite 选项会转发给并行运行器。

分离进程模式是通过 `launchd` 实现的 macOS 操作便利功能。直接提供商传输可以在受支持的平台上以前台方式运行。其他提供商传输默认使用持久化 `launchd` 中继。CI 与 Linux 自动化应传入 `--no-persistent-proxy`，并提供已经妥善管理的中继和隧道。

并行运行器还会读取控制器宿主机上的 `OPENCOLLAB_EVAL_CAPACITY_CONTROL_FILE`。外部 JSON 文件用 `generation_workers` 指定并行运行的任务 worker 数，每个 worker 在解题期间可以发起多次提供商请求。有效范围为 1 到 `--max-workers`，超过上限的正整数按上限执行。指定控制文件后，初始并发数为 `--min-workers`，普通任务和技术恢复任务运行期间每秒刷新一次。无效读取继续使用上次有效值，降低并发后在途任务继续完成。

## 高级 Solver 名称

Duo 使用独立的顶层配置命令。协调器的历史 Solver 目录包含以下名称。已注册工作流与配置好的外部适配仍可使用。历史映射缺少当前生成器目标时，需要原实验运行环境。新的运行选择 Duo 或已注册工作流。

| Solver 名称 | 工作流或适配 | 当前状态 |
| --- | --- | --- |
| `g11` | `validation-council-solve` | 已注册工作流 |
| `g1.1` | `validation-council-solve` | G11 别名 |
| `g20-exp1` | `evidence-action-council-v1` | 历史映射，当前生成器登记表缺少目标 |
| `g20-exp2` | `candidate-tournament-council-v1` | 历史映射，当前生成器登记表缺少目标 |
| `g11-wired` | `validation-council-wired-v1` | 历史映射，当前生成器登记表缺少目标 |
| `baseTeam` | `base-team` | 已注册工作流 |
| `TeamPro` | `team-pro` | 已注册工作流 |
| `openhands` | `openhands-external` | 外部适配，需要 OpenHands 运行环境 |
| `claude-code` | `openhands-external` | 外部适配，使用 Claude Code 命令模板 |

[工作流参考](../../src/opencollab_eval/workflows/README.zh-CN.md)列出当前已注册工作流。工作流与 Agent profile 分别选择，复现实验时保留原设置。

## 高级模块入口

高级命令是已安装软件包中的模块。它们面向仓库操作人员与测试，其接口的演进速度可能快于顶层 CLI。

| 模块 | 用途 |
| --- | --- |
| `opencollab_eval.generation.gen_prediction` | 生成一份单智能体预测 |
| `opencollab_eval.generation.gen_prediction_workflow` | 生成一份工作流预测 |
| `opencollab_eval.generation.gen_prediction_openhands` | 生成一份 OpenHands 预测 |
| `opencollab_eval.commands.swe_g11_parallel_runner` | 协调一个兼容 G1.1 的并行批次 |
| `opencollab_eval.commands.swe_eval_layer_report` | 将有界评测轮次合并成一份事实报告 |
| `opencollab_eval.commands.swe_rejudge_direct_eval` | 重新评测一个已经显式绑定的现有候选 |
| `opencollab_eval.commands.swe_rejudge_queue` | 继续评测一个有界候选队列 |
| `opencollab_eval.commands.swe_token_cost_summary` | 汇总已记录的模型用量与配置价格 |
| `opencollab_eval.commands.swe_frozen_manifest` | 在 Solver 启动前验证冻结任务清单 |

通过已安装的解释器调用模块。

```bash
python -m opencollab_eval.generation.gen_prediction_workflow --help
```

候选投影辅助工具、进程守卫、中继辅助工具、报告渲染器和 sidecar 构建器属于实现接口。生产自动化应调用顶层命令或已经文档化的高级模块，避免自行组合私有辅助工具。

## 退出状态与结果语义

参数错误或验证错误使用非零退出状态。已完成的命令也可能写入任务级技术失败。生成的 JSON 记录每项任务的结果，进程退出码表示整条命令的状态。

题目行使用 `task_result.status`，终态为 `resolved`、`unresolved` 与 `technical_failure`。汇总计数使用 `technical_failed` 记录技术失败总数。结合该状态阅读生成与正式评测字段。`oc-eval run` 的候选资格描述生成结果是否具备提交条件。
