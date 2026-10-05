# 快速入门

[English](../getting-started.md) | **简体中文**

先确定要运行什么。自己的仓库任务使用通用任务 JSONL，输出候选补丁。SWE 基准评测需要有序数据集、任务镜像和正式测试。下面先介绍本地候选生成，再给出完整基准配置的入口。

## 安装软件包

当前评测器版本为 0.9.1，依赖 OpenCollab 0.9.x（`opencollab>=0.9,<0.10`）。Linux 和 macOS 支持 Python 3.10 至 3.12。下面使用 Python 3.12，把环境放在两个源码目录旁边。

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

评测器版本命令应输出 0.9.1。使用已构建的 wheel 时，激活环境后安装两个兼容软件包。

```bash
python -m pip install /path/to/opencollab-0.9.1-py3-none-any.whl
python -m pip install /path/to/opencollab_eval-0.9.1-py3-none-any.whl
```

正式 SWE-bench 评测需要对应的可选依赖，以及 Linux 工作机上的 Docker。OpenHands 支持使用单独的可选依赖，需要 Python 3.12。在源码安装目录中，安装本次运行所需的依赖。

```bash
python -m pip install -e './OpenCollab-Eval[swebench]'
# For OpenHands, use Python 3.12.
python -m pip install -e './OpenCollab-Eval[openhands]'
```

## 选择输入

| 要做的事 | 输入 | 命令 |
| --- | --- | --- |
| 修改自己的 Git 仓库 | 通用任务 JSONL | `oc-eval run` |
| 检查 SWE-Batch Pro 数据集 | 基准 JSONL 和私有身份密钥 | `oc-eval inspect` |
| 用 Duo 生成补丁并正式评测 | 有序基准 JSONL、镜像和 Duo JSON 配置 | `oc-eval duo` |
| 运行远程基准任务子集 | 已准备的工作机与基准设置 | `oc-eval swe-v1-prolite` |

[任务格式](task-formats.md)介绍两种 JSONL。任务输入、凭据和结果保存在源码目录及求解器将修改的仓库之外。

## 运行本地仓库任务

准备一个已有的 Git 仓库，里面放好模型需要的源码与测试。将 `/work/calculator` 换成仓库绝对路径，并换成自己的任务描述。省略 `repo_path` 会选择评测器当前的工作目录，因此应显式设置。

```bash
export EVAL_ROOT="$HOME/oc-evaluation/eval-data"
umask 077
mkdir -p "$EVAL_ROOT"
cat > "$EVAL_ROOT/tasks.jsonl" <<'TASKS'
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
TASKS
```

每个非空行代表一项任务，`task_id` 与 `description` 为必填字段。示例给这项任务设置了 600 秒超时和 100000 token 预算。可选的 `docker_image` 与 `extras` 字段见[任务格式](task-formats.md)。

设置当前 OpenCollab 安装支持的模型与 provider，并在提示处输入 API key。

```bash
export OPENCOLLAB_MODEL=your-model
export OPENCOLLAB_PROVIDER=openai
export OPENCOLLAB_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key > "))')"
```

使用自定义 provider 接口时，运行前设置 API base URL，将示例地址换成自己的接口地址。

```bash
export OPENCOLLAB_BASE_URL='https://api.example.com/v1'
```

首次执行时，每次运行一项任务。

```bash
oc-eval run "$EVAL_ROOT/tasks.jsonl" \
  --output "$EVAL_ROOT/candidate-run" \
  --concurrency 1 --timeout 600
```

## 阅读候选结果

输出目录中包含 `results.jsonl`。读取每项任务的补丁与提交资格字段。

```bash
python - "$EVAL_ROOT/candidate-run/results.jsonl" <<'PY_RESULTS'
import json
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.strip():
        row = json.loads(line)
        print(row["task_id"], row["patch_produced"], row["submission_eligible"])
PY_RESULTS
```

| 字段 | 含义 |
| --- | --- |
| `task_id` | 输入任务标识 |
| `patch_produced` | 已提取到候选补丁 |
| `submission_eligible` | 候选满足提交要求 |
| `patch` | 提取的补丁文本 |
| `error` | 记录的执行或提取错误 |

命令打印的摘要包含 `tasks`、`eligible_patches` 和 `ineligible`。满足提交要求的补丁可以继续评测。官方成功判定来自对匹配候选实际执行的基准目标测试。[评测完整性](evaluation-integrity.md)介绍相关要求，[故障排查](troubleshooting.md)介绍执行与提取失败的处理方法。

## 检查基准数据集

使用已有的有序基准 JSONL。原始 ID 和裁判字段保存在评测器存储中，检查命令输出匿名任务 ID 和行数。

为实验创建一次原始 32 字节身份密钥。

```bash
mkdir -p "$EVAL_ROOT/secrets"
chmod 700 "$EVAL_ROOT/secrets"
python - "$EVAL_ROOT/secrets/identity.key" <<'PY_KEY'
import os
import secrets
import sys
with open(sys.argv[1], "xb") as stream:
    os.chmod(sys.argv[1], 0o600)
    stream.write(secrets.token_bytes(32))
PY_KEY
```

再次检查同一实验时沿用这把密钥。上面的排他创建方式会保留已有密钥。检查时使用与镜像匹配的仓库，示例仓库对应 SWE-bench Pro v1 镜像。

```bash
oc-eval inspect /path/to/instances.jsonl \
  --identity-key-file "$EVAL_ROOT/secrets/identity.key" \
  --image-repository jefzda/sweap-images
```

每行必须包含实例标识、仓库和问题陈述。镜像字段只有标签、缺少仓库前缀时，需要镜像仓库选项。检查命令读取并规范化数据集，正式运行器随后准备任务镜像并执行完整的基准测试计划。密钥、原始数据集和密封裁判字段保存在求解器工作区及源代码管理之外。

## 运行正式基准测试

使用 Duo 时，按[在一台 Linux 工作机上运行 Duo](swe-prolite-operations.md#在一台-linux-工作机上运行-duo)准备数据集和镜像，启动模型 relay，创建下面使用的配置文件。准备完成后运行一题。

```bash
oc-eval duo --config "$EVAL_ROOT/duo.json" \
  --indices 1 --workers 1 --run-id duo-smoke-001
```

打开生成的任务报告及其链接的正式报告，再扩大到批量任务。[操作指南](swe-prolite-operations.md)介绍远程任务子集、其他求解器、批量输出和已有候选恢复。[服务器评测指南](evaluation-suite.md)介绍服务器运行。[CLI 参考](cli-reference.md)列出命令参数。
