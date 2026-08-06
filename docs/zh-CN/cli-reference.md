# CLI 参考

[English](../cli-reference.md) | **简体中文**

常见操作使用已安装命令，仓库操作人员与测试还可以使用高级模块入口。对已安装的版本运行 `--help` 可以查看完整选项。

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

### `oc-eval run`

```text
oc-eval run TASKS_FILE --model MODEL --provider PROVIDER
            [--api-key KEY] [--base-url URL] [--output DIRECTORY]
            [--concurrency COUNT] [--max-tokens COUNT] [--timeout SECONDS]
            [--temperature VALUE] [--top-p VALUE]
```

此命令运行通用评测器并写入 `results.jsonl`。摘要包含任务数、具备资格的候选数和不具备资格的候选数。官方 SWE resolved 判定由 Pro-Lite 评测命令给出。

### `oc-eval swe-v1-prolite`

此命令让一个有界远程 Pro-Lite 切片依次完成生成与官方评测。主要选项组如下。

| 分组 | 选项 |
| --- | --- |
| 远程运行时 | `--host`、`--ssh-command`、`--remote-python`、`--remote-root`、`--remote-runtime-repo` |
| 任务选择 | `--start-index`、`--limit`、`--run-id`、`--base-run-dir` |
| Solver | `--workflow`、`--model-name`、`--llm-model`、`--llm-provider`、`--budget`、`--max-steps` |
| 模型身份 | `--context-window`、`--temperature`、`--top-p`、`--max-output-tokens` |
| 提供商传输 | `--remote-proxy-base-url`、`--local-proxy-base-url`、`--proxy-env-file`、`--remote-api-env-file` |
| 时间限制 | `--llm-timeout`、`--provider-error-time-budget`、`--swe-timeout`、`--task-wall-timeout`、`--eval-timeout`、`--total-timeout` |
| 证据限制 | `--max-task-starts`、`--max-eval-attempts`、`--checkpoint-interval` |
| 输出 | `--json-output`、`--markdown-output`、`--parent-output-dir` |
| 维护 | `--dry-run`、`--eval-only`、`--no-sync-runtime`、`--expected-runtime-tree-sha256` |

构建自动化前，请先运行已安装命令的帮助。

`--llm-timeout` 仍表示一次成功模型请求的最长时间。`--provider-error-time-budget`
为可重试的提供商错误和重试等待提供额外时间。任务生成、整题运行和控制器各增加一次同一份预留，
官方评测时限保持原值。

```bash
oc-eval swe-v1-prolite --help
```

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

## Solver 协调器

```bash
python -m opencollab_eval.commands.swe_eval_run --help
```

协调器会选择 `g11`、`g1.1`、`baseTeam`、`TeamPro`、`openhands` 或 `claude-code`，应用各自的固定默认值，并委托并行 Pro-Lite 运行器执行。协调器自身的选项用于选择数据集、索引、Solver、工作进程数、运行 ID、输出目录和分离进程模式。其他已识别的 Pro-Lite 选项会转发给并行运行器。

分离进程模式是通过 `launchd` 实现的 macOS 操作便利功能。直接提供商传输可以在受支持的平台上以前台方式运行。其他提供商传输默认使用持久化 `launchd` 中继。CI 与 Linux 自动化应传入 `--no-persistent-proxy`，并提供已经妥善管理的中继和隧道。

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

`resolved`、`unresolved` 与 `technical_failed` 是接受官方评测后相互排斥的终态分类。`oc-eval run` 给出的候选资格属于生成分类，不在这组终态分类中。
