# 评测工作流

[English](README.md) | **简体中文**

该包提供可重复的多智能体评测工作流。Python 代码定义控制流程，其中包括智能体
分支和修复轮次。验证门禁与停止条件也由 Python 代码管理，模型在这套控制流内
分析并修改仓库。

该包依赖 OpenCollab 0.7.0 或更高 0.7.x 版本中的工作流编写接口。

```python
from opencollab.tools import Tool, builtin_tools
from opencollab.workflows import WorkflowContext, workflow
```

依赖边界止于上述公开接口。OpenCollab 的适配器、应用服务、启动模块、领域
模块、评测框架代码和已退役的 `opencollab.sdk` 子模块均位于边界之外。

## 内置工作流

| 名称 | 结构 |
| --- | --- |
| `base-team` | 分析员先给出简报，随后进入有界的编码与测试循环 |
| `self-collab` | 按阶段执行，并审查计划和各阶段结果 |
| `split-solve` | 分别完成独立子任务，随后统一验证 |
| `scout-solve` | 并行只读勘察，随后进行一轮修复 |
| `analyst-solve` | 由分析员组织勘察、分阶段修复和最终验证 |
| `team-pro` | `analyst-solve` 的稳定调优别名 |
| `candidate-tournament-council-v1` | 两份独立补丁候选、一个集成角色和一个公共测试执行角色 |
| `validation-council-solve` | 面向 SWE 任务的盲审契约与验证委员会 |
| `swe-committee-v2` | 带有明确证据和测试门禁的委员会工作流 |

生产 Solver 协调器将 `g11` 和 `g1.1` 映射到
`validation-council-solve`，将 `g20-exp2` 映射到
`candidate-tournament-council-v1`，将 `baseTeam` 映射到 `base-team`，将
`TeamPro` 映射到 `team-pro`。`openhands` 和 `claude-code` 是通过共享
生成与候选路径接入的外部 Solver 配置。其余工作流函数是库级构件，可由单实例
工作流生成器选择。

盲审 SWE 工作流接收问题文本、仓库内容、公开测试和公开文档。隐藏的评分断言
留在评测器中，最终任务结果由外部官方评测决定。

## 编写契约

工作流是由 `@workflow` 装饰的异步函数。角色工具来自 `builtin_tools`。它会
返回适合无界面环境的全新实例，并禁用模型提供的测试命令覆盖值。

```python
from typing import Any

from opencollab.tools import builtin_tools
from opencollab.workflows import WorkflowContext, workflow


@workflow(
    name="my-flow",
    description="Inspect a task and report evidence.",
    phases=["inspect"],
)
async def my_flow(
    ctx: WorkflowContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal"'}

    await ctx.phase("inspect")
    report = await ctx.agent(
        f"Inspect the repository and report evidence.\n\nGoal:\n{goal}",
        label="analyst",
        tools=builtin_tools("bash", "file_read", "grep"),
    )
    return {
        "status": "done" if report else "incomplete",
        "report": report,
        "tokens_spent": ctx.tokens_spent(),
    }
```

`WorkflowContext.agent` 运行一个有界会话。schema 用于请求经过验证的结构化
输出。`parallel` 在运行时并发限制内执行无参数异步函数。`phase` 和 `log`
发布进度。运行时提供探针时，`source_changed` 和 `diff` 检查当前工作树。
`tokens_spent`、`tokens_remaining`、`seconds_left` 和 `time_low` 提供只读的
运行预算。

工具列表应与角色匹配。只读角色通常使用 `file_read` 和 `grep`，需要执行探针时
加入 `bash`。编码角色可以加入 `file_write` 和 `apply_patch`，测试通过项目原生
Shell 命令运行，差异审查使用 `git_diff`。向 `builtin_tools` 传入
`allow_file_creation=False` 可以阻止 `file_write` 创建新文件。

模型可见工具均采用当前 OC 原生定义。Eval 观察 Bash 的实际执行，保存命令、退出码
和对应测试输出，供工作流判断使用。命令选择、参数、等待时间、审批、执行及模型
看到的输出沿用原生 Bash 行为。专用测试工具、自动选择运行器、命令改写和工具
GREEN／RED 报告已经移除。

需要精确 pytest 目标证据时，命令可使用 `-rA` 输出实际执行的节点。Go 可使用
`-json`，Django 源码测试可使用原生详细输出。记录保留完整目标路径，空执行、
跳过、截断、错误目标或重放输出均无法作为通过证据。机械候选对照原样执行已经
记录的 Bash 命令。已有工作流证据检查继续要求真实执行，正式评分继续由独立
评测器使用原候选和指定测试完成。

## 约定

每个 `ctx.agent` 结果都可能是 `None`，因此各阶段需要提供明确的回退值或报告
不完整结果。结构化结果通过 `isinstance(result, dict)` 检查。修复循环会带上
上一轮验证意见，并使用固定轮次上限。门禁角色在签发 `PASS` 前保留可执行
探针。工作流返回值使用 `done`、`incomplete` 或 `error`，并包含
`ctx.tokens_spent()`。

共享辅助函数位于名称以下划线开头的私有模块。公开工作流函数从该包的
`__init__.py` 导出。

## 运行

```bash
python -m opencollab_eval.generation.gen_prediction_workflow \
  --instance-file /path/to/instance.json \
  --output /path/to/predictions.jsonl \
  --workflow validation-council-solve
```

生成器负责选择和执行内置工作流。安装后的使用方从
`opencollab_eval.workflows` 导入工作流函数，与 OpenCollab 内部的工作流
发现实现相互独立。
