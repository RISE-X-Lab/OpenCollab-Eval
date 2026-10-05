# 任务与数据集格式

[English](../task-formats.md) | **简体中文**

OpenCollab-Eval 在不同信任边界上接受两种 JSONL 契约。`oc-eval inspect` 读取同时包含公开字段和密封裁判字段的基准数据集。`oc-eval run` 读取已经为 Solver 执行准备好的通用评测器任务。每条命令都有自己的数据结构。

## SWE-Batch Pro 数据集

每个非空行都是一个 JSON 对象。适配器接受下表中的规范名称，以及 `opencollab_eval.benchmarks.swe_batch_pro` 中定义的一小组旧版别名。

| 字段 | 可见性 | 含义 |
| --- | --- | --- |
| `instance_id` | 密封 | 原始基准身份 |
| `repo` | 公开 | 仓库名称 |
| `problem_statement` | 公开 | 主要问题描述 |
| `requirements` | 公开 | 必须满足的行为与验收条件 |
| `interface` | 公开 | 新增或变化的接口 |
| `base_commit` | 密封 | 可信源代码修订 |
| `docker_image` | 密封 | 完整评测镜像名称 |
| `dockerhub_tag` | 密封 | 与 `--image-repository` 配合使用的镜像标签 |
| `FAIL_TO_PASS` | 密封 | 必须从失败变为通过的目标 |
| `PASS_TO_PASS` | 密封 | 回归目标 |
| `test_patch` | 密封 | 由评测器持有的测试改动 |
| `solver_public_hints` | 公开 | 明确批准的提示 |
| `solver_public_metadata` | 公开 | 明确批准的类 JSON 元数据 |

如果公开提示或元数据值中包含实例 ID、基准提交、镜像、目标、测试补丁或其他密封值，规范化器将拒绝该值。公开任务 ID 是通过带密钥的 HMAC 推导出的标识符，例如 `solver-0123456789abcdef0123456789abcdef`。

适配器始终将 `problem_statement`、`requirements` 和 `interface` 组合成
Solver 的完整任务规格。任何生成适配器都不得静默丢弃后两个字段。

`oc-eval inspect` 最多读取 64 MiB，并要求使用一份原始 32 字节密钥。

在评测存储中创建一次原始密钥。需要同一组公开任务身份时复用该密钥。把下方数据集路径替换为实际的有序 V1 文件。独占创建会在重复准备时保留已有密钥。

```bash
export INSPECT_ROOT="$HOME/oc-evaluation/inspect-001"
umask 077
mkdir -p "$INSPECT_ROOT"
python - "$INSPECT_ROOT/identity.key" <<'PY_KEY'
import secrets
import sys
from pathlib import Path
path = Path(sys.argv[1])
with path.open("xb") as stream:
    stream.write(secrets.token_bytes(32))
path.chmod(0o600)
PY_KEY
oc-eval inspect /data/swe-batch-pro.jsonl \
  --identity-key-file "$INSPECT_ROOT/identity.key" \
  --image-repository jefzda/sweap-images
```

数据集和身份密钥保存在源码仓库外的评测存储中。命令向标准输出写入 `count` 与 `public_task_ids`。适配器先验证公开提示与密封裁判字段的隔离，再将公开提示交给 Solver。

## 通用评测器任务 JSONL

`oc-eval run` 接受每个非空行一个评测器任务对象。

```jsonl
{"task_id":"calculator-1","description":"Fix calculator.add and run its tests","repo_path":"/work/calculator","timeout":600,"max_tokens":100000}
```

`task_id` 与 `description` 是必填字符串。`repo_path` 选择本地仓库。`docker_image` 选择容器环境。`timeout` 与 `max_tokens` 覆盖命令默认值。`extras` 必须是 JSON 对象，其中的 `test_patch` 值在存在时必须是字符串。

真实本地任务应使用绝对 `repo_path`。省略此字段会有意选择评测器进程的工作目录。

读取器最多接受 64 MiB 的文件、每行 8 MiB 和 10000 行任务。文件必须是普通文件。结果将写入所选输出目录下的 `results.jsonl`。

此命令报告候选生成情况与提交资格。密封的 SWE 裁判契约和官方 resolved 判定由后续评测命令处理。

把仓库路径改为已存在的 Git 仓库。保存 JSONL 时，每个对象占一个物理行。以下命令在 checkout 外写入该文件并生成候选。执行前通过 `OPENCOLLAB_API_KEY` 与 `OPENCOLLAB_BASE_URL` 设置凭据和入口，并把 `OPENCOLLAB_MODEL` 设为实际模型。

```bash
export TASK_REPOSITORY=/absolute/path/to/calculator
export TASK_OUTPUT="$HOME/oc-evaluation/local-task-001"
mkdir -p "$TASK_OUTPUT"
python - "$TASK_OUTPUT/tasks.jsonl" <<'PY_TASK'
import json
import os
import sys
from pathlib import Path
row = {
    "task_id": "calculator-1",
    "description": "Fix calculator.add and run its tests",
    "repo_path": str(Path(os.environ["TASK_REPOSITORY"]).resolve()),
    "timeout": 600,
    "max_tokens": 100000,
}
Path(sys.argv[1]).write_text(json.dumps(row) + "\n", encoding="utf-8")
PY_TASK
oc-eval run "$TASK_OUTPUT/tasks.jsonl" \
  --provider openai --model "$OPENCOLLAB_MODEL" \
  --output "$TASK_OUTPUT/results" --concurrency 1
```

候选记录在 `results/results.jsonl` 中。生成阶段记录的 Solver 测试描述其实际工作。正式基准结果由基准 runner 和绑定候选的目标执行给出。

## 生成的记录

生成记录描述评测输出，并带有自己的身份要求。

| 记录 | 身份要求 |
| --- | --- |
| Generation metrics | 任务、运行、模型、工作流、记录 ID、运行时树、源补丁 SHA |
| Candidate projection | 可信基准树、候选树、变更路径、模式、补丁 SHA |
| Official report | 实例、记录、已评测补丁 SHA、镜像 ID、目标计划、执行证据 |
| Fact report | 有序任务清单、生成状态、官方状态、语义判定 |
| Clean-run manifest | 事实报告 SHA、运行时身份、证据文件哈希 |
| Final publication manifest | 数据集身份与每项已发布输出的哈希 |

修复失败运行时，应修正源环境或重复某个获得明确授权的阶段。这样可以保留原始记录，同时生成绑定到同一获准身份的新证据。
