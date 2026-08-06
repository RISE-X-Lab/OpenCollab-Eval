# 评测完整性

[English](../evaluation-integrity.md) | **简体中文**

OpenCollab-Eval 在可执行证据完整后接受评测结果。指定目标测试针对 Solver
生成的候选运行且所有必要目标都通过后，任务会成为 resolved。

核心完整性规则如下。

```text
same task
+ same run
+ same trusted baseline
+ same candidate tree
+ same patch SHA-256
+ same official workspace
+ complete target execution proof
= eligible terminal verdict
```

缺少任何一项关联证据都会产生技术失败。完整测试已经留下有效证据，但指定目标
失败时，结果为 unresolved。

## 权威来源与信任边界

数据集声明实例身份、基准提交、镜像、目标测试和测试补丁。

评测控制器负责运行时同步、可信基线、候选构造、官方工作区准备、测试计划
生成、证据解析和终态报告。

Solver 提交自己在一次性工作区中完成的修改。它的 Git 元数据、自报差异、文字
说明和自行运行的测试结果都属于辅助输入。

官方评测进程执行控制器生成的计划，并写入大小受限的证据产物。容器和进程清理
完成后，主机从自有输出目录读取白名单中的普通文件。

## 公开基线准备

Solver 启动前，任务镜像会按照数据集的 `base_commit` 接受检查。任务需要的
运行时依赖与候选视图分离。仓库历史会替换为从可信基准树构造的确定性匿名提交。

准备后的 Solver 仓库只有一个提交，不含远端、替换引用和未来对象历史。准备
记录会绑定声明提交、源码树、匿名提交、镜像身份和工作区摘要。

控制器在 Solver 可见工作区之外持有一个裸 Git 目录。其中保存候选构造所用的
可信基准提交与树对象。该基线由控制器持有，不受 Solver 修改 `.git`、别名、
钩子、配置、忽略文件、引用、引用日志、替换引用或工作树索引的影响。

## 工作区分类

工作区发现项按照阶段、来源、Solver 可见性、模型改动、候选影响、测试影响、
证据影响、身份影响、可修复性与补丁可表示性分类。

| Outcome | 含义 |
| --- | --- |
| `allow` | 状态具有可信来源，或已经证明与结果之间存在无害关系 |
| `sanitize_then_continue` | 可从任务副本删除一次性残留，且任务语义不变 |
| `task_technical_failure` | 当前任务或镜像状态无法表示、修复或证明无害 |
| `pause_batch` | 直接探测证明共享基础设施已失败 |

来自声明提交、公开任务输入或允许的运行时依赖的基线状态可以继续使用。缓存、
日志和临时残留可以从一次性副本删除并重新检查。来源未知且 Solver 可见的基线
内容会让受影响镜像失败。

Solver 退出后，本次运行中能够表示的改动会成为候选输入。无法表示且影响结果
的改动会让任务失败，来源未知的跨运行状态也按任务失败处理。

## 进程静止

候选提取在 Solver 关闭和容器进程清理之后开始。监督器检查自有进程组和容器
状态。自有进程全部停止写入后，工作区才会成为候选输入。

清理不完整时，`execution_quiesced` 或 `cleanup_quiesced` 会设为 false，
候选随即失去评测资格，官方评测无法产生 resolved 判定。

## 控制器持有的候选构造

候选构造使用控制器在外部持有的可信 Git 目录、冻结后的 Solver 工作树和一个
新的临时索引。

Git 环境会清除继承的 `GIT_*` 权限，并禁用系统级与全局配置。替换对象、钩子
和文件系统监视器也会停用。路径按照字面值解释，属性规则由控制器提供。文件
内容计算哈希时不经过 clean filter 或文本转换。

临时索引从可信基准树开始。最终文件系统中的已跟踪改动与删除会加入索引，
未跟踪路径则通过 NUL 分隔符枚举，并按照基线忽略规则分类。Solver 修改后的
`.gitignore` 和 `.gitattributes` 可以作为普通文件变更进入候选，其内容不会
影响其他候选路径的可见性或字节。

忽略的缓存、日志、构建产物和运行时生成路径留在候选之外，候选选择期间也不会
打开。`.git`、`.opencollab` 和旧版产物前缀下的评测控制路径会被排除。

普通文件、删除、二进制数据、内部符号链接和可执行位变化使用 Git 原生表示。
硬链接候选文件会展开成独立的普通文件。嵌套仓库中由 Solver 持有的 `.git`
标记会在投影可见文件前删除。指向外部的符号链接、不可读候选文件、FIFO、
套接字、设备和不受支持的 Gitlink 变化会产生任务级构造错误。

每个基准 Gitlink 都会明确保留、删除或替换。替换记录带有在官方工作区重建时
需要的证据。

最终的 `CandidatePatch` 记录以下内容。

| Field | 用途 |
| --- | --- |
| `anonymous_base` | Solver 可见的确定性基准提交 |
| `base_tree` | 可信基准树 |
| `baseline_sha256` | 可信基线表示的摘要 |
| `candidate_tree` | 临时候选索引创建的树 |
| `patch_sha256` | 二进制完整索引补丁的 SHA-256 |
| `changed_paths` | 规范化的候选路径集合 |
| `path_modes` | 每条变更路径的新旧 Git 模式 |
| `untracked_paths` | 选中的可见新增路径 |
| census fields | 提取期间使用的有界文件数与字节数 |

序列化证据会声明基线和索引来自控制器，并记录 Solver Git 元数据与强制忽略
文件已经排除。

## 全新的官方工作区

官方评测从任务镜像启动一个全新容器，同一补丁需要经过三次投影检查。

源码投影使用干净的临时索引把补丁应用到数据集声明的提交。它根据生成阶段的
预期值检查源码基准提交、源码基准树、匿名基线、源码候选树和补丁 SHA-256。

随后，评测工作区会缩减为公开的单提交基线。仓库初始化命令可以准备依赖，后续
检查则要求准备后的 Git head 保持预期基线身份。准备投影把同一补丁应用到该
基线，并记录候选树。

补丁进入实际官方工作树后，验证程序把每个变更文件、符号链接、模式、删除项与
Gitlink 写入另一个临时索引。计算出的工作树必须等于准备阶段的候选树。
`official_worktree_matches` 成为 true 后，目标测试开始执行。

这种双基线设计可以处理公开准备阶段生成确定性匿名提交的镜像，同时保留生成
阶段的源码树身份。

## 测试计划契约

Pro-Lite 测试计划包含 schema、适配器、指定目标、有序目标批次、有序命令、
证据说明、运行时依赖和经过验证的覆盖模式。

展平后的目标批次必须等于指定目标列表。命令和证据批次的数量必须与目标批次
相同。空命令、`true`、`:` 和其他空操作形式会被拒绝。不受支持的目标语法会
产生技术失败。

`FAIL_TO_PASS` 是必需项，`PASS_TO_PASS` 可以为空。这两类目标使用相同的
解析器执行规则。

## Python 证据

Pytest plan 通过 official container 内的 trusted controller program 执行。
controller 具有预留 proof file 和准备 disposable worker home 所需的权限。
它使用不同的 unprivileged user 和新的 process session 启动 Pytest worker。

command identity 是精确 argument vector 的 SHA-256。controller 只接受预期
Pytest launcher，以及从 evaluation input directory 加载的一个 trusted
proof plugin。proof plugin 加载后，candidate source path 才会进入 worker
import view。

plugin 通过继承的 file descriptor 发送 structured JSONL event。controller
要求一个 session start、一个 collection finish、一个 session finish、
按顺序排列的 per-node phase report、相互一致的 process 与 Pytest exit
status、protocol EOF，并要求不存在 surviving event writer。它记录 worker
PID、worker 和 controller identity、command SHA-256、return code 和 event
stream SHA-256。

成功的 batch 需要至少收集一个 node，并且与 declared target 匹配的每个 node
都具有完整且 passed 的 `setup`、`call` 和 `teardown` phase。parameterized
target 可以使用 verified parent fallback。fallback parent list 必须完全由
declared parameterized target 推导，每个 collected node 都必须位于允许的
exact target 或 parent 下。

当 structured event stream 和 candidate-source binding 指向 declared test
时，import 与 collection failure 可以证明 failing `FAIL_TO_PASS` target。
它们无法证明 passing result。

## Go 证据

Go plan 使用 `go test -count=1 -json`。写成
`path/to/file_test.go::TestName` 的 target 会得到单独的 package command 和
anchored `-run` expression。多个 package 仍是分离的 command，从而保留每个
package-to-test binding。

只声明 test name 且不含 path 的数据集可以使用 runtime discovery。controller
扫描 test file，为每个 package 发出 structured discovery record，然后执行
该 package 中的 exact test。

parser 消费 Go JSON event 和边界明确的 compiler diagnostic。passing proof
要求每个 declared test 都在其 bound package 中具有 `pass` event。dynamic
discovery 还要求每个 declared test 的 ownership 完整，并拒绝 ambiguous
package match。

failing proof 接受 exact target `fail` event。build failure 只有在 package、
test file diagnostic、declared target、observed command 和 planned command
全部一致时才有效。dependency build output 和 unrelated package failure
无法替代 target execution。

## JavaScript 证据

JavaScript 与 TypeScript plan 为 Jest、Mocha 和 ospec 使用 parser-backed
adapter。declared target 会映射到 dataset-selected test file 和 judge test
patch 引入的 file。ambiguous alias、traversal path 和 unverified file mapping
会被拒绝。

Jest 使用 JSON、serial execution、verbose output 和 `runTestsByPath` 执行
显式 test file。Mocha 按 file 对 declared title 分组，构造 anchored
selector，并要求 JSON-stream output。ospec 为其 declared suite 使用
structured launcher。

parser 对照 plan 检查 executed suite 和 target result。只有 process exit
成功无法提供 passing authority。zero test、missing suite、unrelated
passing test、malformed structured output 或 different command 都会让
evidence check 失败。

一个边界严格的 JavaScript suite-load failure 可以证明 `FAIL_TO_PASS`，条件
是单个 declared suite 无法加载由该 suite 的 judge patch 显式 mock 的 module。
repository namespace、suite path、missing module、runtime error count、
test count 和 command identity 必须全部一致。

## 候选与运行身份

generation 和 evaluation record 绑定以下 identity。

| Identity | 绑定内容 |
| --- | --- |
| `instance_id` | Sealed benchmark instance |
| `record_id` | 精确的 prediction 与 metric pair |
| `run_identity_sha256` | Invocation、Solver、model、workflow 和 runtime identity |
| `source_patch_sha256` | Generation 发布的 patch |
| `eval_patch_sha256` | Official evaluation 接受的 patch |
| `source_base_commit` | Generation 使用的 dataset commit |
| `source_anonymous_base` | Deterministic one-commit Solver baseline |
| `source_base_tree` | Trusted source tree |
| `source_candidate_tree` | Generation 期间计算的 candidate tree |
| `runtime_tree_sha256` | 已同步的 OpenCollab-Eval runtime source |
| evaluation attempt fields | 精确的 official execution attempt |

generation patch、candidate proof、prediction row、workflow metric、source
projection、prepared projection、official report 和 aggregate row 必须在
共享 identity 上一致。latest-file lookup 和 matching task name 都无法替代
`record_id` 与完整 SHA-256 pairing。

## Verdict 语义

| Terminal result | 必要事实 |
| --- | --- |
| Resolved | Eligible patch、verified projection 与 cleanup、safe artifact 和 passing target evidence |
| Unresolved | 绑定证据证明 declared target 失败、跳过、候选引起测试前失败，或预期候选 tree 产生前的可信源投影拒绝 |
| Technical failure | 候选身份或评测状态不足以判断候选是否正确 |

evaluator 根据 durable artifact snapshot 推导 verdict。technical reason
包括身份工件不安全或缺失、目标计划不受支持、目标结果未知、Docker 执行失败、
进程未静止、基线不匹配、投影运行失败、仓库准备失败，以及经过直接探测确认的
公共基础设施故障。日志文字本身无法判定基础设施故障。

只有每个 declared F2P 与 P2P target 都具有绑定的 passing evidence，
`resolved` 才会成为 true。一个绑定的候选失败已经足以得到 `unresolved`，
后续 batch 没有运行也不会覆盖这一结论。若尚无其他绑定失败，未知 evidence
仍属于技术失败。进程组已经停止且工作区已经冻结后，容器删除失败会记录为运行
告警。进程无法静止仍属于技术失败。
只有生成阶段尚未记录预期候选 tree 时，可信源投影拒绝才足以证明
`unresolved`。如果源投影拒绝与已经记录的候选 tree 冲突，或补丁在评测准备
基线上遭到拒绝，就说明投影状态不一致，应判为技术失败。

## 评测状态

| State | 含义 |
| --- | --- |
| `needs_generation` | 不存在 prediction |
| `generation_active` | 当前 run 持有的 generation session 仍在运行 |
| `empty_patch_invalid` | Generation 结束但没有 candidate |
| `blocked_missing_metric` | Prediction 缺少对应的 terminal workflow metric |
| `blocked_metric_pairing` | Prediction 与 metric identity 无法配对 |
| `workflow_incomplete` | Workflow 尚未进入 eligible terminal state |
| `workflow_failed` | Generation 进入 terminal failure 或将 submission 标记为 ineligible |
| `ready_for_eval` | 非空 eligible candidate 可以进入 official evaluation |
| `eval_active` | 匹配的 official evaluation 正在运行 |
| `eval_done` | 匹配的 official report 已完成，并包含 resolved 或 unresolved verdict |
| `technical_eval_failed` | Official evaluation 结束但没有 trustworthy terminal evidence |

timeout、context overflow、cancellation、budget exhaustion 和 patch guard
failure 等 generation status 会保留为 generation failure。missing image、
missing specification、empty filtered patch、driver failure 和 failed evidence
等 evaluation status 会保留为 technical evaluation failure。

## 故障范围

| Scope | 影响 |
| --- | --- |
| `none` | 继续当前 task 和 batch |
| `task` | 当前 attempt 失败，其他 task 继续 |
| `image` | 标记受影响的 task image 无效，无关 image 继续 |
| `shared_infrastructure` | direct shared-service probe 失败后暂停新工作 |

文本匹配无法将 failure 提升为 shared scope。task failure 后，parallel runner
可以为 Docker、shared storage、queue state、synchronized runtime 和配置的
model endpoint 发起 fresh probe。确认 shared probe failure 后可以暂停
batch。repository-specific anomaly、没有 shared probe 的 provider error、
patch failure 和 target-test failure 都会保持 local。

## 持久证据

direct evaluation report 包含 base snapshot、source candidate projection、
prepared candidate projection、generation 与 evaluation patch digest、
record identity、evaluation specification digest、runtime dependency
identity、exact command、parser evidence、exit status、bounded log tail、
process quiescence 和 container cleanup。

runner 只从它持有的 temporary directory 发布 allowlisted output name。
missing、duplicated、oversized、non-regular、linked 或 malformed artifact
会增加 technical reason。

aggregate report 保留三个独立计数。`resolved` 统计 proven pass。
`unresolved` 统计技术上完整但失败的候选。`technical_failed` 统计没有有效
semantic verdict 的 task。

final comparison report 在渲染 JSON、Markdown、TeX 或 PDF 输出前验证
dataset identity、task coverage、run identity、candidate SHA-256、
projection evidence、direct execution evidence 和 terminal status。

## 审查清单

当 evaluation change 保持 solver-visible input 公开、judge field 密封、
candidate 来自共享 controller-owned constructor，并且 official workspace
验证 applied tree 时，它便具备 review 条件。

每个新 test adapter 都需要 structural plan validator 和能够证明 declared
target 的 independent parser。每个新 report 都需要完整 identity binding 和
bounded artifact read。每条 cleanup path 都需要在 candidate extraction 前
提供 observable quiescence。每个 batch-wide stop 都需要对其声明失败的
shared dependency 进行 direct probe。
