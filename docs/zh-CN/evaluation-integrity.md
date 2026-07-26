# 评测完整性

[English](../evaluation-integrity.md) | **简体中文**

OpenCollab-Eval 将评测结果视为一条 executable evidence chain。当 declared
target test 针对 Solver 生成的同一个候选真实执行，且所有必要目标都通过时，
任务才会成为 resolved。

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

链条中缺少任何一环都会产生 technical failure。具有有效证据的完整测试运行
可以在 declared target 失败时产生 unresolved result。

## 权威来源与信任边界

数据集声明 instance identity、base commit、image、target test 和 test patch。

evaluation controller 持有 runtime synchronization、trusted baseline、
candidate construction、official workspace preparation、test-plan
generation、evidence parsing 和 terminal report。

Solver 只持有它在一次性工作区中完成的修改。它的 Git metadata、
self-reported diff、prose 和 self-test output 都属于辅助输入。

official-evaluation process 执行 controller-generated plan，并写入 bounded
evidence artifact。完成 container 和 process cleanup 后，host 只从自有
output directory 中读取 allowlisted regular file。

## Public baseline preparation

Solver 启动前，task image 会按照数据集的 `base_commit` 接受检查。任务需要的
runtime dependency 与 candidate view 分离。Repository history 被替换为由
trusted base tree 构造的 deterministic anonymous commit。

准备后的 Solver repository 只有一个 commit，不含 remote、replace ref 和
未来 object history。preparation record 绑定 declared commit、source tree、
anonymous commit、image identity 和 workspace digest。

一个由 controller 持有的 bare Git directory 会在 Solver-visible workspace
之外被捕获。它包含 candidate construction 使用的 trusted baseline commit
和 tree object。Solver 对 `.git`、alias、hook、config、ignore file、ref、
reflog、replace ref 或 worktree index 的修改都无法改变这个
controller-owned baseline。

## 工作区分类

workspace finding 按 phase、origin、Solver visibility、model change、
candidate effect、test effect、evidence effect、identity effect、
repairability 和 patch representability 分类。

| Outcome | 含义 |
| --- | --- |
| `allow` | 状态具有可信来源，或已经证明与结果之间存在无害关系 |
| `sanitize_then_continue` | 可从 task copy 删除一次性残留，且 task semantics 不变 |
| `task_technical_failure` | 当前 task 或 image state 无法表示、修复或证明无害 |
| `pause_batch` | 直接探测证明 shared infrastructure 已失败 |

来自 declared commit、public task input 或 allowed runtime dependency 的
baseline state 可以继续。cache、log 和 temporary residue 可以从 disposable
copy 删除并重新检查。来源未知且 Solver-visible 的 baseline content 会让
受影响的 image 失败。

Solver 退出后，能够表示的 current-run change 会成为 candidate input。
无法表示且影响结果的 change 会让 task 失败。来源未知的 cross-run state
同样会让 task 失败。

## 进程静止

候选提取在 Solver 关闭和 container-wide process cleanup 之后开始。
supervisor 检查自有 process group 和 container state。只有不存在能够继续
写入的自有进程时，workspace 才会成为 candidate input。

不完整的 cleanup 会将 `execution_quiesced` 或 `cleanup_quiesced` 设为 false。
此时候选不再 eligible，official evaluation 无法产生 resolved verdict。

## Controller-owned candidate construction

候选构造使用外部 trusted Git directory、冻结后的 Solver worktree 和一个
新的 temporary index。

Git environment 会清除继承的 `GIT_*` 权限，禁用 system 和 global
configuration、replace object、hook 和 filesystem monitor，使用 literal
pathspec，并安装 controller-owned attribute policy。文件内容在没有 clean
filter 和 text transformation 的情况下计算 hash。

temporary index 从 trusted base tree 开始。tracked change 和 deletion 从
最终文件系统加入索引。untracked path 通过 NUL delimiter 枚举，并按照
baseline ignore view 分类。Solver 修改后的 `.gitignore` 和
`.gitattributes` 可以作为普通文件变更进入候选，但无法隐藏另一个 candidate
path，也无法改变其字节。

被 ignore 的 cache、log、build 和 generated runtime path 保持在候选之外，
candidate selection 期间不会打开这些路径。`.git`、`.opencollab` 和 retired
artifact prefix 下的 harness control path 会被排除。

regular file、deletion、binary data、内部 symbolic link 和 executable mode
change 使用 Git-native representation。hard-linked candidate file 转换为
独立 regular file。nested repository 的 solver-owned `.git` marker 会在
投影可见文件前删除。outward symbolic link、unreadable candidate file、
FIFO、socket、device 和不受支持的 Gitlink change 会产生 task-scoped
construction error。

每个 baseline Gitlink 都会获得显式 preserve、delete 或 replacement
projection。replacement 携带在 official workspace 中重建所需的 evidence。

最终的 `CandidatePatch` 记录以下内容。

| Field | 用途 |
| --- | --- |
| `anonymous_base` | Deterministic solver-visible baseline commit |
| `base_tree` | Trusted baseline tree |
| `baseline_sha256` | Trusted baseline representation digest |
| `candidate_tree` | 由 temporary candidate index 创建的 tree |
| `patch_sha256` | Binary full-index patch 的 SHA-256 |
| `changed_paths` | Canonical candidate path set |
| `path_modes` | 每个 changed path 的 old 和 new Git mode |
| `untracked_paths` | 被选中的 visible addition |
| census fields | 提取期间使用的 bounded file 和 byte count |

序列化后的 proof 声明其 base 和 index 来自 controller，并且排除了 solver
Git metadata 和 forced ignored file。

## 全新的 official workspace

official evaluation 从 task image 启动一个全新的 container。同一 patch
需要经过三次 projection check。

source projection 使用 clean temporary index 将 patch 应用到 declared
dataset commit。它根据 generation expectation 验证 source base commit、
source base tree、anonymous base、source candidate tree 和 patch SHA-256。

随后，evaluation workspace 被缩减为 public single-commit baseline。
repository setup command 可以准备 dependency，之后的检查要求 prepared Git
head 保持 expected baseline identity。prepared projection 将同一 patch
应用到该 baseline，并记录其 candidate tree。

patch 到达实际 official worktree 后，verification 将每个 changed file、
symbolic link、mode、deletion 和 Gitlink 计算到另一个 temporary index 中。
计算出的 worktree tree 必须等于 prepared candidate tree。只有
`official_worktree_matches` 成为 true，target execution 才会开始。

这一 two-base design 能够处理 public preparation 生成 deterministic
anonymous commit 的 image，同时保留 generation 阶段的 source-tree
identity。

## Test-plan contract

Pro-Lite test plan 包含 schema、adapter、declared target、ordered target
batch、ordered command、proof description、runtime dependency 和 verified
coverage mode。

flatten 后的 target batch 必须等于 declared target list。command 和 proof
batch 数量必须与 target batch 相同。empty command、`true`、`:` 和其他
no-op form 会被拒绝。不受支持的 target syntax 会产生 technical failure，
不会得到 passing command。

`FAIL_TO_PASS` 是必需项。`PASS_TO_PASS` 可以为空。存在这两类目标时，它们
使用相同的 parser-backed execution rule。

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
| Unresolved | 完整 official evaluation，以及至少一个 declared target 的有效 failing evidence |
| Technical failure | generation、identity、projection、evidence、cleanup 或 infrastructure 无效 |

evaluator 根据 durable artifact snapshot 推导 verdict。technical reason
包括 unsafe 或 missing output、incomplete target evidence、Docker failure、
non-quiescent process、failed container cleanup、baseline mismatch、
candidate projection mismatch、service setup failure、repository preparation
failure、patch application failure，以及 target log 中的 infrastructure
signature。

只有 technical-reason set 为空，并且每个 F2P 与 P2P evidence batch 上的
`target_evidence_passed` 都返回 true，`resolved` 才会成为 true。

## Evaluation state

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

## Review checklist

当 evaluation change 保持 solver-visible input 公开、judge field 密封、
candidate 来自共享 controller-owned constructor，并且 official workspace
验证 applied tree 时，它便具备 review 条件。

每个新 test adapter 都需要 structural plan validator 和能够证明 declared
target 的 independent parser。每个新 report 都需要完整 identity binding 和
bounded artifact read。每条 cleanup path 都需要在 candidate extraction 前
提供 observable quiescence。每个 batch-wide stop 都需要对其声明失败的
shared dependency 进行 direct probe。
