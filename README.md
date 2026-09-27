<a id="english"></a>

# OpenCollab-Eval

<p align="center"><strong>English</strong> · <a href="#simplified-chinese">简体中文</a></p>

<p align="center"><a href="#supported-environment">Supported environment</a></p>

Reusable evaluation mechanisms for OpenCollab-based and external solvers.
OpenCollab-Eval owns public task input, execution lifecycle, trusted candidate
collection, benchmark scoring adapters, and structured result evidence.

Experiment methods, G-series presets, campaigns, provider proxies, SSH deployment,
monitoring, and report presentation are maintained in
[OpenCollabExp](https://github.com/KaiEureka/OpenCollabExp).
The evaluator has no dependency on that experiment package. Pier remains a
separate evaluator selected by the outer experiment layer.

## Supported environment

Use Python 3.10 or later with OpenCollab 0.7.x. Keep the virtual environment and
runtime output outside source checkouts. This branch contains the proposed
experiment-layer interface changes; the main branch is unchanged.

```bash
python -m pip install -e /path/to/OpenCollab
python -m pip install -e '/path/to/OpenCollab-Eval[dev,swebench]'
oc-eval --help
```

## Public commands

`oc-eval inspect` reads a benchmark dataset and its public task identities.
`oc-eval run` generates candidates and reports submission eligibility.
`oc-eval score` delegates an existing prediction to the installed SWE-bench
scoring wrapper. Candidate submission eligibility and official task success
are distinct result fields.

```bash
oc-eval run /path/to/tasks.jsonl \
  --model MODEL --provider PROVIDER --output /path/to/results
oc-eval run /path/to/tasks.jsonl \
  --model MODEL --provider PROVIDER --output /path/to/results \
  --workflow my_solver.workflow:solve
oc-eval score --help
```

The workflow is an explicit callable supplied by the caller. Benchmark tests,
solver isolation, candidate quiescence, patch collection, and scoring evidence
retain their existing checks. [Task formats](docs/task-formats.md),
[CLI reference](docs/cli-reference.md), and [architecture](docs/architecture.md)
describe the evaluator interfaces. [Migration](MIGRATION.md) lists moved entries.

## Development

Run `ruff check .` and `pytest -q` in an installed environment. Direct caches to
an external location through `RUFF_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, and
pytest's `-o cache_dir=PATH`. Generated candidates, datasets, model transcripts,
and reports belong to the experiment workspace.

<a id="simplified-chinese"></a>

# OpenCollab-Eval

<p align="center"><a href="#english">English</a> · <strong>简体中文</strong></p>

<p align="center"><a href="#支持的环境">支持的环境</a></p>

OCE提供公开任务输入、执行生命周期、可信候选收集、基准评分适配和结构化结果。
具体实验方法、G系列配置、批量实验、代理、SSH、监控和报告展示由
[OpenCollabExp](https://github.com/KaiEureka/OpenCollabExp)管理。
OCE独立于实验包，Pier由外层实验部署选择，继续作为独立评测运行器。

## 支持的环境

使用Python3.10及以上和OpenCollab0.7.x。虚拟环境、缓存与运行产物放在源码目录外。
这个分支保存实验层解耦所需接口，主分支保持原状。

```bash
python -m pip install -e /path/to/OpenCollab
python -m pip install -e '/path/to/OpenCollab-Eval[dev,swebench]'
oc-eval --help
```

## 公开命令

`oc-eval inspect`读取基准数据与公开任务身份。`oc-eval run`生成候选并返回提交资格。
`oc-eval score`通过已有包装调用SWE-bench评分。候选可提交与正式测试通过分别记录。

```bash
oc-eval run /path/to/tasks.jsonl \
  --model MODEL --provider PROVIDER --output /path/to/results
oc-eval run /path/to/tasks.jsonl \
  --model MODEL --provider PROVIDER --output /path/to/results \
  --workflow my_solver.workflow:solve
oc-eval score --help
```

调用方显式传入workflow函数。原有测试隔离、候选静止、补丁采集和评分证据检查保持。
接口说明见[任务格式](docs/zh-CN/task-formats.md)、[命令参考](docs/zh-CN/cli-reference.md)、
[架构](docs/zh-CN/architecture.md)和[迁移说明](MIGRATION.zh-CN.md)。

## 开发

安装后执行`ruff check .`与`pytest -q`。通过`RUFF_CACHE_DIR`、`PYTHONPYCACHEPREFIX`
和pytest的`-o cache_dir=PATH`将缓存放到外部目录。候选、数据、轨迹与报告由实验工作目录保存。
