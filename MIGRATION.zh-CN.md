# OpenCollab 兼容性与实验归属

[English](MIGRATION.md) | **简体中文**

OpenCollab-Eval 负责基准规范化、评测执行、候选构造与评分证据。候选构造在隔离的进程环境中运行，其输出保留执行证据。OpenCollab 负责智能体框架、公开 Python API 和框架测试。

实验命令与工作流由 [OpenCollabExp](https://github.com/KaiEureka/OpenCollabExp) 维护。
该层使用 `oc-exp g22`、`oc-exp swe-run`、`oc-exp rejudge-queue`、`oc-exp package-runtime`
和 `oc-exp final-report`。OCE 保留 `inspect`、`run` 和 `score`，方法通过 `module:function` 显式传入。
配套接口位于 `codex/extract-experiment-layer` 分支，主分支保持原状。

## 软件包归属

| 归属方 | 软件包 |
| --- | --- |
| 公开与密封任务契约 | `opencollab_eval.contracts` |
| 基准规范化 | `opencollab_eval.benchmarks` |
| 评测器与证据引擎 | `opencollab_eval.engine` |
| 生成与进程隔离 | `opencollab_eval.generation` |
| 核心 CLI 与评分入口 | `opencollab_eval.commands` |
| 评测进程资源 | `opencollab_eval.resources` |
| 实验工作流方法 | `opencollab_exp.workflows` |
| 批量实验、部署与结果展示 | `opencollab_exp.commands` |

评测器采用 `src` 软件包布局。安装后的命令通过 `python -m` 或 `oc-eval` 控制台脚本启动模块。远程执行会同步声明的 OpenCollab 公开软件包和 OpenCollab-Eval 运行时，验证其源码树身份，再从同步后的软件包根目录执行导入。

## OpenCollab 版本边界

OpenCollab-Eval 0.7.0 要求使用 OpenCollab 0.7.x。历史 0.5.1 版本配套 OpenCollab 0.5.0，记录保留在更新日志中。当前公开 API 提供 Responses 传输、运行时身份检查与公开测试契约。软件包根目录提供 `OpenCollab`、`RunResult`、`RunError` 和 `workflow`。可选的公开契约与组合辅助工具位于 `opencollab.environments`、`opencollab.tools` 和 `opencollab.workflows`。

生产代码和测试禁止导入已弃用的 `opencollab.sdk` 命名空间，以及内部的 `opencollab.adapters`、`opencollab.application`、`opencollab.bootstrap`、`opencollab.domain` 和 `opencollab.harness` 命名空间。边界测试会对源码和已安装的 wheel 强制执行这项规则。

通用评测机制与对应测试归 OpenCollab-Eval 所有，框架行为和公开 API 测试归 OpenCollab 所有。
基准数据、模型输出、预测、补丁和报告保存在调用方的实验 workspace。
部署操作见 [OpenCollabExp 的 SWE Pro-Lite 指南](https://github.com/KaiEureka/OpenCollabExp/blob/main/docs/zh-CN/swe-prolite-operations.md)。

当前数据流见 [架构指南](docs/zh-CN/architecture.md)，兼容性验证见 [wheel 契约](CONTRIBUTING.zh-CN.md)。
