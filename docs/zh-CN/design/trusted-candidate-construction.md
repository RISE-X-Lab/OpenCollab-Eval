# 可信候选构造

[English](../../design/trusted-candidate-construction.md) | **简体中文**

候选身份由评测器持有的基线和 Solver 最终可见的文件系统共同决定。
Solver 自己拥有的 Git 配置、引用、对象数据库、索引、ignore 变更、
alias、hook 和 replace object 均无权决定最终发布的候选。

## 基线权威

Solver 开始执行前，评测器根据数据集给出的基线版本创建一个匿名基线提交。
Solver 获得一个便于使用的单提交 Git 仓库。另有一个由控制器持有的 Git
目录保留可信基线 tree，并且从不挂载到 Solver 运行环境中。

基线证据记录数据集 commit、匿名 commit、基线 tree、工作区摘要、任务镜像
身份和已同步运行时身份。Solver 工作区中不含 remote、未来 commit、
replace ref、reflog、宿主机未跟踪文件、旧结果和其他任务的产物。

## 候选投影

Solver 退出后，评测器回收由当前任务持有的完整进程集合，并要求工作区达到
稳定静止状态。随后，候选构造根据可信基线 tree 创建新的临时索引。

tracked 文件的修改和删除从最终工作树加入索引。未跟踪路径通过 NUL 分隔的
literal 名称枚举，并在打开任何候选文件前按照可信基线的 ignore 规则完成
分类。Solver 对 `.gitignore` 和 `.gitattributes` 的修改仍可作为普通候选
变更，但这些修改无法在提取过程中隐藏其他路径或改变文件字节。

Git 生成 candidate tree、binary full-index patch、变更路径清单、文件 mode
和 patch SHA-256。共享的 patch-to-tree 投影使用相同的 base tree 验证提取后
过滤结果和每个外部 Solver sidecar。

普通文件、二进制文件、删除、符号链接和可执行位变化使用 Git 原生表示。
硬链接会转换为相互独立的普通文件。FIFO、socket、设备文件、向外部逃逸的
链接、不可读候选文件和无法重建的 Gitlink replacement 会使当前任务失败。

## 忽略内容与残留状态

被 ignore 的缓存、日志和构建输出不会成为候选路径。它们在打开前完成分类，
因此 ignore 路径下的失效链接、FIFO、root 所有条目或 mode `000` 不会造成
错误的技术失败。

可读取的答案残留、未来 Git 状态、宿主机文件、其他任务输出和 tracked
基线漂移都无法被静默接受。可恢复的残留只会从一次性任务副本中删除，净化后
的状态会在 Solver 启动前再次检查。

## official evaluation 绑定

official evaluator 从全新的任务镜像或全新的基线工作区启动。它应用发布的
原始 patch，重新计算用于评测的 patch SHA-256 和 candidate tree，并要求
二者与生成阶段证据一致。

终态记录绑定 task identity、record ID、run identity、dataset base、
anonymous base、base tree、candidate tree、source patch SHA、evaluated
patch SHA、runtime tree、image ID、target plan、execution proof 和 cleanup
result。测试只能证明这一个完成绑定的候选。

## 故障范围

不可读或无法表示的候选、镜像异常和无法静止的任务进程只会让对应任务失败。
暂停批次需要 Docker、存储、队列或已同步运行时等共享基础设施的直接探测
失败。错误消息中的关键词不能决定故障范围。

## 验证

表驱动测试覆盖 tracked 修改与删除、未跟踪文件、ignore 文件、修改后的
ignore 规则、不可读缓存与候选文件、二进制文件、链接、mode、硬链接、
特殊文件、Gitlink、嵌套仓库、Solver Git 变更和后台延迟写入。

确定性 Docker smoke 验证可恢复残留能够被净化、可读答案残留会被阻断、
持续写入者无法发布候选，以及一个任务失败不会停止无关任务。

完整性覆盖台账将每项已实现要求映射到精确的回归测试。参见
[评测完整性](../evaluation-integrity.md)和
[机器可读台账](../../integrity-coverage.json)。
