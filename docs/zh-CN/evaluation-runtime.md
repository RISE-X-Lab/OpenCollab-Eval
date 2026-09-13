# 评测运行时映射

[English](../evaluation-runtime.md) | **简体中文**

OpenCollab-Eval 提供多个执行层级。下表给出不同结果对应的入口。

| 入口 | 输入 | 输出 | 官方判定 |
| --- | --- | --- | --- |
| `oc-eval inspect` | SWE-Batch Pro 数据集与身份密钥 | 匿名任务清单 | 否 |
| `oc-eval run` | 通用评测器任务 JSONL | 候选资格记录 | 否 |
| `oc-eval swe-v1-prolite` | 一个有界远程 Pro-Lite 切片 | 生成报告与官方报告 | 是 |
| `opencollab_eval.commands.swe_eval_run` | Solver 名称与任务索引 | 协调执行的 Pro-Lite 批次 | 是 |
| `oc-eval final-report` | 两份终态事实报告与审计清单 | 经过验证的发布文件集 | 使用已有判定 |

远程生产运行器会同步 OpenCollab 公开软件包与 OpenCollab-Eval 的完整声明源代码树。它会写入运行时清单，验证本地与远程代码树的 SHA-256，探测所选远程 Python 解释器，并在生成开始前再次检查身份。

生成过程从经过验证的任务镜像和 Solver 可见的一次性仓库开始。进程静止后，候选构造使用由控制器持有的 Git 状态。官方评测会把已绑定补丁应用到新工作区，并记录精确的目标执行证据。

候选采集与环境回收分别记录。回收失败时保留已采集的补丁和提取结果，清理错误及最终静止状态仍待确认时，自动提交继续保持禁用。默认清理等待为 60 秒。成功的 diff 命令输出超限时，Eval 会在执行静止后将新 diff 保存到自有临时文件，排除该文件后分块完整传输。原有结果记录大小限制继续生效。失败或残缺的传输无法形成正式提交。

Single 使用当前 OC 内置工具，通过 Bash 运行项目原生测试。需要精确目标证据的研究工作流使用 Eval 自有验证工具，并保留其实际执行证据检查。详见[工作流工具文档](../../src/opencollab_eval/workflows/README.zh-CN.md)。

供操作人员与测试使用的单实例生成器模块仍然可用。

```bash
python -m opencollab_eval.generation.gen_prediction --help
python -m opencollab_eval.generation.gen_prediction_workflow --help
python -m opencollab_eval.generation.gen_prediction_openhands --help
```

打包的 `run_team_batch.sh` 与 `start_team_run.sh` 资源属于旧版门禁。它们会在 Solver 启动前返回技术状态 125，因为其历史挂载设计无法提供当前要求的隔离与可信候选证据。请使用 `oc-eval swe-v1-prolite` 或 Solver 协调器。

可执行命令请参阅 [SWE Pro-Lite 操作指南](swe-prolite-operations.md)，命令选择请参阅 [CLI 参考](cli-reference.md)，结果语义请参阅[评测完整性](evaluation-integrity.md)。
