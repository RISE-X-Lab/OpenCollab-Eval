# eval_adapter

[English](README.md) | **简体中文**

`eval_adapter` 将基准数据行转换为评测框架使用的记录。它还把官方评测中的
基础设施故障归入稳定字段。

适配器使用以下模型。

| Model | 职责 |
| --- | --- |
| `TaskSpec` | 规范化的任务身份、仓库、问题描述、基准提交、镜像、测试与服务要求 |
| `WorkspaceSpec` | 启动工作区所需的镜像、仓库根目录候选、服务与环境 |
| `PatchCandidate` | Solver 补丁、补丁 SHA、日志路径、token 用量与成本 |
| `EvalResult` | 官方评测完成状态、resolved 状态、技术失败状态、原因与日志路径 |
| `RunRecord` | 汇集任务、候选和评测结果的逐任务记录 |

Pro-Lite 专用规则位于 `prolite.py`。这些规则处理 JSONL 数据集加载、Docker
镜像名、优先从 `/app` 查找仓库、NodeBB Redis 要求和空补丁记录。Redis、
SSH、Docker、超时、测试补丁应用失败与报告缺失也在这里归类为技术失败。

包边界参见[架构指南](../../../../docs/zh-CN/architecture.md)。候选、目标证据
和判定要求参见
[评测完整性指南](../../../../docs/zh-CN/evaluation-integrity.md)。
