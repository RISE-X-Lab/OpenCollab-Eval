# 为 OpenCollab-Eval 贡献代码

[English](CONTRIBUTING.md) | **简体中文**

感谢你帮助改进 OpenCollab-Eval。该仓库负责基准适配、Solver 隔离、候选构建、官方评测、证据、远程执行和报告。

## 开发环境配置

请使用 Python 3.10 或更高版本。OpenHands 集成要求 Python 3.12。

```bash
python -m pip install -e ../OpenCollab
python -m pip install -e '.[dev,swebench]'
ruff check .
pytest -q
```

OpenCollab 检出版本必须与 `pyproject.toml` 中声明的兼容版本一致。wheel 契约测试会构建两个仓库，并在不使用可编辑导入的情况下验证安装后的边界。

发布前请构建两个 wheel 并验证打包后的契约。

```bash
python -m pip install build
wheel_root="$(mktemp -d)"
python -m build --wheel --outdir "$wheel_root/opencollab" ../OpenCollab
python -m build --wheel --outdir "$wheel_root/eval" .
scripts/verify_wheel_contract.sh \
  "$wheel_root"/opencollab/opencollab-0.4*.whl \
  "$wheel_root"/eval/opencollab_eval-0.1.0*.whl
```

确定性 SWE E2E 要求安装 Docker、`sshd`、`ssh`、`ssh-keygen` 和 `rsync`。它使用本地伪模型服务，无需提供方凭据。

```bash
scripts/run_deterministic_swe_e2e.sh --output /tmp/oce-e2e --runs 1
```

## 架构

生产代码仅可使用已有文档说明的 OpenCollab 公开 API。边界测试会拒绝从 OpenCollab 适配器、应用服务、启动模块、领域模块或已弃用的 harness 代码导入内容。

只有声明的目标测试在官方 harness 中完成执行并通过后，评测才能将任务报告为 resolved。空测试计划、测试收集数为零、证据缺失、补丁身份漂移和未进入静止状态的工作区均属于技术失败。

新增行为需要配套测试。Python 模块不得超过 800 行，新文件不得超过 500 KB。生成的报告、模型记录、预测、补丁、数据集、容器导出文件、PDF、凭据和本地运行时路径均不得提交。

凡是会影响命令、默认值、运行时拓扑、证据或结果语义的更改，都应同步更新操作员指南、CLI 参考、架构说明和完整性文档。文档示例必须使用外部输出路径和占位符，避免使用本地基础设施信息。

公开代码、注释、测试和规范文档使用英文。简体中文文档镜像仅放在根目录 `*.zh-CN.md` 文件、`docs/zh-CN/` 目录，以及 `src/opencollab_eval/` 下紧邻包 README 的 `README.zh-CN.md` 文件中。每份镜像都应与其英文源文档同步，并保留相同的代码块和技术标识。提交摘要、拉取请求标题、拉取请求描述和审查回复使用中文，同时保留英文 Conventional Commit 类型。

## 贡献许可

提交贡献即表示你有权合法提供该贡献。该贡献依据与仓库许可证一致的 [木兰宽松许可证第 2 版](LICENSE) 获得许可。

修改密钥检测器或继承自可信基准的发现，需要在合并前接受专项安全审查。

安全报告应遵循 [SECURITY.zh-CN.md](SECURITY.zh-CN.md) 中说明的私密流程。
