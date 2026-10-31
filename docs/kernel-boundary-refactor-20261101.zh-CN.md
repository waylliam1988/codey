# 内核边界整理与验证，2026-11-01

范围：沿现有 operation reducer、durable mutation、effect 收据及 completion gate
整理职责。分四批提交，每批先验证再提交；不运行全量 pytest，不引入第二运行时、
新事实库或通用领域生命周期框架。

## 第一批：资源与评审依据

删除 KernelExecutionDeps 中生产内核未读取的 durable、managed_outputs、session_id。
这些资源继续来自原 KernelEffectSink；测试会话身份由测试 helper 显式配置。
生产入口、测试及相关手工实机脚本一并停止传递重复资源。

评审的 schema、工作提示和完成准备统一读取 session.research_review_basis，
删除 completion_context 中的第二入口。保留原 basis/batch digest 和收据校验。
review_basis 仍从已接受报告及精确知识版本重建；本项不承诺跨实现版本的判断单元
完全不变，重建范围与原收据不符时应拒绝，不能改用最新知识解释旧评审。

为已存在的 session、响应、快照、effect 与完成证明补充具体类型，并明确拒绝
缺少评审 basis 或非结构化的 native 响应。

实机使用本机 Gemma 4 12B Q4_K_M、seed=74，隔离任务/状态目录。
第一次 contract_consistency_live 读文件运行因手工入口遗漏 durable boundary callback
失败；已修正入口，失败原件保留。第二次两次生成完成 CodeMode 双文件读取、
观察值绑定 JSON 交付、事实恢复与重复恢复检查，ok=true。
原始资料：artifacts/kernel-refactor-20261101/batch1-read{-02}/。

回归中在原始 cf276731 独立工作树复现了两类既有测试问题：Research 提示 fixture
遗漏当前所需的任务/问题绑定，以及 CodeMode 断言绑定旧引擎的完整错误文案。
fixture 按当前合同补全；错误测试继续要求真实类型错误、原冻结合同和失败事实，
使用两种引擎共有的错误语义，不改生产错误内容。

扩大选集时发现 Zen 包装器未透传内核已冻结的观察文本、历史组数与输出预算；
补齐三个原请求参数，不重新计算参数、不降级请求。原基线也复现此失败。
其余相关旧 fixture 按当前协议修复：覆盖选择替代模型重写报告、明确失败的发布、
严格 native 调用 ID、正式 durable 恢复入口及原始 provider 请求作用域。
生产提示未改，更新的提示 hash 与原基线实际输出一致。

针对性修复选集 36 passed，正式恢复独立选集 8 passed；扩大选集 1378 passed、3 skipped、38 subtests passed（9779 deselected，310.61 秒）。
实机成功记录为 batch1-read-02（2 次生成，约 9.86 秒）。
Mypy、Pyrefly、ty 生产全目录检查通过；Ruff 检查生产目录与本批修改文件。

## 后续批次

第二批集中最终迁移关系、结果解码与控制决定类型，删除空操作。
第三批将研究完成准备收回原 gate，保留字段纠错、原始提案和原接受报告快照。
第四批调整 retry 观察计数的提交时机与旧决定恢复，保留冻结请求的准入预算。
其中旧 retry 的拟更新计数可能尚未进入 checkpoint，不能作为冗余字段直接丢弃。
