# OpenCollab-Eval 架构

[English](../architecture.md) | **简体中文**

OpenCollab-Eval 评测使用 OpenCollab 构建的 Solver。它向 Solver 提供公开任务
数据，根据可信基线构造候选补丁，并在全新的工作区中运行指定测试。终态报告会
把结果与记录下来的证据绑定。

OpenCollab 提供智能体运行时和工作流编写接口。OpenCollab-Eval 负责面向基准
的代码、候选构造、执行证据、评测状态和报告。

## 依赖边界

生产代码的依赖方向如下。

```text
opencollab_eval
        |
        v
documented OpenCollab public API
```

OpenCollab-Eval 使用以下 OpenCollab 公开接口。

| Public module | 使用的能力 |
| --- | --- |
| `opencollab` | `OpenCollab` 和 `RunResult` |
| `opencollab.environments` | `Environment`、`attach_container`、`docker_environment` 和 `worktree_environment` |
| `opencollab.tools` | `BuiltinToolName`、`Tool` 和 `builtin_tools` |
| `opencollab.workflows` | `workflow` |

已退役的 `opencollab.sdk` 包以及 OpenCollab 的 `adapters`、`application`、
`bootstrap`、`domain` 和 `harness` 等实现层均位于该依赖边界之外。
`tests/test_boundaries.py` 规定生产代码与测试可以使用哪些导入，并根据已安装的
OpenCollab 包检查这些公开名称。

OpenCollab-Eval 通过 `opencollab>=0.4.1,<0.5` 声明运行时依赖的版本范围。
OpenCollab 的公开 API 保持兼容时，其内部实现变化不会影响这里。

## 包结构

| Package | 职责 |
| --- | --- |
| `contracts` | 在基准、Solver 与裁判信任边界之间传递的值 |
| `benchmarks` | 数据集加载、验证、任务规范化与公开身份推导 |
| `workflows` | 使用 OpenCollab 公开 API 组装的 Solver 工作流 |
| `engine` | 状态、执行、检查点、测试计划、证据、候选投影与远程执行基础能力 |
| `generation` | Solver 适配器、一次性工作区、进程静止检查与候选构造 |
| `commands` | 本地运行、远程 Pro-Lite、重新评测、监控与报告命令 |
| `resources` | 随包发布的 shell 入口与容器侧辅助程序 |
| `configs` | 随包发布的工作流配置 |

包根目录中的小型共享模块负责有界报告读写、补丁路径解析、Gitlink 处理、
运行时配置和模型用量统计。

## 数据归属

规范化后的基准任务包含两条记录。

`PublicTask` 包含匿名任务标识、仓库名、问题描述、公开提示和明确标为公开的
元数据。匿名标识由 HMAC 推导。公开元数据会拒绝裁判字段和其他密封值。

`JudgeSpec` 保留原始实例标识、基准提交、评测镜像、`FAIL_TO_PASS`、
`PASS_TO_PASS` 和测试补丁。评测控制器将该对象留在 Solver 输入之外。

这两个值共同组成 `BenchmarkTask`。

```text
dataset row
    |
    +---- public fields ----> PublicTask ----> solver
    |
    +---- sealed fields ----> JudgeSpec  ----> evaluator only
```

较底层的 Pro-Lite 适配器还包含类型化的 `TaskSpec`、`WorkspaceSpec`、
`PatchCandidate`、`EvalResult` 和 `RunRecord`。数据在生成、执行和报告之间
传递时，这些记录明确标出任务、候选和官方结果的身份。

## 执行入口

安装后的 `oc-eval` 命令提供四个用户入口。

| Command | 用途 |
| --- | --- |
| `oc-eval inspect` | 通过密封任务边界验证并汇总 SWE-Batch Pro JSONL 数据集 |
| `oc-eval run` | 根据任务 JSONL 运行本地无界面评测引擎 |
| `oc-eval swe-v1-prolite` | 使用已同步运行时和直接评测运行有界的远程 Pro-Lite 切片 |
| `oc-eval final-report` | 验证两份完整终态事实报告并生成绑定后的对比发布物 |

专用命令模块支持 G1.1 并行调度、OpenHands 与 Claude Code 适配器、直接重新
评测、冻结清单、token 汇总和运行监控。这些模块随 Python 包安装，并使用包内
导入。旧版 shell 启动器缺少当前要求的隔离证据，因此会在启动 Solver 前以
技术状态终止。

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

本地控制器同步完整的必要源码树，并写入包含成员列表、总大小和 SHA-256 的
运行时清单。远端在生成前重新计算同一身份。共享预检可以把后续任务运行绑定到
同一棵运行时源码树，部分同步或过期的远端安装因此无法悄然参与评测。

每次生成尝试都有自己的运行身份、产物目录、容器所有权记录和一次性仓库状态。
Solver 看到普通的单提交 Git 仓库，可以继续使用常见开发工具。评测控制器另行
持有一个记录可信基线的 Git 目录，该目录始终位于 Solver 可见挂载之外。

Solver 关闭后，控制器检查进程是否静止，并冻结工作区的最终视图。候选构造通过
可信基线和临时索引读取最终文件。候选身份由控制器决定，不受 Solver 持有的 Git
配置、引用、对象历史或索引状态影响。

官方评测从全新的镜像工作区启动。控制器先把补丁投影到数据集声明的提交上，
随后准备一个公开的单提交工作区，把同一补丁投影到准备后的基线并应用。控制器
再根据实际工作树重新计算结果树，只有各项树身份一致后才会启动目标测试。

## Solver 集成

评测工作流位于 `opencollab_eval.workflows`。它们使用 OpenCollab 的工作流
装饰器与工具工厂，同时把基准秘密留在工作流参数之外。

内置 Solver 注册表当前包含 G1.1、BaseTeam、TeamPro、OpenHands 和 Claude
Code 配置。工作流 Solver 把智能体生命周期交给 OpenCollab 管理。外部 Solver
适配器在一次性容器中启动对应工具，并把 sidecar 用量和候选证据送入同一条生成
路径。

所有适配器都使用共享的候选构造器。适配器专用的 shell 代码可以启动进程并
收集 sidecar，补丁规范化、候选树计算、补丁 SHA-256 与官方投影仍由公共评测
服务完成。

## Runtime 与工作区边界

本地进程负责数据集解析、凭据、运行配置、远程运行时同步、调度和持久报告。

Solver 容器持有当前任务工作区和临时 Solver 产物。它接收公开任务文本和配置
后的模型连接。裁判目标、参考材料、未来仓库历史与其他运行的产物都留在工作区
之外。

官方评测容器接收已绑定的候选补丁、裁判测试规范、由解析器支持的测试程序和
白名单输出目录。它会创建自己的公开基线，并在执行测试前验证应用后的候选树。

生成的预测、轨迹、日志、报告、数据集、补丁和 PDF 应放在源码检出之外的运行
目录。仓库保存 schema、代码、测试和可复用文档。

## 状态与报告

生成与官方评测使用相互独立的状态。具有终态生成指标的非空补丁，还需通过提交
完整性证据检查才能进入官方评测。空补丁、指标不完整、身份配对失败和生成失败
会保留为不同的任务状态。

技术流程完整结束后，官方评测产生 `eval_done`。在该状态内，`resolved` 记录
每个指定目标是否都有通过的执行证据。基础设施、产物、清理、投影或证据故障会
产生 `technical_eval_failed`。

报告把任务身份、生成记录、补丁 SHA-256、运行时源码树、候选投影、测试计划、
解析器证据、容器清理和最终判定关联起来。汇总报告分别统计 resolved、
unresolved 和技术失败任务。

## 扩展系统

新增基准适配器时，应把输入规范化为公开的 Solver 视图和密封的裁判视图。特定
数据集的解析代码放在 `benchmarks` 或 `engine.eval_adapter` 下。

新增工作流应使用已有文档说明的 OpenCollab 公开导入，并放在 `workflows`
下。任务输入要包含完整的基准任务规范，生成适配器必须保留所有 Solver 可见
字段。

新增外部 Solver 应复用一次性快照准备、进程静止检查和共享候选构造器。
sidecar 负责报告用量与 Solver 身份，补丁内容仍由候选构造器确定。

新增语言测试适配器应创建确定性测试计划，让指定目标、命令批次与解析器证据
批次保持可由机器检查的一一对应关系。普通 shell 命令成功不足以证明目标已经
执行。

新增报告字段应从持久且大小受限的产物推导，并保留将其关联到同一任务、运行、
候选与评测尝试所需的身份字段。
