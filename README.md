<a id="english"></a>

<h1 align="center">OpenCollab-Eval</h1>

<p align="center"><strong>Generate software patches and evaluate them with official benchmark tests</strong></p>

<p align="center"><strong>English</strong> · <a href="#simplified-chinese">简体中文</a></p>

<p align="center"><a href="#supported-environment">Install</a> · <a href="#choose-a-command">Choose a command</a> · <a href="#run-duo">Run Duo</a></p>

OpenCollab-Eval prepares tasks, runs [OpenCollab](https://github.com/RISE-X-Lab/OpenCollab) solvers, and evaluates their patches in isolated official test workspaces. The current version is **0.9.1**, paired with **OpenCollab >=0.9,<0.10**.

<a id="supported-environment"></a>

## Supported environment

Use Python 3.10 through 3.12 on Linux or macOS. The source installation below uses Python 3.12. Official SWE evaluation uses a Linux worker with Docker. OpenHands support requires Python 3.12.

```bash
mkdir -p "$HOME/oc-evaluation"
cd "$HOME/oc-evaluation"
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./OpenCollab
python -m pip install -e ./OpenCollab-Eval
oc-eval --version
oc-eval --help
```

Keep task inputs, credentials, and generated results outside the source checkouts. [Getting started](docs/getting-started.md) also covers wheel installation and optional dependencies.

<a id="choose-a-command"></a>

## Choose a command

Prepare the input shown in the middle column, then follow the linked guide.

| Goal | Input | Command and guide |
| --- | --- | --- |
| Generate a patch for your own repository | Generic task JSONL with an absolute repository path | `oc-eval run` · [Getting started](docs/getting-started.md) |
| Check a benchmark dataset | Benchmark JSONL and an evaluator-owned identity key | `oc-eval inspect` · [Task formats](docs/task-formats.md) |
| Run Duo with Single2 and official scoring | Ordered benchmark JSONL, task images, and a model configuration | `oc-eval duo` · [Duo on a Linux worker](docs/swe-prolite-operations.md#run-duo-on-one-linux-worker) |
| Run another workflow or a bounded remote slice | Prepared Linux worker, dataset, and model settings | `oc-eval swe-v1-prolite` · [Operations](docs/swe-prolite-operations.md) |
| Score saved candidates again | A queue plan bound to the existing candidates | `oc-eval rejudge-queue` · [Recovery](docs/swe-prolite-operations.md#resume-and-evaluation-only-maintenance) |
| Prepare an installed runtime for a server | Installed compatible OC and OCE packages | `oc-eval package-runtime` · [Evaluation suite](docs/evaluation-suite.md) |
| Build a final comparison | Two completed fact reports and their evidence | `oc-eval final-report` · [Final reports](docs/final-report.md) |

The generic task file and benchmark dataset have different fields. Choose their matching command using the [task format reference](docs/task-formats.md). [Operations](docs/swe-prolite-operations.md) covers model configuration, and [CLI reference](docs/cli-reference.md) describes other solver entrypoints.

## Run a local repository task

Replace `/work/calculator` with the absolute path of your Git repository and describe the change you want. Create the input file outside that repository.

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
umask 077
mkdir -p "$EVAL_ROOT"
cat > "$EVAL_ROOT/tasks.jsonl" <<'TASKS'
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
TASKS
```

Save the file as `$HOME/oc-evaluation/eval-data/tasks.jsonl`. Set your model and provider, then enter the API key at the prompt. A custom endpoint can be selected with `OPENCOLLAB_BASE_URL`.

```bash
export OPENCOLLAB_MODEL=your-model
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key > "))')"
oc-eval run "$EVAL_ROOT/tasks.jsonl" \
  --output "$EVAL_ROOT/candidate-run" \
  --concurrency 1 --timeout 600
```

Read `results.jsonl` in the selected output directory. `patch_produced` tells you whether a patch was captured, and `submission_eligible` tells you whether it can proceed to evaluation. Official pass or failure requires the benchmark's target tests to execute against that patch. [Getting started](docs/getting-started.md) explains the result fields and dataset inspection.

<a id="run-duo"></a>

## Run Duo and read the official result

Duo uses two coders and an adjudicator to select a candidate, then Eval runs its official tests. The default Base agent profile resolves to Single2. Follow [Duo on one Linux worker](docs/swe-prolite-operations.md#run-duo-on-one-linux-worker) to prepare the ordered dataset, images, model relay, and configuration. After that preparation, run one task.

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 1 --workers 1 --run-id duo-smoke-001
```

Read `task_1_report.json` under the configuration directory's `results/duo-smoke-001` directory. It links the candidate to its official test report. A passing `resolved` result requires executed official targets and matching evidence. Missing evidence, failed environment preparation, and invalid candidate capture produce `technical_failure`. [Operations](docs/swe-prolite-operations.md) explains these outcomes, batch execution, and recovery. `oc-eval g22` is a compatibility alias for Duo.

## Find the next guide

The [documentation index](docs/README.md) groups guides by task. Use [CLI reference](docs/cli-reference.md) for options, [Troubleshooting](docs/troubleshooting.md) for failures, and [Evaluation integrity](docs/evaluation-integrity.md) for scoring rules. [Evaluation suite](docs/evaluation-suite.md) covers server operation and provider capacity.

## Development

From the installation directory, install the development and SWE-bench extras, then run the repository checks.

```bash
python -m pip install -e './OpenCollab-Eval[dev,swebench]'
cd OpenCollab-Eval
ruff check .
pytest -q
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers development and review. [CHANGELOG.md](CHANGELOG.md) records releases. The license is [MulanPSL-2.0](LICENSE), with dependency notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

<a id="simplified-chinese"></a>

<h1 align="center">OpenCollab-Eval</h1>

<p align="center"><strong>生成代码补丁，并用基准的正式测试评测补丁</strong></p>

<p align="center"><a href="#english">English</a> · <strong>简体中文</strong></p>

<p align="center"><a href="#支持的环境">安装</a> · <a href="#选择命令">选择命令</a> · <a href="#运行-duo">运行 Duo</a></p>

OpenCollab-Eval 负责准备任务、调用 [OpenCollab](https://github.com/RISE-X-Lab/OpenCollab) 求解器，并在独立的正式测试工作区中评测补丁。当前版本为 **0.9.1**，配套依赖为 **OpenCollab >=0.9,<0.10**。

<a id="支持的环境"></a>

## 支持的环境

Linux 和 macOS 支持 Python 3.10 至 3.12。下面使用 Python 3.12 安装源码。正式 SWE 评测需要一台装有 Docker 的 Linux 工作机，OpenHands 支持需要 Python 3.12。

```bash
mkdir -p "$HOME/oc-evaluation"
cd "$HOME/oc-evaluation"
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./OpenCollab
python -m pip install -e ./OpenCollab-Eval
oc-eval --version
oc-eval --help
```

任务输入、凭据和生成结果保存在源码目录之外。[快速入门](docs/zh-CN/getting-started.md)还介绍了 wheel 安装和可选依赖。

<a id="选择命令"></a>

## 选择命令

先准备中间一列的输入，再按对应指南运行。

| 目标 | 输入 | 命令与指南 |
| --- | --- | --- |
| 给自己的仓库生成补丁 | 含仓库绝对路径的通用任务 JSONL | `oc-eval run` · [快速入门](docs/zh-CN/getting-started.md) |
| 检查基准数据集 | 基准 JSONL 和评测器持有的身份密钥 | `oc-eval inspect` · [任务格式](docs/zh-CN/task-formats.md) |
| 用 Single2 运行 Duo 并正式评分 | 有序的基准 JSONL、任务镜像和模型配置 | `oc-eval duo` · [在 Linux 工作机上运行 Duo](docs/zh-CN/swe-prolite-operations.md#在一台-linux-工作机上运行-duo) |
| 运行其他工作流或远程任务子集 | 已准备好的 Linux 工作机、数据集和模型设置 | `oc-eval swe-v1-prolite` · [操作指南](docs/zh-CN/swe-prolite-operations.md) |
| 重新评分已保存的候选 | 绑定已有候选的队列计划 | `oc-eval rejudge-queue` · [恢复运行](docs/zh-CN/swe-prolite-operations.md#恢复运行与仅评测维护) |
| 为服务器准备安装后的运行代码 | 已安装的兼容 OC 与 OCE 软件包 | `oc-eval package-runtime` · [服务器评测指南](docs/zh-CN/evaluation-suite.md) |
| 生成最终对比报告 | 两份完成的事实报告及其证据 | `oc-eval final-report` · [最终报告](docs/zh-CN/final-report.md) |

通用任务文件和基准数据集使用不同字段，按[任务格式](docs/zh-CN/task-formats.md)选择匹配的命令。[操作指南](docs/zh-CN/swe-prolite-operations.md)介绍模型配置，[CLI 参考](docs/zh-CN/cli-reference.md)介绍其他求解器入口。

## 运行本地仓库任务

将 `/work/calculator` 换成自己 Git 仓库的绝对路径，并写清想完成的修改。下面在目标仓库之外创建输入文件。

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
umask 077
mkdir -p "$EVAL_ROOT"
cat > "$EVAL_ROOT/tasks.jsonl" <<'TASKS'
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
TASKS
```

文件保存为 `$HOME/oc-evaluation/eval-data/tasks.jsonl`。设置模型与 provider 后，在提示处输入 API key。自定义接口地址通过 `OPENCOLLAB_BASE_URL` 设置。

```bash
export OPENCOLLAB_MODEL=your-model
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key > "))')"
oc-eval run "$EVAL_ROOT/tasks.jsonl" \
  --output "$EVAL_ROOT/candidate-run" \
  --concurrency 1 --timeout 600
```

打开所选输出目录中的 `results.jsonl`。`patch_produced` 表示是否提取到了补丁，`submission_eligible` 表示候选是否可以继续评测。官方通过或失败判定来自对这份补丁实际执行的基准目标测试。[快速入门](docs/zh-CN/getting-started.md)介绍结果字段和数据集检查。

<a id="运行-duo"></a>

## 运行 Duo 并阅读正式结果

Duo 由两个编码角色和一个裁决角色选择候选，再交给 Eval 执行正式测试。默认 Base 角色配置会解析为 Single2。按照[在一台 Linux 工作机上运行 Duo](docs/zh-CN/swe-prolite-operations.md#在一台-linux-工作机上运行-duo)准备有序数据集、镜像、模型 relay 和配置文件，然后运行一题。

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 1 --workers 1 --run-id duo-smoke-001
```

配置文件所在目录的 `results/duo-smoke-001` 目录中会生成 `task_1_report.json`，其中包含候选与正式测试报告的对应关系。`resolved` 通过判定需要正式目标测试实际执行，并有匹配证据。证据缺失、环境准备失败、候选提取无效会记为 `technical_failure`。[操作指南](docs/zh-CN/swe-prolite-operations.md)介绍结果含义、批量执行和恢复步骤。`oc-eval g22` 是 Duo 的兼容命令别名。

## 查找下一份指南

[文档索引](docs/zh-CN/README.md)按用途组织指南。参数见 [CLI 参考](docs/zh-CN/cli-reference.md)，运行失败时看[故障排查](docs/zh-CN/troubleshooting.md)，评分规则见[评测完整性](docs/zh-CN/evaluation-integrity.md)。[服务器评测指南](docs/zh-CN/evaluation-suite.md)介绍服务器运行和 provider 请求容量。

## 开发

在安装目录中安装开发与 SWE-bench 可选依赖，再运行仓库检查。

```bash
python -m pip install -e './OpenCollab-Eval[dev,swebench]'
cd OpenCollab-Eval
ruff check .
pytest -q
```

[CONTRIBUTING.md](CONTRIBUTING.zh-CN.md)介绍开发与评审方式。[CHANGELOG.md](CHANGELOG.md)记录版本变化。许可证为 [MulanPSL-2.0](LICENSE)，依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
