# 外部正式评分适配

[English](../scoring-adapters.md) | **简体中文**

Pro-Lite 运行器支持由外部登记文件配置的评测适配。通过
`--scoring-adapter-registry` 传入 worker 上的绝对路径。并行运行器接受
同一选项，并传给准备检查及每个题目进程。`swe_eval_run` 将这一选项转交
并行运行器。补评队列可通过 `runner_args` 提供该选项。

```bash
oc-eval swe-v1-prolite \
  --runner-transport local \
  --scoring-adapter-registry "$WORKER_SCORING_REGISTRY" \
  ...
```

`OPENCOLLAB_EVAL_SCORING_ADAPTER_REGISTRY` 提供 host 配置默认值，显式
CLI 选项优先。控制器通过 worker 配置向本地与 SSH 两种传输发送路径。
路径指向 worker 上已安装的文件。源码打包会包含通用 loader 与评分入口。
具体 benchmark 登记文件和适配模块保存在评测操作人员维护的外部目录。

已安装 runtime 也可以包含 `engine/scoring_adapters/registry.json`。
省略显式路径和 host 默认值时，loader 使用这个相对路径。未配置的公共
runtime 保留原评分内容。题目循环遇到显式配置的不可读文件、格式错误的
登记文件或非法的匹配适配时，会在生成和评分开始前抛出错误。

题目循环在候选隔离前建立评分副本。生成接收原始数据行，评分依据副本
准备计划与评测身份。直接调用的 `eval_for_task` 和 `eval_for_task_once`
使用同一个准备函数，串接这些入口时适配实际执行一次。内存中的 dict
子类独立保存回执，数据字段保持原结构。

登记文件沿用 `opencollab.scoring_adapter_registry.v1` 格式。

```json
{
  "schema": "opencollab.scoring_adapter_registry.v1",
  "entries": [
    {
      "instance_id": "example-instance",
      "adapter_id": "example-public-interface-v1",
      "module": "example_adapter.py",
      "allowed_changed_fields": ["test_patch"]
    }
  ]
}
```

每个模块导出 `INSTANCE_IDS`、`ADAPTER_ID` 和
`adapt(instance) -> (adapted_instance, receipt)`。loader 向模块传入深拷贝，
并校验模块身份、精确允许的变化字段及原实例身份。回执保留既有隔离声明
`solver_input_unchanged=true` 与 `gold_production_code_added=false`，
`adapter_id` 和 `changed_fields` 与登记条目相匹配。评测操作人员提供
适配的功能验证，并将证据保存在外部模块旁边。

题目报告包含 `scoring_adapter`。已执行的正式报告和摘要包含同一回执，
正式输入目录保存 `scoring_adapter_receipt.json`。回执记录登记路径、
模块路径、适配 ID、声明的变化及模块提供的来源信息。
既有 `original_instance_sha256` 和 `adapted_instance_sha256` 回执字段
沿用原序列化格式描述两份实例。

直接调用已安装运行器时，在传给 `swe_v1_remote_runner.install_into` 的
配置中提供 `scoring_adapter_registry`。单独准备评分副本时，调用
`swe_eval_scoring_adapters.prepare_scoring_row(row, registry_path)`。

历史 metadata 可能同时包含规范 `instance_id` 与匿名 `task_id`。
匿名身份采用 `solver-<32 lowercase hexadecimal characters>` 名称空间。
共用任务解析器采用规范实例身份，并保留原字典。其他冲突的 benchmark
别名继续拒绝配对，record 与 patch 身份校验保持既有行为。
