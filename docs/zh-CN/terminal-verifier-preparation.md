# Terminal 评分准备

[English](../terminal-verifier-preparation.md) | **简体中文**

`opencollab_eval.generation.terminal_verifier.run_terminal_verifier` 先准备保留候选的评分副本，再调用一次原始评分器。准备与评分共享题目原有时限。CPU、内存、题面、候选选择和测试内容继续由调用方管理。

使用保存候选创建独立评分容器，准备前保留原候选快照。评分使用题目原资源限制，每次尝试使用新的产物目录。

## 按任务准备

对于 `make-doom-for-mips` 和 `make-mips-interpreter`，准备过程先归档已有 `/tmp/frame.bmp`，再隔离这一输出。原测试在启动 VM 后等待该路径出现。此前自测留下的图片会让等待立即结束，导致新进程过早停止，或者把旧图片用于验证。归档保留原字节和元数据，候选其他文件与服务继续保留。

对于 `configure-git-webserver`，调用方提供可信的 `login_probe` Shell 命令，实际完成题面承诺的登录。探针缺失或登录失败会在正式评分前返回准备错误。任务启动设置应在生成前提供普通账户与认证方式。辅助函数检查该设置，账户安装、组权限、仓库权限与候选内容部署继续由各自负责方管理。

登录探针来自可信任务运行器配置。使用任务配置的 SSH 身份文件或 agent，避免把口令或密钥嵌入命令文本。辅助函数通过标准输入传入探针并抑制其输出，回执记录结果，省略命令和认证诊断。探针应完成实际登录并检查身份，单独检查端口不足以验证登录。

其他任务的评分入口检查候选前台命令已经结束，文件系统保持原状态。

## 调用评分入口

已有的官方评分适配器负责执行原测试命令、收集原 reward 与报告，并执行传入的剩余时限。

```python
from pathlib import Path

from opencollab_eval.generation.terminal_container_backend import ContainerEnvironment
from opencollab_eval.generation.terminal_verifier import run_terminal_verifier

async def score_candidate(
    task, candidate_copy, task_settings, original_verify, output, preparation_receipt
):
    environment = ContainerEnvironment(
        candidate_copy, task_settings["workspace"], Path(output) / "commands"
    )

    async def verify(remaining_seconds):
        # Run the unchanged official verifier in candidate_copy. Return its raw
        # reward, exit_code, timed_out, and error, together with report paths.
        return await original_verify(
            candidate_copy, timeout_seconds=remaining_seconds
        )

    return await run_terminal_verifier(
        task.name,
        environment,
        Path(output) / "preparation",
        verify,
        timeout_seconds=task_settings["verifier_timeout_seconds"],
        login_probe=task_settings.get("login_probe"),
        preparation_receipt=preparation_receipt,
    )
```

登录题还应在模型生成前，对已经准备好的 source 环境调用 `prepare_terminal_verifier`，使用独立产物目录。这样能在消耗模型 token 前发现缺失登录。评分入口会在评分副本再次检查。

```python
from opencollab_eval.generation.terminal_verifier_preparation import (
    prepare_terminal_verifier,
)

if task.name == "configure-git-webserver":
    await prepare_terminal_verifier(
        task.name,
        source_environment,
        Path(output) / "provided-login",
        login_probe=task_settings["login_probe"],
        timeout=30,
    )
```

## 结果与恢复

每次调用由调用方提供一个新的 `preparation_receipt` 字典，保存过程中产生的备份与隔离位置。外部取消继续传播标准的 `asyncio.CancelledError`，调用方仍可从这个字典读取并持久保存恢复证据。回执独立于取消异常对象，因此适用于 Python 3.10、3.11 和 3.12 的任务取消行为。

原评分器返回数值 0 或 1 的奖励、整数退出码、false 超时标记且错误为空时，
结果的 `status` 为 `normal`。奖励 1 还要求退出码为 0。`reward` 保留官方
结果，`verifier` 保留原始记录。可用的奖励 1 判为 P，可用的奖励 0 判为 F，
准备成功后的失败也按此处理。

准备失败和不可用的原评分结果返回 E，记录为 `status="facility_error"`、
`reward=None` 与 `retry_kind="score_only"`。`failure_phase` 区分准备与评分
阶段，`reason` 保存具体故障。准备失败时，官方评分尚未执行。保留候选与准备证据，检查任务环境，恢复题面已提供的前提后，在新的同候选副本上评分。探针失败意味着需要检查，镜像、启动配置或候选均可能造成服务缺失，具体原因由原始证据确定。

若准备消耗原900秒中的2秒，评分最多获得剩余898秒。超时和取消调用环境的 abort 路径。abort 失败会保留错误，并暂停自动补评，待现有环境检查完成后继续。原评分错误和不可用结果仍保存在返回记录中。

批次控制器保存原成绩，将每次恢复结果关联到同一候选与任务尝试。辅助函数向
控制器返回一次尝试的记录。

## 回归覆盖

Linux 信号测试执行真实 `EXEC_WRAPPER` 和 `CANCEL_EXEC`，覆盖标准输入、进程组、SIGINT 送达、外部取消及内部超时。缺少所需 Linux 工具的平台会明确跳过。它们补充了已有的延迟退出测试，后者以直接启动 Python 替代 Docker 传输。

准备测试覆盖旧帧、没有帧、归档失败、文件变动、不支持的文件类型、登录失败和其他任务。评分测试验证准备失败会跳过官方评分、真实错误保持F，以及准备与评分共享原时限。

```bash
pytest -q tests/generation/test_terminal_verifier_preparation.py \
  tests/generation/test_terminal_verifier.py \
  tests/generation/test_terminal_command_signals.py
```
