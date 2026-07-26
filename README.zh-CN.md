# OpenCollab-Eval

[English](README.md) | **简体中文**

OpenCollab-Eval 是面向基于 OpenCollab 的软件工程智能体的评测系统。它负责基准规范化、Solver 隔离、可信候选构建、官方测试执行、证据验证、远程批次协调和报告发布。OpenCollab 提供智能体框架及其公开 Python API。

该仓库面向这样一类实验，其中错误的 `resolved` 值比技术失败造成的损害更大。只有声明的目标测试针对 Solver 生成的同一候选补丁完成执行并通过后，任务才会变为 resolved。空计划、测试收集数为零、证明缺失、候选身份漂移和仍在变化的工作区均属于技术失败。

## 评测流程

```text
trusted benchmark row
        |
        v
sealed judge data + anonymous Solver task
        |
        v
disposable Solver workspace
        |
        v
controller-owned candidate projection
        |
        v
fresh official evaluation workspace
        |
        v
target execution evidence + terminal report
```

Solver 会收到公开的问题描述和一个一次性仓库。评测器保留基准提交、测试补丁、目标列表、镜像身份和运行身份。候选提取使用评测器拥有的 Git 状态，官方评测器在运行声明的测试前会再次验证候选补丁的 SHA-256。

## 支持的环境

OpenCollab-Eval 要求 Python 3.10 或更高版本，以及 OpenCollab 0.4.x。SWE-bench 评测要求 Docker 和可选的 `swebench` 依赖项。OpenHands 集成要求 Python 3.12。远程 Pro-Lite 运行还要求一台可通过 SSH 访问的 Linux 工作节点，其中已安装 Python 运行时和所需的任务镜像，并提供按运行划分的可写存储。

通过已构建的发行包安装核心软件包。

```bash
python -m pip install /path/to/opencollab-0.4.x-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.1.0-py3-none-any.whl
```

需要官方评测时，安装 SWE-bench 集成。

```bash
python -m pip install '/path/to/opencollab_eval-0.1.0-py3-none-any.whl[swebench]'
```

使用源码检出时，先安装与之匹配的 OpenCollab 仓库。

```bash
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
```

凭据、数据集、预测、轨迹、补丁、报告、PDF 和运行时日志应存放在源码检出目录之外。

## 命令概览

| 命令 | 用途 | 官方终结判定 |
| --- | --- | --- |
| `oc-eval inspect` | 验证 SWE-Batch Pro 数据集清单并进行匿名化 | 无 |
| `oc-eval run` | 运行通用评测引擎并生成候选资格记录 | 无 |
| `oc-eval swe-v1-prolite` | 生成一个有界远程 Pro-Lite 切片并进行官方评测 | 有 |
| `oc-eval final-report` | 验证并发布由两份已完成事实报告构成的比较结果 | 使用现有判定 |
| `python -m opencollab_eval.commands.swe_eval_run` | 选择 Solver 并协调一个有界 Pro-Lite 批次 | 有 |

`oc-eval run` 会报告候选是否已经生成，以及是否仍具备提交资格。其结果限于候选资格，SWE-bench 的 resolved 结果仍需经过官方评测。`oc-eval swe-v1-prolite` 和多 Solver 协调器均包含官方评测阶段。

使用 `oc-eval --help` 和各子命令的帮助信息查看当前完整选项集。

## 数据集检查

身份密钥是由评测器持有且恰好包含 32 个随机字节的文件。请将其与密封运行状态存放在一起。同一批次重试时应复用该密钥，使公开任务 ID 保持稳定。

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

该命令验证有界 JSONL 输入，并输出匿名化的公开任务清单。它的职责限于检查，不会启动 Solver 或暴露密封的评判字段。

## 通用候选生成

通用引擎每行接受一个 JSON 对象。每行都必须提供 `task_id` 和 `description`，还可以提供 `repo_path`、`docker_image`、`timeout`、`max_tokens` 以及一个 `extras` 对象。

```json
{"task_id":"example-1","description":"Fix the failing calculator test","repo_path":"/work/calculator"}
```

```bash
export OPENCOLLAB_MODEL=example-model
export OPENCOLLAB_PROVIDER=openai
read -r OPENCOLLAB_API_KEY < /run/secrets/model-api-key
export OPENCOLLAB_API_KEY
oc-eval run /data/tasks.jsonl \
  --output /results/candidate-run \
  --concurrency 1
```

提供方配置通过 OpenCollab 公开 API 解析。与在命令行中传递凭据相比，外部密钥存储或受保护的环境文件更合适。结果摘要会统计具备资格和未具备资格的候选，输出目录中会生成 `results.jsonl`。

## 官方 SWE Pro-Lite 评测

生产环境远程入口点会同步当前 OpenCollab 公开运行时和 OpenCollab-Eval 运行时，验证工作节点上的源码树身份，生成候选，等待进程进入静止状态，将候选投影到全新的官方工作区，运行声明的目标测试，并写入 JSON 和 Markdown 报告。

```bash
oc-eval swe-v1-prolite \
  --host evaluator@example-worker \
  --remote-root /srv/opencollab-eval \
  --remote-runtime-repo /srv/opencollab-eval/runtime \
  --base-run-dir /srv/opencollab-eval/runs/example \
  --run-id example-001 \
  --session-prefix example-001 \
  --start-index 1 \
  --limit 1 \
  --model-name kimi-for-coding \
  --llm-model kimi-for-coding \
  --llm-provider openai \
  --context-window 262144 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --workflow-env OPENCOLLAB_THINKING=true \
  --workflow-env 'OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","keep":"all"}}' \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1 \
  --json-output /results/example-001.json \
  --markdown-output /results/example-001.md
```

准备新的工作节点时，先运行 `--dry-run`。试运行会验证配置和计划执行的工作，终结任务结果需要完成实际运行。提供方传输、Solver 选择、远程布局、重试、报告和失败处理的说明见 [Pro-Lite 运维指南](docs/zh-CN/swe-prolite-operations.md)。

## Solver 选择

统一协调器支持 `g11`、`g1.1`、`baseTeam`、`TeamPro`、`openhands` 和 `claude-code`。

```bash
python -m opencollab_eval.commands.swe_eval_run \
  --indices 1 \
  --solver g11 \
  --workers 1 \
  --run-id example-g11-001 \
  --output-dir /results/example-g11-001 \
  --host evaluator@example-worker \
  --remote-root /srv/opencollab-eval \
  --remote-eval-work-root /srv/opencollab-eval/runs \
  --session-prefix example-g11-001 \
  --remote-python /srv/opencollab-eval/venv/bin/python \
  --model-name kimi-k3-g11 \
  --llm-model k3 \
  --llm-provider openai \
  --context-window 1048576 \
  --temperature 1 \
  --top-p 0.95 \
  --max-output-tokens 32768 \
  --remote-proxy-base-url https://api.kimi.com/coding/v1 \
  --remote-api-env-file /srv/opencollab-eval/secrets/kimi.env \
  --image-repository registry.example/swe \
  --max-task-starts 1 \
  --max-eval-attempts 1
```

这个示例选择经过验证的 K3 配置，使用 1048576-token 上下文与 `reasoning_effort=high`。协调器会应用各 Solver 专属的默认设置。OpenHands 和 Claude Code 还要求安装各自的外部运行时。随附的 shell 资源充当这些运行时的适配器，两个产品仍由各自渠道提供。

## 结果与证据

OpenCollab-Eval 区分五种状态。

| 状态 | 含义 |
| --- | --- |
| 候选已生成 | 已提取一个非空补丁 |
| 具备提交资格 | 候选和生命周期证据已通过生成检查 |
| 评测完成 | 官方评测已生成绑定报告 |
| Resolved 或 unresolved | 针对绑定候选执行声明的目标，并生成语义判定 |
| 技术失败 | 系统无法建立可信的语义判定 |

每项可发布结果都会绑定任务身份、运行身份、记录 ID、完整补丁 SHA-256、运行时身份、全新评测工作区、目标计划、命令证据、进程清理情况和官方报告。只有执行证据证明预期目标已经运行时，失败的目标才会判为 unresolved。导入失败、收集失败、不受支持的计划、日志缺失和身份不匹配均属于技术失败。

Python 目标使用由评测器持有的控制器和结构化的逐节点 Pytest 事件。Go 目标使用 `go test -json` 证据。JavaScript 目标使用各框架专用、由解析器支持的证据。不受支持的目标语法会直接判为技术失败。

## 文档

[文档索引](docs/zh-CN/README.md) 按任务引导读者。最重要的指南包括 [入门指南](docs/zh-CN/getting-started.md)、[任务格式参考](docs/zh-CN/task-formats.md)、[Pro-Lite 运维指南](docs/zh-CN/swe-prolite-operations.md)、[架构指南](docs/zh-CN/architecture.md)、[评测完整性指南](docs/zh-CN/evaluation-integrity.md)、[CLI 参考](docs/zh-CN/cli-reference.md) 和 [故障排除指南](docs/zh-CN/troubleshooting.md)。

[最终报告契约](docs/zh-CN/final-report.md) 介绍受证据约束的 100 项任务对比结果发布。[MIGRATION.zh-CN.md](MIGRATION.zh-CN.md) 规定仓库归属和 OpenCollab 公开 API 边界。[CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md) 介绍开发和审查要求。[SECURITY.zh-CN.md](SECURITY.zh-CN.md) 说明私密漏洞报告流程。

OpenCollab-Eval 依据 [木兰宽松许可证第 2 版](LICENSE) 发行。依赖项和署名详情记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中。

## 开发验证

```bash
ruff check .
pytest -q
scripts/verify_wheel_contract.sh \
  /path/to/opencollab-0.4.x-py3-none-any.whl \
  /path/to/opencollab_eval-0.1.0-py3-none-any.whl
scripts/run_deterministic_swe_e2e.sh --output /tmp/oce-e2e --runs 1
```

wheel 契约会隔离安装两个发行包，并针对打包产物运行 Eval 测试套件。确定性 E2E 使用本地伪 OpenAI 兼容服务、临时 SSH、真实 `rsync`、Docker、可信候选提取和官方目标执行，整个过程无需提供方凭据。

更改评测行为前，请阅读 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。
