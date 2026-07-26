# OpenCollab 兼容性与仓库归属

[English](MIGRATION.md) | **简体中文**

OpenCollab-Eval 负责基准契约、评测器编排、Solver 工作流、候选构建、SWE-bench 生成、进程隔离、执行证据、报告和远程评测。OpenCollab 负责智能体框架、领域与应用服务、适配器、组合机制、精简的公开 Python API 和框架测试。

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

OpenCollab 0.4.0 是首个兼容的公开 API 版本。软件包根目录提供 `OpenCollab`、`RunResult`、`RunError` 和 `workflow`。可选的公开契约与组合辅助工具位于 `opencollab.environments`、`opencollab.tools` 和 `opencollab.workflows`。

生产代码和测试禁止导入已弃用的 `opencollab.sdk` 命名空间，以及内部的 `opencollab.adapters`、`opencollab.application`、`opencollab.bootstrap`、`opencollab.domain` 和 `opencollab.harness` 命名空间。边界测试会对源码和已安装的 wheel 强制执行这项规则。

评测程序、基准数据、模型输出、预测、补丁、报告和集成测试归 OpenCollab-Eval 所有。框架行为和公开 API 测试归 OpenCollab 所有。

当前数据流见 [架构指南](docs/zh-CN/architecture.md)，兼容性验证见 [wheel 契约](CONTRIBUTING.zh-CN.md)。
