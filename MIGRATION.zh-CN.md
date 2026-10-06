# 版本配套与升级

[English](MIGRATION.md) | **简体中文**

OpenCollab 负责让智能体做题。OpenCollab-Eval 负责准备题目、保存生成的补丁、运行官方测试和整理结果。选择配套版本或升级现有安装时，可以从这页开始。

## 各部分由谁负责

| 工作 | 负责模块 |
| --- | --- |
| 智能体会话和内置 Duo 工作流 | OpenCollab 公开 API |
| 公开题面与私有评分输入 | `opencollab_eval.contracts` |
| 基准数据转换 | `opencollab_eval.benchmarks` |
| 候选生成与进程隔离 | `opencollab_eval.generation` |
| 官方评分与结果依据 | `opencollab_eval.engine` |
| 批量命令与报告 | `opencollab_eval.commands` |
| 评测器自带的研究工作流 | `opencollab_eval.workflows` |
| 随包提供的 Shell 脚本与配置 | `opencollab_eval.resources`, `opencollab_eval.configs` |

评测器使用 `src` 软件包布局。安装后的入口是 `oc-eval` 和 `python -m opencollab_eval`。准备运行环境时，会先打包或同步指定的框架与评测器源码，再让执行机器导入它们。

## 选择配套版本

当前评测器版本为 0.9.2，依赖范围为 `opencollab>=0.9,<0.10`，CI 使用 OpenCollab 0.9.2 验证。实际验证的框架提交写在 `.github/workflows/ci.yml`。

按照[入门指南](docs/zh-CN/getting-started.md)安装发布的 wheel 或源码。开始运行前，先确认当前环境实际加载的两个包版本。

```bash
oc-eval --version
python -c 'from importlib.metadata import version; print("OC", version("opencollab")); print("OCE", version("opencollab-eval"))'
```

框架的公开入口包括 `OpenCollab`、`RunResult`、`RunError` 和 `workflow`。评测集成还会使用已有文档说明的 `opencollab.builtin_workflows`、`opencollab.environments`、`opencollab.patches`、`opencollab.profiles`、`opencollab.models`、`opencollab.teams`、`opencollab.tools` 和 `opencollab.workflows`。已有源码和安装包检查会拒绝导入框架内部实现，以及已退休的 `opencollab.sdk` 命名空间。

## 让已有结果仍然能够解释清楚

升级时另建环境，保留已完成运行的原配置和输出。升级软件包会改变之后生成候选所用的运行环境。已有候选重新评分时，继续按[评测完整性](docs/zh-CN/evaluation-integrity.md)中的规则核对候选与测试。

历史版本变化保留在 [CHANGELOG.md](CHANGELOG.md)。已完成的研究代码整合记录在[源码覆盖表](docs/iclr-source-coverage.json)和[历史实验材料](experiment/README.md)中。当前开发与发布要求见 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)和 [RELEASING.md](RELEASING.md)。
