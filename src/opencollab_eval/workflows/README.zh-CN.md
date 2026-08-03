# 评测工作流

[English](README.md) | **简体中文**

该包包含用于评测的确定性 multi-agent workflow。普通 Python 代码控制
agent fan-out、修复轮次、验证 gate 和停止条件。模型在这些固定控制流边界内
完成仓库分析和修改。

该包只依赖 OpenCollab 0.4 的 workflow authoring surface。

```python
from opencollab.tools import Tool, builtin_tools
from opencollab.workflows import WorkflowContext, workflow
```

它不导入 OpenCollab adapter、application service、bootstrap internal、
domain module、harness code 和已退役的 `opencollab.sdk` submodule。

## 内置工作流

| 名称 | 结构 |
| --- | --- |
| `base-team` | Analyst brief，随后进入有界 coder 与 tester 循环 |
| `self-collab` | 顺序 phase、plan review 和逐 phase verification |
| `split-solve` | 独立 subtask，随后执行联合 verification |
| `scout-solve` | 并行只读侦察，随后执行一轮 repair loop |
| `analyst-solve` | Analyst 主导的侦察、分阶段修复和最终 verification |
| `team-pro` | `analyst-solve` 的稳定调优 alias |
| `validation-council-solve` | Blind evidence council，随后由一个 coder 生成候选并交给 official evaluation |
| `swe-committee-v2` | 带显式 evidence 和 test gate 的 committee workflow |

生产 Solver coordinator 将 `g11` 和 `g1.1` 映射到
`validation-council-solve`，将 `baseTeam` 映射到 `base-team`，将
`TeamPro` 映射到 `team-pro`。`openhands` 和 `claude-code` 是通过共享
generation 与 candidate 路径接入的外部 Solver 配置。其余 workflow
function 是 library-level building block，可以由 single-instance workflow
generator 选择。

Blind SWE workflow 接收 issue text、repository content、public test 和
public documentation。它们不会收到隐藏的 grader assertion。在
`validation-council-solve` 中，advisory role 负责准备 evidence package，
clean-source probe 用来隔离前置 role 与唯一 coder 的影响。首个能够归属于
coder 的非空源码修改会直接交给可信候选提取，再由外部 official evaluation
判定。

## 编写契约

workflow 是由 `@workflow` 装饰的 async function。role tool 来自
`builtin_tools`，它会返回全新的 headless-safe instance，并禁用模型提供的
test command override。

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

`WorkflowContext.agent` 运行一个有界 session。schema 用于请求通过验证的
structured output。`parallel` 在 runtime concurrency limit 下执行零参数
async thunk。`phase` 和 `log` 发布进度。当 runtime 提供 probe 时，
`source_changed` 和 `diff` 检查当前 work tree。`tokens_spent`、
`tokens_remaining`、`seconds_left` 和 `time_low` 暴露只读运行预算。

工具列表应与 role 匹配。只读 role 通常使用 `file_read` 和 `grep`，只有
需要 executable probe 时才使用 `bash`。Coder 可以加入 `file_write` 和
`apply_patch`。Test gate 需要 `run_tests`。Diff auditor 使用 `git_diff`。
向 `builtin_tools` 传入 `allow_file_creation=False` 可以阻止 `file_write`
创建新文件。

通过 public helper 创建的每个 `run_tests` instance 都会拒绝 runner
override 和模型提供的额外参数。当 benchmark 需要精确 target execution
evidence 时，workflow 可以在调用后检查该 instance 的 parser-backed
`verified_targets`。模型写入的 `tests_run` 字段无法替代 executable
evidence。

## 约定

每个 `ctx.agent` 结果都可能是 `None`，因此每个阶段都需要提供显式 fallback
或报告 incomplete result。Structured result 通过 `isinstance(result, dict)`
检查。Repair loop 包含上一轮 verifier finding，并使用固定轮次上限。Gate
role 在签发 `PASS` 前保留 executable probe。Workflow 返回值使用 `done`、
`incomplete` 或 `error`，并包含 `ctx.tokens_spent()`。

共享 helper 位于名称以下划线开头的 private module。Public workflow
function 从该包的 `__init__.py` 导出。

## 运行

```bash
python -m opencollab_eval.generation.gen_prediction_workflow \
  --instance-file /path/to/instance.json \
  --output /path/to/predictions.jsonl \
  --workflow validation-council-solve
```

generator 负责选择和执行内置 workflow。安装后的 consumer 从
`opencollab_eval.workflows` 导入 workflow function，不依赖 OpenCollab
内部的 workflow discovery 实现。
