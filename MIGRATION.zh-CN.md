# OpenCollab 兼容性与仓库归属

[English](MIGRATION.md) | **简体中文**

OpenCollab-Eval 负责基准与评测代码，其中包括 Solver 工作流和远程评测。候选构造在隔离的进程环境中运行，其输出保留执行证据。OpenCollab 负责智能体框架、公开 Python API 和框架测试。

## 软件包归属

| 归属方 | 软件包 |
| --- | --- |
| 公开与密封任务契约 | `opencollab_eval.contracts` |
| 基准规范化 | `opencollab_eval.benchmarks` |
| 评测器与证据引擎 | `opencollab_eval.engine` |
| 生成与进程隔离 | `opencollab_eval.generation` |
| 批次、报告与远程命令 | `opencollab_eval.commands` |
| Solver 工作流 | `opencollab_eval.workflows` |
| Shell 与配置资源 | `opencollab_eval.resources`, `opencollab_eval.configs` |

评测器采用 `src` 软件包布局。安装后的命令通过 `python -m` 或 `oc-eval` 控制台脚本启动模块。远程执行会同步声明的 OpenCollab 公开软件包和 OpenCollab-Eval 运行时，验证其源码树身份，再从同步后的软件包根目录执行导入。

## OpenCollab 版本边界

OpenCollab-Eval 0.5.1 要求使用 OpenCollab 0.5.0。本补丁版本保持 0.5.0 配套关系，同时修正旧版结果与受控停止处理。这组配套版本提供当前评测器使用的 Responses 传输、运行时身份检查与公开测试契约。软件包根目录提供 `OpenCollab`、`RunResult`、`RunError` 和 `workflow`。可选的公开契约与组合辅助工具位于 `opencollab.environments`、`opencollab.tools` 和 `opencollab.workflows`。

生产代码和测试禁止导入已弃用的 `opencollab.sdk` 命名空间，以及内部的 `opencollab.adapters`、`opencollab.application`、`opencollab.bootstrap`、`opencollab.domain` 和 `opencollab.harness` 命名空间。边界测试会对源码和已安装的 wheel 强制执行这项规则。

评测程序、基准数据、模型输出、预测、补丁、报告和集成测试归 OpenCollab-Eval 所有。框架行为和公开 API 测试归 OpenCollab 所有。

当前数据流见 [架构指南](docs/zh-CN/architecture.md)，兼容性验证见 [wheel 契约](CONTRIBUTING.zh-CN.md)。
