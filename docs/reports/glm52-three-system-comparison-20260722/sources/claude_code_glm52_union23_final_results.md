# Claude Code 与 GLM-5.2 二十三题最终评测报告

生成日期 2026 年 7 月 22 日

署名 拱垲

## 最终结论

Claude Code 2.1.175 驱动 GLM-5.2 完成了 G1.1 或 OpenHands 曾经解决的 23 道 SWE-bench Pro-Lite 题。23 道题均形成非空原始补丁，其中第 43 题的改动全部属于测试文件，过滤后没有可提交的评测补丁。其余 22 道题进入 official eval，最终得到 15 道 resolved、8 道 unresolved 和 0 道技术失败。

全体目标题的 resolved 比例为 15 比 23，约 65.2%。按“是否通过”的最终能力结果统计，15 道通过，8 道未通过，没有待定题。第 35、78 题经过额外的新鲜 official eval 后再次复现由候选实现位置错误造成的导入失败，第 43 题经过候选重新发布验证后确认过滤结果为空。这三道题均已从技术失败转为 unresolved。报告仍保留零目标执行和空评测补丁的事实，防止这些失败被误记为 resolved。

resolved 题号为 7、11、21、32、33、34、37、53、55、60、69、70、74、92、93。

unresolved 题号为 35、36、43、46、47、50、75、78。

技术失败题号为空。

## 评测范围与配置

题目集合由冻结清单 `claude-code-glm52-union23-20260718` 给出。筛选规则是 G1.1 或 OpenHands 在同一百题集合中拥有可信 resolved 终态。题号为 7、11、21、32、33、34、35、36、37、43、46、47、50、53、55、60、69、70、74、75、78、92、93。

模型调用使用 Claude Code 2.1.175 和 GLM-5.2，经 Anthropic 兼容接口访问 `api.cherr.cc`。上下文窗口为 200000，`temperature` 为 1，`top_p` 为 1，最大输出 token 为 32000。候选补丁由可信基线构造，模型写入的测试文件在 official eval 前从评测补丁中剔除。resolved 只由相同候选补丁在新鲜 official 工作副本中的全部声明目标证明，数据集声明保留测试时还必须取得相应执行证明。

## 与 G1.1 和 OpenHands 的详细对比

G1.1 在这 23 题中做出 19 题，OpenHands 做出 16 题。二者共同做出 12 题，只有 G1.1 做出的有 7 题，只有 OpenHands 做出的有 4 题。Claude Code 与 GLM-5.2 最终做出 15 题。

| 来源分组 | 题数 | 题号 | Claude resolved | Claude unresolved |
| --- | --- | --- | --- | --- |
| G1.1 与 OpenHands 共同做出 | 12 | 7、11、21、32、34、53、60、69、74、75、92、93 | 11 | 1 |
| 只有 G1.1 做出 | 7 | 35、36、37、43、46、47、50 | 1 | 6 |
| 只有 OpenHands 做出 | 4 | 33、55、70、78 | 3 | 1 |

Claude 在双方共同做出的 12 题中解决 11 题，比例约 91.7%。在 OpenHands 独有的 4 题中解决 3 题，比例为 75.0%。在 G1.1 独有的 7 题中解决 1 题，比例约 14.3%。

换一个观察方向，G1.1 做出的 19 题中有 12 题也被 Claude 解决，另有 7 题 unresolved。OpenHands 做出的 16 题中有 14 题也被 Claude 解决，另有 2 题 unresolved。Claude 的 15 道 resolved 中有 11 道属于双方交集，3 道属于 OpenHands 独有集合，1 道属于 G1.1 独有集合。

结果显示 Claude Code 与 GLM-5.2 在双方都能解决的题上表现稳定，在 OpenHands 独有集合上的重合度也较高。G1.1 独有集合形成明显难点，失败主要集中在模块边界判断、公开接口位置、回归兼容和候选发布证据。

## 逐题最终结果

| 题号 | 先前做出者 | 候选补丁 | 最终结果 | 关键证据 |
| --- | --- | --- | --- | --- |
| 7 | G1.1 与 OpenHands | 已生成 | resolved | Go F2P 与 P2P 精确目标全部通过 |
| 11 | G1.1 与 OpenHands | 已生成 | resolved | 候选身份修复后 F2P 与 P2P 全部通过 |
| 21 | G1.1 与 OpenHands | 已生成 | resolved | Pytest 目标与保留测试全部通过 |
| 32 | G1.1 与 OpenHands | 已生成 | resolved | 固定镜像运行依赖恢复后目标与保留测试全部通过 |
| 33 | OpenHands | 已生成 | resolved | F2P 与 P2P 全部通过 |
| 34 | G1.1 与 OpenHands | 已生成 | resolved | F2P 与 P2P 全部通过 |
| 35 | G1.1 | 已生成 | unresolved | 同一候选经过两次 official eval，均因实现位于错误模块而在公开测试导入阶段收集到零个目标 |
| 36 | G1.1 | 已生成 | unresolved | SQL Server 错误分类没有满足目标断言 |
| 37 | G1.1 | 已生成 | resolved | 全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 43 | G1.1 | 已生成 | unresolved | 原始补丁只修改测试文件，重新发布后过滤得到空评测补丁，没有可接受的生产代码候选 |
| 46 | G1.1 | 已生成 | unresolved | 候选缺少目标测试要求的端点会话结构 |
| 47 | G1.1 | 已生成 | unresolved | 候选缺少 `validateCredentials` 边界处理 |
| 50 | G1.1 | 已生成 | unresolved | 新行为目标通过，文件夹扩展属性的保留测试发生回归 |
| 53 | G1.1 与 OpenHands | 已生成 | resolved | 全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 55 | OpenHands | 已生成 | resolved | 全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 60 | G1.1 与 OpenHands | 已生成 | resolved | 全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 69 | G1.1 与 OpenHands | 已生成 | resolved | 全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 70 | OpenHands | 已生成 | resolved | 结构化重判后全部声明的 F2P 目标执行并通过，数据集未声明 P2P 目标 |
| 74 | G1.1 与 OpenHands | 已生成 | resolved | 候选投影修复后 Go F2P 与 P2P 全部通过 |
| 75 | G1.1 与 OpenHands | 已生成 | unresolved | `TestCore` 真实执行 44 项，43 项通过，1 项失败 |
| 78 | OpenHands | 已生成 | unresolved | 同一候选经过两次 official eval，均因公开接口缺少 `get_non_isbn_asin` 而在导入阶段终止 |
| 92 | G1.1 与 OpenHands | 已生成 | resolved | Go JSON 证明确认 `TestGetEvaluationRolloutsCached` 执行并通过 |
| 93 | G1.1 与 OpenHands | 已生成 | resolved | `test_get_doc` 与 `test_process_facet` 精确执行并通过 |

## 三道转为 unresolved 的追加重评

第 35 题拥有可信非空候选，补丁 SHA-256 为 `0059d026dbcca27aa8fd488ef58ae677d8a297c0eeea709fdcfa407bd5a6105d`。模型把选区工具写入 `wysiwyg_composer/hooks/utils.ts`，公开 F2P 测试导入 `wysiwyg_composer/utils/selection.ts`。追加评测在新容器中成功应用同一候选和测试补丁，149 个 P2P 目标通过，两个 F2P 目标仍在导入阶段报模块缺失。两次 official eval 得到相同结果，已经排除偶发容器准备故障。候选没有满足公开接口要求，最终结果记为 unresolved。

第 43 题形成了 2883 字节原始补丁，补丁 SHA-256 为 `6b951a00e3f93ff0d389a5b26c5d79b22213980e6dd60fa349ff0f0fa2c76157`，候选树为 `ac0829164a8cc0ffa1e31d789708459d7c9e4921`。补丁只修改 `config/config_test.go` 并新增 `config/syslogconf_test.go`，没有生产代码改动。追加重评把原始候选记录按原哈希复制到新目录，当前评测器再次得到 `skipped_no_generation_patch`。模型写入的测试文件在 official eval 前被剔除，过滤后的评测补丁为空。该候选没有提供生产修复，最终结果记为 unresolved。

第 78 题拥有可信非空候选，原始补丁 SHA-256 为 `5366505adf6098814817940ba4f859260b6e29e68cc0400cd4fc39c0b7cfd344`，过滤测试文件后的评测补丁 SHA-256 为 `2ee5827d650c96195055f78f3289aea59fe774145e526786dfb2bfeead633853`。模型修改了 `openlibrary/core/models.py`，公开目标要求 `openlibrary.catalog.utils.get_non_isbn_asin`。追加评测先验证原目录与新鲜重评目录中的候选记录 SHA-256 完全一致，再启动新容器。Pytest 仍在导入 `openlibrary/tests/catalog/test_utils.py` 时找不到该函数，18 个声明目标均未完成收集。两次 official eval 得到相同结果，候选没有实现公开目标要求的接口，最终结果记为 unresolved。

## 近期四题的终态核验

第 75 题只启动一次，模型轨迹由 GLM-5.2 完成，使用 4,242,125 token，费用为 3.577445 美元。原始补丁 SHA-256 为 `9f10f5edc3e296945af8c6bc287b8c4fc6252655a52cf922d89ce83a25285542`。评测器剔除模型新增测试后真实执行 `TestCore`，44 项中有 1 项失败，终态为 unresolved。

第 78 题的模型生成只启动一次，使用 6,543,808 token，费用为 5.208616 美元。候选身份、运行时身份和补丁 SHA 均完成绑定，随后对同一候选执行了两次 official eval。两次运行都在公开目标导入阶段终止，最终结果为 unresolved。追加重评没有调用模型，也没有增加 token 用量。

第 92 题只启动一次，使用 1,949,811 token，费用为 1.537559 美元。原始补丁 SHA-256 为 `e32c1dbaf973a3b63d433f36578a9283f117f594865f376ebc4937f37959c959`。模型新增测试被剔除后，Go JSON 事件证明 `TestGetEvaluationRolloutsCached` 真实运行并通过，终态为 resolved。

第 93 题只启动一次，使用 6,501,937 token，费用为 4.982241 美元。原始补丁 SHA-256 为 `a8955a7a2fc5950b0173ab84bcafbcb62145f5e340fccbb7c21a38df39d4ec57`，候选树为 `c4c476466041b0215491ddab6836d2b52283e283`。模型新增测试被剔除后，official eval 精确执行 `test_get_doc` 与 `test_process_facet`，两项均通过，终态为 resolved。

第 92、93 题采用二并发运行，两题各启动一次，没有生成重试和评测重试。两题合计使用 8,451,748 token，费用为 6.5198 美元。运行结束后，远端相关进程、生成容器、official 容器、专属网络和本地 LaunchAgent 均已退出或被证明不存在。

## 结果可信度与评测层修复

每个 resolved 终态都要求任务身份、记录身份、运行身份、原始候选补丁 SHA、评测补丁 SHA 和新鲜 official 工作副本互相对应。全部声明的 F2P 命令必须与计划匹配并拥有目标执行证明，数据集声明 P2P 时还必须满足同一要求，容器清理也必须完成。模型写入的测试文件不会进入最终评测补丁。

评测过程中修复了两个通用问题。提交 `d89c5ce` 让模型转发在上游断线后自动恢复。提交 `8207bfc` 让候选构造忽略不可写的 `.pytest_cache` 与 `.hypothesis` 运行产物，同时在候选构造失败时保留真实模型用量。相关源码测试得到 2020 项通过和 13 项跳过，Ruff、Shell 语法检查、差异格式检查和双 wheel 隔离契约均通过。独立监督 Reviewer 对代码与最后两题证据给出 APPROVE。

这些修复直接作用于通用候选构造和用量证据，没有加入仓库名、题号或文件路径特例。最终统计只保留每道题最新的可信终态，已经恢复的中间技术错误没有进入结果总表。

## 证据索引

逐题详细证据保存在实验运行归档中。这个发布快照记录结果、记录身份与证据文件名，不复制体积较大的容器日志和运行目录。第 7、11、21 题对应 `summary.json`、`task11_final_fact_report.json` 与 `task21_direct_eval_c4e4df0.json`。第 32、33、34、35、36、37 题对应 `task32c.json`、`task33_eval.json`、`task34_eval.json`、`task35.json`、`task36_eval_v2.json` 与 `task37_recovery_eval_f3d955c.json`。

第 43、46、47、50 题对应 `task43_recheck_fresh.json`、`task46_eval.json`、`task47_eval.json` 与 `summary.json`。第 53、55、60、69、70、74、75、78、92、93 题对应 `task_53_report.json`、`task55_eval_only_d0e27a1.json`、`task_60_report.json`、`task_69_report.json`、`task70_eval.json`、`task74.json`、`task_75_report.json`、`task78_recheck_fresh.json`、`task_92_report.json` 与 `task_93_report.json`。第 35、43、78 题的追加重评保留了零目标、空补丁和导入失败证据。
