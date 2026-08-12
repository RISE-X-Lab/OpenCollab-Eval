# 快速入门

[English](../getting-started.md) | **简体中文**

本指南介绍安装和通用候选引擎的首次运行，其中包括数据集验证。官方的 resolved 或 unresolved 判定按照 [SWE Pro-Lite 操作指南](swe-prolite-operations.md)执行。

## 环境要求

核心软件包支持 Python 3.10 至 3.12，并要求使用 OpenCollab 0.4.1 或更新的 0.4 或 0.5 版本。容器任务和官方 SWE-bench 评测需要 Docker。OpenHands 可选依赖仅支持 Python 3.12。

评测器与框架应来自彼此兼容的发行版，或来自已经共同测试过的源码修订。仓库 CI 会构建两者的 wheel，并验证安装后的边界。

## 安装发行版或本地 wheel

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install /path/to/opencollab-0.4.1-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.1.0-py3-none-any.whl
oc-eval --version
oc-eval --help
```

通过软件包的可选依赖安装官方 SWE-bench 支持。

```bash
python -m pip install '/path/to/opencollab_eval-0.1.0-py3-none-any.whl[swebench]'
```

在 Python 3.12 环境中安装 OpenHands 支持。

```bash
python -m pip install '/path/to/opencollab_eval-0.1.0-py3-none-any.whl[openhands]'
```

## 安装源码检出

```bash
git clone https://github.com/RISE-X-Lab/OpenCollab.git
git clone https://github.com/RISE-X-Lab/OpenCollab-Eval.git
cd OpenCollab-Eval
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

可编辑安装的 OpenCollab 源码检出适合开发。发行验证与 CI 验证应使用已经构建的 wheel，防止仓库路径掩盖软件包文件缺失。

## 检查 SWE-Batch Pro 数据集

`oc-eval inspect` 接受大小受限的 JSONL 数据。它会将公开的 Solver 数据与密封的裁判数据分开，并用带密钥的匿名标识符替换每个实例 ID。

在受保护的评测器状态中创建一份原始 32 字节身份密钥。

```bash
install -d -m 700 /sealed/opencollab-eval
python -c 'import os,secrets,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_bytes(32)); os.close(fd)' \
  /sealed/opencollab-eval/identity.key
```

检查数据集。

```bash
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file /sealed/opencollab-eval/identity.key \
  --image-repository registry.example/swe
```

检查时必须提供实例身份、仓库和问题陈述。命令会规范化已有的基准提交、镜像、目标和测试补丁字段，并将其保持为密封状态。这里使用较小的检查契约即可。生产运行器还会验证完整任务规范、基线、镜像和测试计划。当某一行仅包含 `dockerhub_tag` 时，必须提供镜像仓库选项。命令会输出一个 JSON 对象，其中包含行数和匿名任务 ID。

同一实验的重试应沿用同一把密钥。新实验可以使用新密钥。密钥、原始数据集和密封的裁判字段应留在 Solver 工作区与源代码管理之外。

## 运行通用候选引擎

`oc-eval run` 接受评测器任务 JSONL 文件。此格式与 SWE-Batch Pro 数据集格式相互独立。

```json
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
```

支持的行字段如下。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `task_id` | 是 | 运行范围内安全的任务身份 |
| `description` | 是 | 展示给 Solver 的目标 |
| `repo_path` | 否 | 本地源代码仓库 |
| `docker_image` | 否 | 容器环境 |
| `timeout` | 否 | 每项任务的墙上时钟超时秒数 |
| `max_tokens` | 否 | 每项任务的 token 预算 |
| `extras` | 否 | 由评测器持有的结构化扩展 |

每项真实本地任务都应使用绝对 `repo_path`。省略此字段时，评测器会有意使用自身的当前工作目录，这可能导致源码检出本身成为 Solver 的目标。

通过环境变量配置 OpenCollab 模型，并避免让凭据进入 shell 历史记录。

```bash
export OPENCOLLAB_MODEL=example-model
export OPENCOLLAB_PROVIDER=openai
read -r OPENCOLLAB_API_KEY < /run/secrets/model-api-key
export OPENCOLLAB_API_KEY
oc-eval run /data/eval-tasks.jsonl \
  --output /results/candidate-run \
  --concurrency 1 \
  --timeout 600
```

命令会写入 `/results/candidate-run/results.jsonl` 并输出候选资格计数。具备资格的候选仍需接受官方评测，之后才能称为 resolved 或 unresolved。

## 后续步骤

生产远程运行接着阅读 [SWE Pro-Lite 操作指南](swe-prolite-operations.md)。[评测完整性](evaluation-integrity.md)解释结果状态与必要证据，技术失败的处理方法见[故障排查](troubleshooting.md)。
