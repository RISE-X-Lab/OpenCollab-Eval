# OpenCollab-Eval 文档

[English](../README.md) | **简体中文**

此目录将操作指南、契约、架构说明与设计记录分别组织，避免把实现计划误当作命令指南。

## 从这里开始

| 阅读目标 | 文档 |
| --- | --- |
| 安装软件包并运行第一条本地命令 | [快速入门](getting-started.md) |
| 准备数据集或通用任务 JSONL | [任务格式](task-formats.md) |
| 运行真实的远程 SWE Pro-Lite 任务 | [SWE Pro-Lite 操作指南](swe-prolite-operations.md) |
| 选择正确的命令 | [CLI 参考](cli-reference.md) |
| 了解组件与依赖关系 | [架构](architecture.md) |
| 了解可信结果与失败状态 | [评测完整性](evaluation-integrity.md) |
| 诊断失败的运行 | [故障排查](troubleshooting.md) |
| 发布经过验证的 100 任务对比结果 | [最终报告契约](final-report.md) |

仓库级的 [README](../../README.zh-CN.md) 给出了最精简的完整概览。[MIGRATION.md](../../MIGRATION.zh-CN.md) 界定 OpenCollab 与 OpenCollab-Eval 的职责归属。[CONTRIBUTING.md](../../CONTRIBUTING.zh-CN.md) 说明开发与评审要求。[SECURITY.md](../../SECURITY.zh-CN.md) 说明私下报告安全问题的流程。

## 操作与契约文档

[evaluation-runtime.md](evaluation-runtime.md) 说明已安装命令与运行时层级之间的对应关系，并解释哪些入口会生成候选结果，哪些入口会给出官方判定。[final-report.md](final-report.md) 是 `oc-eval final-report` 的完整输入与证据契约。

机器可读的[完整性覆盖台账](../integrity-coverage.json)将已知完整性要求映射到负责人、实现文件、测试和精确的测试节点 ID。测试套件会验证这份台账。更新实现与回归测试时，也应同步更新相应条目。

## 设计与验证记录

[可信候选构造](design/trusted-candidate-construction.md)介绍已经实现的、由控制器持有 Git 状态的投影机制。[确定性 SWE 端到端测试](testing/deterministic-swe-e2e.md)介绍基于已安装 wheel 的测试。该测试使用临时 SSH、伪模型服务、Docker、候选提取以及官方目标执行。

设计记录解释当前实现为何采用现有形态。测试记录描述可执行验证。需要执行命令的操作人员应使用快速入门、Pro-Lite、CLI 与故障排查指南。
