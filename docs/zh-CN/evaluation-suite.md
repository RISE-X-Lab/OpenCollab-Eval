# 可复用评测方案

[English](../evaluation-suite.md) | **简体中文**

英文原文见 [evaluation-suite.md](../evaluation-suite.md)。这份开发包将现用评测修复整合到 OC 0.5 维护接口，OC 与 OCE 两个开发分支需要配对安装。OC 提供候选工作区隔离、显式无限预算、继承配置的推理行为、请求生命周期记录和公开模型及快照接口。OCE 负责题面交付、公开依赖准备、可信候选提取、正式测试和结果解释。

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
python -m opencollab_eval.commands.swe_g11_parallel_runner   --runner-transport local --host localhost   --indices "$TASK_INDICES" --max-workers 8 --min-workers 8   --workflow base-team-single-pass-v1   --run-id "$RUN_ID" --session-prefix "$RUN_ID"   --output-dir "$EVAL_ROOT/$RUN_ID/controller"   --remote-base "$EVAL_ROOT/$RUN_ID/tasks"   --remote-runtime-repo "$EVAL_ROOT/runtime"   --remote-python "$(command -v python)" --remote-root "$BENCHMARK_ROOT"   --image-repository "$IMAGE_REPOSITORY"   --remote-proxy-base-url "$PROVIDER_BASE_URL"   --local-proxy-base-url "$PROVIDER_BASE_URL" --proxy-env-file "$PROVIDER_ENV"   --model-name "$MODEL" --llm-model "$MODEL" --llm-provider openai   --context-window "$CONTEXT_WINDOW" --max-output-tokens 65536   --temperature 1 --top-p 0.95 --budget 1000000000000 --max-steps 1000000000000   --swe-timeout 1000000000000 --task-wall-timeout 1000000000300   --total-timeout 1000001000000 --llm-timeout 46800   --eval-container-bind-timeout 120 --max-task-starts 1   --workflow-env OPENCOLLAB_UNBOUNDED_LIMITS=true   --workflow-env OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1   --workflow-env OPENCOLLAB_THINKING=true   --workflow-env OPENCOLLAB_REASONING_EFFORT=max   --workflow-env OPENCOLLAB_WIRE_PROTOCOL=responses   --workflow-env OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT=43200
```

显式无限开关会把整题及角色的 token 与步骤上限解析为 `None`，较大的命令行数值用于兼容数字参数入口。单次输出和上下文大小仍是模型参数。无进展时间观察完整模型回复和工具动作，触及运维时间边界的等待会保留原始原因，外部原因或归因未明时进入评测中断复核。

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

## 恢复与结果

评测器在调用公开工作流前绑定真实轨迹目录，异常链经过凭据脱敏后保存。调用未正常返回时，完整模型事件提供已知 token 与回合下界，已经开始但缺少响应的调用继续保留用量未完整的标记。

候选捕获失败时保留自有容器与可信基础代码，回执包含通过安装模块执行的恢复命令。恢复先确认使用的运行版本和原拥有进程已经退出，再沿既有静止提取入口取得候选。已有候选通过 eval-only 入口正式评分，保持原实例、记录及补丁身份，健康会话和已采用结果保持各自尝试。

最终分类为确定通过、确定能力失败、评测失败。可信候选通过正式目标测试即可计入通过，有效完成的尝试或已证明的求解器自身错误可以确定能力失败，外部中断及证据不足保留评测失败。运行和排队是执行状态，已经复核的现成候选计入完成进度一次，继承旧配置的通过与新配置实测通过分别列出。

## 验证

回归测试覆盖公开题面的信息隔离、候选身份与工作区隔离、Conda 激活成功及失败、产物恢复、正式目标解析、请求取消和共享 API 并发。并发测试使用三个真实本机进程取得 45 与 30 个名额，并覆盖 100 个名额的服务，以及取消、记录写入失败、响应关闭和进程退出后的释放。测试使用零模型调用，两仓库分别执行 `ruff check .` 与 `pytest -q`，并将 `OPENCOLLAB_SOURCE_ROOT` 指向配套 OC。

恢复时同时使用回执中的 recovery_environment 与 recovery_argv，确保解释器加载选定运行目录。网关在途名额覆盖连接建立和响应读取，供应商后台的计算并发另外记录。

需要服务器出口代理时，设置 HTTPS_PROXY 并移除 direct-upstream 参数。共享请求限额同样覆盖该路径。
