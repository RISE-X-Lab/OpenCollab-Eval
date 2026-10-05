# OpenCollab-Eval 文档

[English](../README.md) | **简体中文**

按要完成的任务选择指南。仓库 [README](../../README.md#simplified-chinese)介绍 0.9.1 和常用命令。[快速入门](getting-started.md)从安装开始，带你准备输入、运行任务并打开结果文件。

## 准备与运行任务

| 需要做的事 | 指南 |
| --- | --- |
| 安装并运行本地仓库任务 | [快速入门](getting-started.md) |
| 准备正确的 JSONL 输入 | [任务与数据集格式](task-formats.md) |
| 在一台 Linux 工作机上运行 Duo，再扩大到批量任务 | [SWE Pro-Lite 操作指南](swe-prolite-operations.md) |
| 运行服务器队列并管理 provider 请求容量 | [服务器评测指南](evaluation-suite.md) |
| 查找已安装的命令与参数 | [CLI 参考](cli-reference.md) |
| 为其他任务类型配置正式评分 | [评分适配](scoring-adapters.md) |

## 阅读结果与恢复运行

| 需要做的事 | 指南 |
| --- | --- |
| 理解候选提交资格与正式结果 | [评测完整性](evaluation-integrity.md) |
| 排查报错或停滞的运行 | [故障排查](troubleshooting.md) |
| 了解各运行阶段和证据文件 | [评测运行方式](evaluation-runtime.md) |
| 用 `oc-eval final-report` 生成对比报告 | [最终报告指南](final-report.md) |

## 理解与开发评测器

| 需要做的事 | 指南 |
| --- | --- |
| 了解软件包结构与 OC 依赖 | [架构](architecture.md) |
| 理解前台命令与容器候选采纳 | [Terminal 容器运行](terminal-container-runtime.md) |
| 准备任务前提并保留原始评分时限 | [Terminal 评分准备](terminal-verifier-preparation.md) |
| 阅读 Duo 候选证据与裁决工具 | [Duo 文件证据](duo-file-evidence.md) |
| 理解由控制器持有的候选提取 | [可信候选构造](design/trusted-candidate-construction.md) |
| 运行已安装 wheel 的 SSH 与 Docker 测试 | [确定性 SWE 端到端测试](testing/deterministic-swe-e2e.md) |

[MIGRATION.md](../../MIGRATION.zh-CN.md)介绍 OC 与 OCE 的职责。[CONTRIBUTING.md](../../CONTRIBUTING.zh-CN.md)介绍开发与评审方式，[SECURITY.md](../../SECURITY.zh-CN.md)介绍私下报告安全问题的方式。

[完整性覆盖台账](../integrity-coverage.json)将评分要求对应到实现与回归测试。研究历史来源保存在[实验索引](../../experiment/README.md)和 [ICLR 源码覆盖记录](../iclr-source-coverage.json)中。
