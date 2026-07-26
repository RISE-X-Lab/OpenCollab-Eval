# 最终 SWE 对比报告

[English](../final-report.md) | **简体中文**

`oc-eval final-report` 根据两次已经完成、各含 100 项任务的 SWE-bench Pro-Lite 运行发布一份对比结果。每种输出格式都由同一个经过验证的 JSON 模型渲染。只有 PDF 与所有源文件均以原子方式发布，且发布清单状态为 `final` 后，命令才会成功退出。

## 命令

```bash
oc-eval final-report \
  --method-a-report /sealed/runs/g11/final_report.json \
  --method-a-audit-manifest /sealed/runs/g11/clean_run_manifest.json \
  --method-a-name G1.1 \
  --method-b-report /sealed/runs/openhands/final_report.json \
  --method-b-audit-manifest /sealed/runs/openhands/clean_run_manifest.json \
  --method-b-name OpenHands \
  --dataset-file /sealed/datasets/swe-batch-pro-lite.jsonl \
  --meeting-date 2026-07-15 \
  --author "Evaluation Team" \
  --labels-json /sealed/report_labels.json \
  --narrative-json /sealed/report_notes.json \
  --output-dir /sealed/publication
```

输出目录会收到一组具有相同日期派生前缀的文件，包括作为 JSON 保存的经过验证的对比模型、Markdown、TeX、编译后的 PDF 和发布清单。首次验证失败或 LaTeX 构建失败会记录清单状态 `failed` 并返回非零退出码。一旦完整的 `final` 发布已经存在，后续失败尝试不会改变五份已发布文件及其哈希。发布过程会串行化同一前缀的写入者，预检每个目标，备份此前的输出文件集，再次检查每项已发布哈希，并在任何替换或清单写入失败时恢复完整的此前文件集。

## 事实报告契约

必需的 `--dataset-file` 是可信且大小受限的 Pro-Lite JSON 或 JSONL 源文件。其字节必须匹配已经记录的 Pro-Lite 1-100 快照 SHA-256 `a1d473cb415ec0050eee023f373cdf71183436351216240f3f88c820a200c078`。加载任一方法报告前，命令会读取其有序 100 任务清单，以及每项任务的 `FAIL_TO_PASS` 与 `PASS_TO_PASS` 目标。两份事实报告都必须将索引 1 至 100 映射到同一组任务身份。两份审计清单都必须声明精确计算出的数据集 SHA-256。每份官方报告都必须保留评测使用的不可变 `sha256:...` Docker 镜像身份，其两份声明目标列表必须与可信数据集行精确匹配，此后命令证据才能建立终态判定。数据集路径与哈希会记录在对比模型和发布清单中。

计划从可信数据集行独立推导。适配器、覆盖模式、目标批次、命令和证据绑定必须等于推导出的计划。Python 目标通过评测器持有的控制器运行。该控制器将可信 Pytest 协议与候选解释器分开，并输出结构化的逐节点证据。Go 目标使用 `go test -json`，JavaScript 目标使用特定于框架且由解析器支持的证据。存储的退出码、控制台文本或未绑定的插件事件都无法让一行结果满足发布要求。

每份事实报告使用 schema `opencollab.swe_eval_layer_final_report.v1`。它必须包含索引 1 至 100 的精确有序任务清单。每一行都必须已经完成生成与官方评测，具有布尔判定、零项待处理或技术状态、稳定记录身份、完整补丁 SHA-256、官方报告路径和直接执行证据。声明的聚合计数必须等于从各行推导出的值。任务缺失、重复、重排、含糊或技术失败都会停止发布。

## 干净运行审计清单契约

每份审计清单使用 schema `opencollab.swe_clean_run_manifest.v1`，并通过 `source_report_sha256` 绑定到精确的事实报告。它记录方法名称、覆盖干净轨迹、候选身份、网络隔离和直接执行的完整任务清单、具有可执行证据的精确 resolved 任务集、OpenCollab 与 OpenCollab-Eval 提交、数据集 SHA-256，以及一份或多份结构化证据文件。参与对比的两种方法必须使用相同的运行时与数据集身份。

```json
{
  "schema": "opencollab.swe_clean_run_manifest.v1",
  "method": "G1.1",
  "source_report_sha256": "<64 lowercase hex characters>",
  "expected_indices": [1, 2, 3],
  "clean_trajectory_indices": [1, 2, 3],
  "candidate_identity_indices": [1, 2, 3],
  "network_isolation_indices": [1, 2, 3],
  "direct_execution_indices": [1, 2, 3],
  "resolved_execution_indices": [2],
  "runtime": {
    "opencollab_commit": "<40 or 64 lowercase hex characters>",
    "opencollab_eval_commit": "<40 or 64 lowercase hex characters>",
    "dataset_sha256": "<64 lowercase hex characters>"
  },
  "evidence_files": [
    {
      "path": "evidence/trajectory_audit.json",
      "sha256": "<64 lowercase hex characters>"
    }
  ]
}
```

上面的缩略数组用于说明字段含义。一份可以发布的 Pro-Lite 清单会在每个完整清单字段中包含索引 1 至 100。

列出的每份证据文件都使用 schema `opencollab.swe_clean_run_evidence.v1`。其中的方法、源报告 SHA-256 与运行时对象必须等于清单中的值。其任务行合在一起必须无重叠地覆盖精确的有序 1 至 100 清单。每一行都绑定事实报告中的任务 ID、记录 ID 与补丁 SHA-256，并将 `trajectory_clean`、`candidate_identity_verified`、`network_isolated` 和 `direct_execution_proven` 设为 `true`。它还通过路径与 SHA-256 精确绑定四项底层产物，包括官方评测报告、轨迹证据、候选身份凭据与网络隔离证据。相对产物路径从结构化证据文件所在位置解析。每项产物都必须是非空、大小受限且字节匹配声明哈希的普通文件。支持产物仅验证哈希，不保留其内容。检查任务记录时最多保留一份大小受限的官方报告内容，因此内存用量不会随所有引用产物的总大小增长。每份文件的覆盖数组必须等于实际任务行，resolved 执行索引必须等于从这些绑定事实推导出的 resolved 子集。

官方报告产物路径必须与事实报告中任务的 `report_path` 精确一致。产物必须包含一条结构化 `opencollab.prolite_direct_eval.v2` 记录，并对应同一任务、记录 ID、补丁 SHA-256 和判定。命令会根据目标测试计划、命令证据、退出状态、清理证据和容器结果独立重新计算可执行证据。匹配的审计布尔值无法替代缺失、发生改变或内部不完整的官方报告。由评测器持有的审计文档负责解释轨迹、身份和网络产物，final-report 命令则将这份解释固定到已经评审的精确原始产物字节。

```json
{
  "schema": "opencollab.swe_clean_run_evidence.v1",
  "method": "G1.1",
  "source_report_sha256": "<same fact report SHA-256>",
  "runtime": {
    "opencollab_commit": "<same commit>",
    "opencollab_eval_commit": "<same commit>",
    "dataset_sha256": "<same dataset SHA-256>"
  },
  "covered_indices": [1],
  "clean_trajectory_indices": [1],
  "candidate_identity_indices": [1],
  "network_isolation_indices": [1],
  "direct_execution_indices": [1],
  "resolved_execution_indices": [],
  "tasks": [
    {
      "index": 1,
      "task": "<fact report task ID>",
      "record_id": "<fact report record ID>",
      "patch_sha256": "<fact report patch SHA-256>",
      "trajectory_clean": true,
      "candidate_identity_verified": true,
      "network_isolated": true,
      "direct_execution_proven": true,
      "artifacts": {
        "official_report": {
          "path": "/sealed/eval/task-1/report.json",
          "sha256": "<official report SHA-256>"
        },
        "trajectory": {
          "path": "raw/task-1/trajectory.jsonl",
          "sha256": "<trajectory evidence SHA-256>"
        },
        "candidate_identity": {
          "path": "raw/task-1/candidate.json",
          "sha256": "<candidate evidence SHA-256>"
        },
        "network_isolation": {
          "path": "raw/task-1/network.json",
          "sha256": "<network evidence SHA-256>"
        }
      }
    }
  ]
}
```

## 可选呈现输入

标签文档使用 schema `opencollab.swe_final_report_labels.v1`，用于覆盖已知呈现标签。resolved 计数、对比计数、终态覆盖范围和证据声明直接由经过验证的模型生成，无法由标签提供。叙述文档使用 schema `opencollab.swe_final_report_narrative.v1`。它可以添加概览段落，以及带任务索引和证据引用的任务注释。叙述文本无法更改任何判定、计数、对比集合、运行时身份或证据哈希。叙述中的证据引用必须指向已经由两份审计清单之一验证的文件。所有外部文本都会分别针对 Markdown 与 TeX 进行转义。

## 发布要求与输出

默认渲染器要求 `PATH` 中存在 `xelatex`。可以使用 `--latex-engine` 选择其他兼容引擎。输出目录必须位于源码检出之外，并且允许评测器写入。

命令会发布一组使用同一前缀的 `.json`、`.md`、`.tex`、`.pdf` 和 `.manifest.json` 文件。发布清单会记录每份最终文件的名称、SHA-256、字节大小、经过验证的运行时身份、数据集身份与最终状态。

退出状态 0 表示完整发布文件集已经通过验证、完成渲染与哈希计算，并已提交。输入验证、证据验证、文件安全、锁定、渲染或发布替换失败会返回非零状态，并在目标位置可安全写入时记录失败清单。
