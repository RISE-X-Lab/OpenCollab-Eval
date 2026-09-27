# OpenCollab-Eval架构

[English](../architecture.md) | **简体中文**

依赖方向为`opencollab_exp -> opencollab_eval -> opencollab`。Pier由实验层调用，是独立后端。

## 评测职责

OCE负责任务数据、资源生命周期、求解调用、候选构造、独立评分和结构化执行证据。
`engine` 包含通用生命周期和 SWE 评分适配，`generation` 包含求解器接入与候选采集。
`contracts`、`benchmarks` 和 `verification` 分别表达数据边界、读取基准与解释执行证据。

## 方法注入

调用方用`module:function`显式提供workflow，`workflow_loader`加载该入口。
方法注册、角色提示、方法预算和友好名称由调用方持有。候选环境分别选择
`shared`、`isolated`或`lean`，原有隐藏测试隔离与候选有效性行为继续由OCE维护。

OpenCollab 0.8 持有 canonical `duo` 工作流，通过 `opencollab.builtin_workflows:duo` 选择。
其角色 profile `base` 解析到 `single2`。评测器使用公开 workflow API，采集采纳后的补丁并执行独立评分。
历史研究方法和对应提示继续保存在 OpenCollabExp。

## 入口

`inspect` 归一化基准输入，`run` 管理任务并返回候选提交资格，`score` 调用已有 SWE-bench 原评分包装。
Pro-Lite direct evaluator继续作为可配置库执行面，保留数据集F2P/P2P、原评分资源和证据要求。

## 运行归属

调用方传入生成launcher路径、workflow引用、方法名称和声明的环境变量键。
OCE保留容器、进程、候选与评分的身份检查。部署凭据、供应方池、批量实验调度和远端同步由
OpenCollabExp持有。OCE的安装和测试独立于实验包。

## 既有兼容实现

Pro-Lite运行器继续通过原兼容命名空间组合评分模块。具体实验preset与命令通过显式输入分离。
