# Terminal 容器命令生命周期

[English](../terminal-container-runtime.md) | **简体中文**

Terminal 运行器可以使用 `opencollab_eval.generation.terminal_container_backend` 执行命令并保留完整候选容器。调用方提供容器、工作目录、产物目录和候选工厂。评测运行器继续负责题目镜像、资源限制、网络策略、来源保护和原始评分器执行。

后端同时持有每个前台 Docker 执行与输出收集任务。进程退出且输出成功收集后，命令才会结束登记。即使外层工具调用已因取消失败而返回，后续完成也会更新记录。候选清理与采纳在检查剩余命令前会先核实这些状态。

仍在运行或收集输出的命令会阻止采纳。输出收集失败或被取消时，执行状态继续保留为未解决。候选身份仍需与已捕获差异以及实际运行的容器一致。任务需要的后台服务会在选中容器中保留。

## 接入已有 Terminal 运行器

用下列导入替换运行器中复制的 `container_backend.py`。

```python
from opencollab_eval.generation.terminal_container_backend import (
    CommandResult,
    ContainerCandidates,
    ContainerEnvironment,
    docker,
    inspect,
    state_diff,
)
```

调用方可以继续使用原有方式创建执行环境和候选接口。

```python
environment = ContainerEnvironment(container_id, workspace, command_artifacts)

async def create_candidate(label):
    container_id = await create_task_container(label)
    return ContainerEnvironment(container_id, workspace, candidate_artifacts / label)

candidates = ContainerCandidates(source_container_id, create_candidate, candidate_artifacts)
```

将 `environment` 传入公开 OpenCollab 构造器的执行环境参数，将 `candidates`
传入工作流调用的候选工作区参数。

```python
from opencollab import OpenCollab
from opencollab.builtin_workflows import duo

client = OpenCollab(workspace=workspace, environment=environment)
result = await client.workflow(duo, task_inputs, candidate_workspace=candidates)
selected = candidates.selected
```

采用候选后，`selected.environment.container` 标识保留的候选容器。调用方记录
这个身份，创建独立副本评分，并在保存结果后删除自己持有的容器。原有工具包装、评测来源保护、题目配置和评分入口继续由调用方持有。升级已有运行器时，让新 worker 使用包内实现。

宿主机需要 Docker CLI。任务容器需要 Linux、Bash、`setsid` 和支持
`--default-signal` 的 GNU `env`。重置 INT 与 QUIT 可以在包装层使用后台进程时保留前台信号行为。终止操作继续针对自身持有的进程组。

## 回归覆盖

命令生命周期测试使用真实本地子进程，并替换 Docker 传输。测试复现取消传输失败后进程延迟退出的情况，随后采纳同一个选中候选。测试还覆盖成功与非零退出、未解决输出、活动命令拒绝、候选身份和容器状态。

```bash
pytest -q tests/generation/test_terminal_command_lifecycle.py \
  tests/generation/test_terminal_candidate_delivery.py
```

[Terminal评分准备](terminal-verifier-preparation.md)提供题面登录检查与新输出隔离的评分入口。
