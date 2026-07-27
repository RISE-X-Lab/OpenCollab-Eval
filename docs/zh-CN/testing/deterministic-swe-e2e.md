# 确定性 SWE 端到端测试

[English](https://rise-x-lab.github.io/OpenCollab-Eval/en/testing/deterministic-swe-e2e/) | **简体中文**

确定性 E2E 在不联系真实模型服务商的情况下验证安装后的完整评测路径。它会
真实使用构建后的 OpenCollab 和 OpenCollab-Eval wheel、临时 SSH、`rsync`、
本地 OpenAI-compatible 服务、一次性 Git 任务、Docker、可信候选提取、
official target execution、终态报告和自有资源清理。

## 测试证明的事实

合成仓库中的 `calculator.add()` 错误地执行了减法。它的目标测试在可信
基线上失败。假模型发出确定性的工具调用，检查源码，将减法改为加法，运行
目标测试，然后结束。

生产 runner 通过 SSH 同步两个已安装的源码树，并记录相同的本地和远端 tree
身份。候选路径提取一个非空 patch，并将其 SHA-256 与 run identity 和
record identity 绑定。official SWE-bench harness 在新工作区中应用同一
patch，收集一个目标，使该目标通过，并报告一个 resolved 任务和零项
technical failure。

镜像中包含基线文件 mode、失效链接、被 ignore 的缓存、未跟踪残留、嵌套
仓库和未来 Git 状态，用于验证 Solver 启动前的净化。另一个完整性 smoke
测试覆盖可恢复残留、task-scoped 镜像拒绝、并发任务隔离，以及无法达到静止
状态的后台写入者。

## 假模型契约

本地服务实现 model listing 和 Chat Completions。它接受固定的合成 token
以及测试 fixture 使用的 `kimi-for-coding` 身份。它验证 262144-token
context、temperature 1、top-p 0.95、maximum output 32768 和保留的
thinking history。

该身份属于确定性测试 fixture。它不负责选择或验证所有生产服务商 profile。

请求会写入经过脱敏且限定于当前 run 的 transcript。未知路由、错误身份、
错误 sampling 配置、畸形请求和服务提前退出都会使运行失败。

## 本地运行

宿主机需要 Docker、`sshd`、`ssh`、`ssh-keygen` 和 `rsync`。当 OpenCollab
源码根目录不是相邻的 `../OpenCollab` checkout 时，需要显式提供它。

```bash
export OPENCOLLAB_SOURCE_ROOT=/path/to/OpenCollab
scripts/run_deterministic_swe_e2e.sh \
  --output /tmp/opencollab-eval-e2e \
  --runs 1
```

输出目录必须为空。使用 `--runs 3` 可以进行重复稳定性验证。每次重复运行都会
获得新的 run ID，并在全新环境中生成相同的候选 patch。

## 证据与清理

每次运行都会记录 runtime synchronization、model transcript、prediction、
generation metrics、candidate identity、production report、independent
official proof、validation summary 和 cleanup result。成功要求 patch hash
一致，`resolved=1`、`unresolved=0`、`technical_failed=0`，恰好收集一个
目标，并完成全部清理。

清理范围限制为带有当前 run ID 的进程、容器、镜像、密钥、端口和目录。最终
记录证明假模型已经停止，自有容器和镜像已经删除，临时工作目录已经消失，
真实服务商变量没有进入测试。

## CI

`deterministic-e2e` GitHub Actions job 构建两个 wheel，安装 SSH 和
`rsync`，并在十分钟 job timeout 下运行一次。失败产物保留十四天。本地发布
验证可以连续运行三次。

聚焦单元测试覆盖模型启动前的 runtime digest mismatch、错误 model
identity、patch digest mismatch、错误 context identity、zero collected
tests、服务提前退出和资源残留。
