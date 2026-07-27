# OpenCollab-Eval 架构

[English](../architecture.md) | **简体中文**

OpenCollab-Eval 是基于 OpenCollab 的 Solver 所使用的评测实现。它加载
benchmark task，只向 Solver 暴露公开任务数据，根据可信基线构造候选 patch，
在全新的 evaluation workspace 中运行目标测试，并发布与证据绑定的终态结果。

OpenCollab 提供 agent runtime、environment、tool 和 workflow decorator。
OpenCollab-Eval 持有 benchmark adapter、Solver 配置、候选构造、test
execution proof、evaluation state 和 report。

## 依赖边界

生产代码的依赖方向如下。

```text
opencollab_eval
        |
        v
documented OpenCollab public API
```

OpenCollab-Eval 导入以下 OpenCollab public surface。

| Public module | 使用的能力 |
| --- | --- |
| `opencollab` | `OpenCollab` 和 `RunResult` |
| `opencollab.environments` | `Environment`、`attach_container`、`docker_environment` 和 `worktree_environment` |
| `opencollab.tools` | `BuiltinToolName`、`Tool` 和 `builtin_tools` |
| `opencollab.workflows` | `workflow` |

已退役的 `opencollab.sdk` 包以及 OpenCollab 的 `adapters`、`application`、
`bootstrap`、`domain` 和 `harness` 等实现层均位于该依赖边界之外。
`tests/test_boundaries.py` 扫描 import，并根据已安装的 OpenCollab 包检查允许
使用的 public name。

OpenCollab-Eval 通过 `opencollab>=0.4,<0.5` 获得有版本约束的 runtime
dependency。只要 documented public API 保持兼容，OpenCollab 内部实现变化
就不会影响这里。

## 包结构

| Package | 职责 |
| --- | --- |
| `contracts` | 在 benchmark、Solver 和 judge 信任边界之间传递的值 |
| `benchmarks` | Dataset 加载、验证、task 规范化和 public identity 推导 |
| `workflows` | 使用 OpenCollab public API 组装的 evaluation-owned Solver workflow |
| `engine` | 状态、执行、checkpoint、test plan、evidence、candidate projection 和 remote primitive |
| `generation` | Solver adapter、一次性工作区、process quiescence 和候选构造 |
| `commands` | Local run、remote Pro-Lite、rejudge、monitor 和 report 的安装命令 |
| `resources` | 随包发布的 shell entrypoint 和 container-side helper program |
| `configs` | 随包发布的 workflow 配置 |

包根目录中的小型共享模块实现 bounded report I/O、patch path parsing、
Gitlink handling、runtime configuration 和 model usage accounting。

## 数据归属

规范化后的 benchmark task 分为两个部分。

`PublicTask` 包含匿名 task identifier、repository name、problem statement、
public hint 和显式 public metadata。匿名 identifier 是一个 HMAC-derived
值。Public metadata 会拒绝 judge field 和其他 sealed value。

`JudgeSpec` 保留原始 instance identifier、base commit、evaluation image、
`FAIL_TO_PASS`、`PASS_TO_PASS` 和 test patch。Evaluation controller 使该对象
保持在 Solver 输入之外。

这两个值共同组成 `BenchmarkTask`。

```text
dataset row
    |
    +---- public fields ----> PublicTask ----> solver
    |
    +---- sealed fields ----> JudgeSpec  ----> evaluator only
```

较底层的 Pro-Lite adapter 还包含类型化的 `TaskSpec`、`WorkspaceSpec`、
`PatchCandidate`、`EvalResult` 和 `RunRecord`。数据在 generation、execution
和 reporting 之间移动时，这些 record 显式表达 task、candidate 和 official
result identity。

## 执行入口

安装后的 `oc-eval` 命令提供四个面向用户的入口。

| Command | 用途 |
| --- | --- |
| `oc-eval inspect` | 通过 sealed task boundary 验证并汇总 SWE-Batch Pro JSONL 数据集 |
| `oc-eval run` | 在 task JSONL 上运行 local headless evaluation engine |
| `oc-eval swe-v1-prolite` | 使用已同步 runtime 和 direct evaluation 运行有界 remote Pro-Lite slice |
| `oc-eval final-report` | 验证两份完整 terminal fact report 并渲染绑定后的对比发布物 |

专用 command module 支持 G1.1 parallel scheduling、OpenHands 和 Claude Code
adapter、direct rejudging、frozen manifest、token summary 和运行监控。这些
模块是安装后的 Python module，并使用 package import。无法产生当前 isolation
evidence 的 legacy shell launcher 会在启动 Solver 前以 technical status
终止。

## 端到端执行

生产 SWE 路径按以下顺序执行。

```text
validated dataset row
        |
        v
sealed task normalization
        |
        v
local evaluation controller
        |
        +---- runtime tree manifest and SHA-256
        |
        v
synchronized remote OpenCollab and OpenCollab-Eval source
        |
        v
fresh task image and disposable solver workspace
        |
        v
verified single-commit public baseline
        |
        v
OpenCollab workflow or external solver adapter
        |
        v
container-wide process quiescence
        |
        v
controller-owned candidate projection
        |
        v
fresh official-evaluation workspace
        |
        v
parser-backed FAIL_TO_PASS and PASS_TO_PASS execution
        |
        v
identity-bound terminal report
```

local controller 同步完整的必要源码树。它写入 runtime manifest，其中包含
member list、aggregate size 和 SHA-256。remote side 在 generation 前重新
计算同一个 identity。shared preflight 可以将后续 task run 绑定到同一个
runtime tree。这样，部分同步或过期的远端安装无法静默评测候选。

每次 generation attempt 都有自己的 run identity、artifact directory、
container ownership record 和一次性 repository state。Solver 看到的是一个
普通单提交 Git 仓库，可以继续使用熟悉的开发工具。另有一个由 evaluation
controller 持有的 Git directory 记录可信基线，并且从不进入 Solver-visible
mount。

Solver 关闭后，controller 检查 process quiescence，并冻结最终 workspace
view。候选构造通过可信基线和 temporary index 读取最终文件。Solver-owned
Git config、ref、object history 和 index state 均无权决定 candidate
identity。

official evaluation 从全新的 image workspace 启动。controller 首先将
patch 投影到数据集声明的 commit 上。随后，它准备 public single-commit
workspace，将同一 patch 投影到 prepared base，应用该 patch，并根据实际
worktree 重新计算 resulting tree。只有这些 tree identity 一致，目标测试
才会开始。

## Solver 集成

evaluation-owned workflow 位于 `opencollab_eval.workflows`。它们使用
OpenCollab workflow decorator 和 tool factory，同时使 benchmark secret
保持在 workflow argument 之外。

内置 workflow Solver registry 当前包含 G1.1、BaseTeam、TeamPro、
OpenHands 和 Claude Code 配置。workflow Solver 将 agent lifecycle
management 委托给 OpenCollab。external Solver adapter 在一次性容器中
启动对应工具，并将 sidecar usage 和 candidate evidence 返回到同一条
generation 路径。

所有 adapter 最终都会进入共享 candidate construction。adapter-specific
shell code 可以启动进程并收集 sidecar。Patch canonicalization、candidate
tree calculation、patch SHA-256 和 official projection 仍由公共的
evaluation service 完成。

## Runtime 与工作区边界

local process 持有 dataset parsing、credential、run configuration、remote
runtime synchronization、scheduling 和 durable report。

Solver container 只持有当前 task workspace 和临时 Solver artifact。它接收
public task text 和配置后的 model connection。Judge target、reference
material、未来 repository history 和其他 run 的 artifact 都不会进入该
workspace。

official-evaluation container 接收完成绑定的 candidate patch、judge test
specification、parser-backed test program 和 allowlisted output directory。
它创建自己的 public baseline，并在执行测试前验证 applied candidate tree。

生成的 prediction、trajectory、log、report、dataset、patch 和 PDF 应位于
source checkout 之外的 run directory。仓库中保存 schema、code、test 和
可复用 documentation。

## 状态与报告

generation 和 official evaluation 是相互独立的状态。只有 submission
integrity evidence 有效，具有 terminal generation metric 的非空 patch 才能
进入 official evaluation。empty patch、incomplete metric、identity pairing
failure 和 failed generation 会保留为不同的 task state。

技术上完整的运行结束后，official evaluation 产生 `eval_done`。在该状态内，
`resolved` 记录每个 declared target 是否都具有 passing execution proof。
infrastructure、artifact、cleanup、projection 或 evidence failure 会产生
`technical_eval_failed`。

report 将 task identity、generation record、patch SHA-256、runtime tree、
candidate projection、test plan、parser evidence、container cleanup 和
final verdict 连接起来。Aggregate summary 分别统计 resolved、unresolved
和 technical-failure task。

## 扩展系统

新的 benchmark adapter 应将输入规范化为 public Solver view 和 sealed
judge view。dataset-specific parsing 应放在 `benchmarks` 或
`engine.eval_adapter` 下。

新的 workflow 应只使用 documented OpenCollab public import，并放在
`workflows` 下。其 task input 应包含 public issue information。

新的 external Solver 应复用 disposable snapshot preparation、process
quiescence 和共享 candidate constructor。它的 sidecar 应报告 usage 和
Solver identity，不能成为 patch content 的权威来源。

新的 language test adapter 应创建 deterministic test plan，使其 declared
target、command batch 和 parser proof batch 之间存在 machine-checkable
one-to-one relationship。任意 shell 命令的成功无法证明 target execution。

新的 report field 应从 durable bounded artifact 推导，并保留将其连接到
同一个 task、run、candidate 和 evaluation attempt 所需的 identity field。
