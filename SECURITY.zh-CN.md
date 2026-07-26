# 安全策略

[English](SECURITY.md) | **简体中文**

## 报告漏洞

请通过 [GitHub Security Advisories](https://github.com/RISE-X-Lab/OpenCollab-Eval/security/advisories/new) 报告疑似漏洞。报告中请包含最小复现、受影响的修订版本和预期影响。维护者计划在 72 小时内确认收到报告。

## 风险面

OpenCollab-Eval 可以执行模型生成的命令、连接 Docker、连接远程工作节点并运行基准测试。请使用不含无关数据的一次性工作节点。凭据应保存在仓库之外的文件中，每个工作节点仅获得当前运行所需的访问权限。

评测记录可能包含源码补丁、模型记录、任务身份、运行时路径和提供方元数据。请将其存放在源码检出目录之外，并在发布前进行审查。

[SWE Pro-Lite 运维指南](docs/zh-CN/swe-prolite-operations.md) 介绍按运行划分的凭据、工作节点、存储和输出。[评测完整性指南](docs/zh-CN/evaluation-integrity.md) 介绍基准数据、Solver 工作区、候选构建、官方执行和报告之间的信任边界。

## 支持的版本

OpenCollab-Eval 目前处于 1.0 之前的版本阶段。安全修复在 `main` 分支维护。
