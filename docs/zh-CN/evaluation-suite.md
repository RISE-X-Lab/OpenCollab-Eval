# 可复用评测方案

[English](../evaluation-suite.md) | **简体中文**

英文原文见 [evaluation-suite.md](../evaluation-suite.md)。这份开发包将现用评测修复整合到 OC 0.6 主分支接口，OC 与 OCE 两个开发版本需要配对安装。OC 提供候选工作区隔离、显式无限预算、继承配置的推理行为、请求生命周期记录和公开模型及快照接口。OCE 负责题面交付、公开依赖准备、可信候选提取、正式测试和结果解释。

## 安装与打包

在 OCE 仓库执行下面的命令，相邻的 `OpenCollab` 目录放置配套 OC 源码。`package-runtime` 使用已有运行包格式，把安装后的源码复制到新目录。目录可以直接复制到评测服务器，供应商凭据通过运维环境文件读取。

```bash
python -m pip install -e '../OpenCollab[dev]' -e '.[dev,swebench]'
export OPENCOLLAB_SOURCE_ROOT="$(cd ../OpenCollab && pwd)"
export EVAL_ROOT="$(pwd)/evaluation-output"
export OPENCOLLAB_EVAL_OUTPUT_ROOT="$EVAL_ROOT/test-output"
mkdir -p "$OPENCOLLAB_EVAL_OUTPUT_ROOT"
oc-eval package-runtime --output "$EVAL_ROOT/runtime"
```

## API 请求并发

直接网关通过共享目录在多个进程和入口之间合并计算同一供应商的请求。响应关闭后释放名额，等待阶段取消和进程退出也会释放对应资源。名额记录写入失败时会关闭已经取得的描述符。

示例文件 `examples/evaluation-suite/provider-limits.json` 分别配置 45、30、100、100 个请求。使用时填入真实上游地址和存储目录，任务数量与 API 实际请求数量分别记录。第四个高费用服务供优先的外部基准和 Single 使用，完成这些组后将其 `enabled` 设为 `false`，已有响应继续结束，后续其他组使用其余入口。

环境文件提供 `OPENCOLLAB_UPSTREAM_BASE_URL`、`OPENCOLLAB_UPSTREAM_API_KEY` 和用于本机调用的独立 `OPENCOLLAB_PROXY_CLIENT_TOKEN`。由服务器服务管理器运行下面的网关命令。

```bash
python -m opencollab_eval.commands.llm_api_proxy   --env-file "$PROVIDER_ENV" --port "$PROVIDER_PORT"   --direct-upstream --timeout 46800   --provider-limits-file "$PROVIDER_LIMITS_FILE"
```

## 服务器本机队列

`TASK_INDICES` 指定需要运行的数据行，`RUN_ID` 使用新名称。`BENCHMARK_ROOT` 指向已有数据与公开准备资源，`IMAGE_REPOSITORY` 指向对应镜像，`MODEL`、`CONTEXT_WINDOW` 和服务地址描述实际模型接口。下面示例启动八个题目任务，由服务器服务管理器或常驻终端持有进程，客户端电脑关机后服务器继续执行。

```bash
python -m opencollab_eval.commands.swe_g11_parallel_runner   --runner-transport local --host localhost   --indices "$TASK_INDICES" --max-workers 8 --min-workers 8   --workflow base-team-single-pass-v1   --run-id "$RUN_ID" --session-prefix "$RUN_ID"   --output-dir "$EVAL_ROOT/$RUN_ID/controller"   --remote-base "$EVAL_ROOT/$RUN_ID/tasks"   --remote-runtime-repo "$EVAL_ROOT/runtime"   --remote-python "$(command -v python)" --remote-root "$BENCHMARK_ROOT"   --image-repository "$IMAGE_REPOSITORY"   --remote-proxy-base-url "$PROVIDER_BASE_URL"   --local-proxy-base-url "$PROVIDER_BASE_URL" --proxy-env-file "$PROVIDER_ENV"   --model-name "$MODEL" --llm-model "$MODEL" --llm-provider openai   --context-window "$CONTEXT_WINDOW" --max-output-tokens 65536   --temperature 1 --budget 1000000000000 --max-steps 1000000000000   --swe-timeout 1000000000000 --task-wall-timeout 1000000000300   --total-timeout 1000001000000 --llm-timeout 46800   --eval-container-bind-timeout 120 --max-task-starts 1   --workflow-env OPENCOLLAB_UNBOUNDED_LIMITS=true   --workflow-env OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1   --workflow-env OPENCOLLAB_THINKING=true   --workflow-env OPENCOLLAB_REASONING_EFFORT=max   --workflow-env OPENCOLLAB_WIRE_PROTOCOL=responses   --workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200
```

显式无限开关会把工作流整题及角色的 token 与步骤上限解析为 `None`。原生 Single 使用传给公开 Agent 接口的数字 `budget` 和 `max_steps`，启动配置应传入同样充分的大数值。较大的命令行数值用于兼容数字参数入口。单次输出和上下文大小仍是模型参数。无进展时间观察完整模型回复和工具动作，触及运维时间边界的等待会保留原始原因，外部原因或归因未明时进入评测中断复核。

Single 使用 OC 正常的 `coding` 配置及系统提示，公开题面包含问题、要求和接口说明。合作设置可以通过 `--workflow` 选择。

| Setting | Workflow entry |
| --- | --- |
| Base Team | `base-team-single-pass-v1` |
| G11 | `validation-council-solve` |
| G20 | `validation-council-wired-v1` |
| G21 | `validation-council-wired-dual-g20-v1` |
| Triple | `validation-council-triple-coder-contract-v1` |
| Dual Contract | `validation-council-wired-dual-contract-v1` |
| G20 + Coder Contract | `validation-council-g20-coder-contract-v1` |
| Red-Green v2 | `validation-council-g20-coder-red-green-v2` |

`claude-code` 使用已有外部 CLI 适配器及相同候选和正式评分设施。mini-swe-agent 和 Native Harbor 在导入候选前也需要获得相同公开题面与已清理的源码视图，其版本与外部启动配置纳入运行来源记录。

## 环境与信息隔离

生成工作区从指定基础代码建立新的匿名 Git 仓库，原始历史、远端引用、隐藏测试补丁、参考答案和评分日志放在求解器视图之外。源码清理前保存公开依赖及构建产物，清理后恢复。NodeBB 的公开构建产物与 Redis 服务准备独立于隐藏测试。使用 Conda 的镜像需要在求解命令开始前确认 `testbed` 激活成功，其他语言保持正常工具链。

候选子工作区继承获准的运行依赖，各自保存源码改动。最终提取继续使用已有静止检查、所有权校验、路径检查和候选身份配对。`OPENCOLLAB_EVAL_OUTPUT_ROOT` 指定服务器存储位置，正式测试容器可以写入该目录。

## 完整角色证据

角色交接完整保留公开报告、结构化字段、路径列表和测试记录。验证决定携带获批测试的完整候选说明，包括命令、准备步骤、断言和契约引用。Coder 同时收到定位、需求、测试地图和既有反馈，并能在当前编码阶段继续查看定义及执行公开验证。原角色关系、批准数量和修复轮次继续作为各工作流的策略。

G11、G20 各变体、G21、Triple、Dual Contract、G20 + Coder Contract、Red-Green，以及证据与锦标赛工作流均传递完整交接文本。比较候选测试证据时保留完整命令。原有私有字段过滤、候选路径检查、补丁验证和正式评分证据继续生效。Base Team 已完整传递报告，回归测试覆盖了这一行为。模型上下文容量由所配置的模型运行时处理。

完整轨迹采用逐条读取验证。总文件可以超过 16 MiB，同时保留每条记录的内存边界、文件稳定性检查、模型身份、推理配置和全文件摘要。历史结果保留原运行版本，采用交接修复的新运行记录对应源码版本。

## 恢复与结果

评测器在调用公开工作流前绑定真实轨迹目录，异常链经过凭据脱敏后保存。调用未正常返回时，完整模型事件提供已知 token 与回合下界，已经开始但缺少响应的调用继续保留用量未完整的标记。

候选捕获失败时保留自有容器与可信基础代码，回执包含通过安装模块执行的恢复命令。恢复先确认使用的运行版本和原拥有进程已经退出，再沿既有静止提取入口取得候选。已有候选通过 eval-only 入口正式评分，保持原实例、记录及补丁身份，健康会话和已采用结果保持各自尝试。

最终分类为确定通过、确定能力失败、评测失败。可信候选通过正式目标测试即可计入通过，有效完成的尝试或已证明的求解器自身错误可以确定能力失败，外部中断及证据不足保留评测失败。运行和排队是执行状态，已经复核的现成候选计入完成进度一次，继承旧配置的通过与新配置实测通过分别列出。

## 验证

回归测试覆盖公开题面的信息隔离、候选身份与工作区隔离、Conda 激活成功及失败、产物恢复、正式目标解析、请求取消和共享 API 并发。并发测试使用三个真实本机进程取得 45 与 30 个名额，并覆盖 100 个名额的服务，以及取消、记录写入失败、响应关闭和进程退出后的释放。测试使用零模型调用，两仓库分别执行 `ruff check .` 与 `pytest -q`，并将 `OPENCOLLAB_SOURCE_ROOT` 指向配套 OC。

恢复时同时使用回执中的 recovery_environment 与 recovery_argv，确保解释器加载选定运行目录。网关在途名额覆盖连接建立和响应读取，供应商后台的计算并发另外记录。

需要服务器出口代理时，设置 HTTPS_PROXY 并移除 direct-upstream 参数。共享请求限额同样覆盖该路径。

## 运行时与恢复配置

显式 `--context-window` 会传入原生 Agent 与工作流运行时，实际值继续记录在生成身份中。G1.1 在每次角色调用时读取 `OPENCOLLAB_VALIDATION_COUNCIL_ROLE_BUDGET`，支持正整数或 `unbounded`，旧变量 `OPENCOLLAB_G11_ROLE_BUDGET` 保留为别名。`OPENCOLLAB_VALIDATION_COUNCIL_MAX_CODER_ROUNDS` 可以调整修复轮数，默认保持三轮。外部服务恢复时间计入角色外层等待，正常模型调用时限保持原值。

仅评分队列的任务可以通过 `source_base_run_dir` 指定与 `base_run_dir` 不同的原候选目录，省略时保留原同目录行为。该参数由队列管理并传递给单题运行器。只读健康检查及同一运行包的重复传输可以在原有次数和总时间限制内重试传输超时。

候选采集使用控制器拥有的路径视图解释可信忽略规则，Solver 的只读目录与缓存控制文件保持原样。Gitlink 全树扫描使用已有的完整目录扫描额度，与较小的 Gitlink 清单大小限制分开。已证明身份完整、因明确 OC 内部原因停止的候选可以继续评分而无需再次生成，未知错误及服务方错误继续保留原拒绝行为。

pytest 收集失败需要绑定到固定测试中的调用位置及对应候选模块，才能判定为候选失败。JavaScript 缺失模块错误需要证明相应导入由候选新增。离线补判可以使用预期测试补丁摘要，从已保存评测输入中恢复同样的对应关系，缺少这些证据的失败仍归为技术问题。

镜像预装依赖的保存、恢复、移除及候选副本准备使用 `OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT`，默认等待 900 秒。这些操作可能需要传输大体积依赖目录。普通 Docker 控制操作使用 `OPENCOLLAB_DOCKER_TIMEOUT`。
