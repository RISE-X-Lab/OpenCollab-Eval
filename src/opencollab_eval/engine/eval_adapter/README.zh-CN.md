# eval_adapter

[English](README.md) | **简体中文**

`eval_adapter` 是 evaluation harness 的 benchmark boundary。它将 dataset
row 转换为 evaluation record，并将 official-evaluation infrastructure
failure 分类到稳定字段中。

adapter 使用以下 model。

| Model | 职责 |
| --- | --- |
| `TaskSpec` | 规范化的任务身份、仓库、问题陈述、base commit、镜像、测试和服务需求 |
| `WorkspaceSpec` | 启动工作区所需的镜像、仓库根目录候选、服务和环境 |
| `PatchCandidate` | Solver patch、patch SHA、日志路径、token 用量和成本 |
| `EvalResult` | official-evaluation 完成状态、resolved 状态、technical-failure 状态、原因和日志路径 |
| `RunRecord` | 组合任务、候选和评测结果的最终逐任务记录 |

Pro-Lite 专用规则位于 `prolite.py`。这些规则覆盖 JSONL 数据集加载、Docker
镜像名、优先使用 `/app` 的仓库发现、NodeBB Redis 要求、empty-patch
记录，以及 Redis、SSH、Docker、timeout、test-patch 应用和缺少报告等情况
的 technical-failure 分类。

包边界参见[架构指南](../../../../docs/zh-CN/architecture.md)。候选、
target-proof 和 verdict 要求参见
[评测完整性指南](../../../../docs/zh-CN/evaluation-integrity.md)。
