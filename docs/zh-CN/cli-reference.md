# 命令参考

[English](../cli-reference.md) | **简体中文**

`oc-eval --help`列出当前命令。`oc-eval inspect`用评测侧身份键读取数据集。
`oc-eval run TASKS_JSONL`生成候选并返回提交资格，可以用`--workflow module:function`
显式注入方法。`oc-eval score`把参数转给已有SWE-bench评分包装，参数见`oc-eval score --help`。

使用 OpenCollab 0.8 canonical Duo 时，选择 `--workflow opencollab.builtin_workflows:duo`。
`--agent-profile base` 通过 Base 映射到 `single2`，显式 `--agent-profile single2` 使用同一 profile。
兼容 profile 名称 `single` 和 `default` 也通过 Base 解析。评测器解析并记录调用方传入的 profile。

批量实验、G22配置、补评队列、代理、运行包部署和比较报告使用OpenCollabExp的`oc-exp`。
