# 浏览器生产接入：七批收尾

起点：`7ebb62c2`。每批必须通过直接相关测试、Ruff、生产修改文件类型检查，以及对应的 KoboldCpp + Chromium 实机 gate，记录后提交，才能开始下一批。

约束：同一 ToolSpec、TurnSnapshot、kernel、回执和 ledger；不另建运行时或双写。机械协议由程序处理，语义决策留给模型。修改的生产文件不保留显式 Any。不运行完整 pytest、覆盖率或打包安装测试。

| 批次 | 工作 | 状态 |
| --- | --- | --- |
| 1 | 完整恢复工具元数据；验证恢复后的执行与 catalog/replay 身份 | 完成 |
| 2 | 同时间采集版本排序、旧 handle 与乱序 replay；对齐 CodeMode 授权 fixture | 完成 |
| 3 | 子调用失败的原回执反馈与 Hybrid 修复验收，保留显式错误处理 | 待开始 |
| 4 | 浏览器各工具实际输出的 schema 与 CodeMode 声明 | 待开始 |
| 5 | 上传的项目/附件文件授权与真实文件回执 | 待开始 |
| 6 | 下载启用、持久化存储、摘要与回执 | 待开始 |
| 7 | 完整能力 gate 迁移到生产合同；Chat/Research/Hybrid 集成验收 | 待开始 |

## 批次 1

- 修复 `_restore_spec()` 丢失 namespace/effect/evidence_route/preconditions；使用已有冻结定义，拒绝缺失/错误字段，不从实时注册表补值。
- 新增六组 Chat/Research/Hybrid × text/native roundtrip 测试，核对完整快照、catalog digest、repair digest 与浏览器动作分类。
- 生产 gate 新增可选序列化 roundtrip，强制用恢复后的原合同执行；不替换授权或 executor。这是恢复合同验证，不宣称模拟了操作系统进程崩溃。
- 验证：`test_browser_snapshot_recovery`、`test_frozen_tool_views`、`test_codemode_repair_replay` 共 20 passed；Ruff、provider_receipts.py 的 mypy 通过。
- 实机：`.e2e-artifacts/browser-seven-b1-restored`，KoboldCpp Hybrid 通过；4 份序列化恢复后的快照，完整浏览器链和真实 edit，结果 173 / QUARTZ_851。没有修改 prompt。

## 批次 2

- 根因：Windows 时钟可连续返回相同微秒，正文 hash 被误当成先后关系。新采集在原 ledger 内给同一来源分配严格递增的采集时间：时钟相同或回退时仅推进一微秒；原回执保留该时间，merge/replay 不重算。没有另建版本计数器、账本或时间状态。
- 新测试固定/回退时钟，校验旧 handle 失效，以及三份原回执的全部排列和重复 replay 都收敛到最新正文。
- 五项 CodeMode 写入失败是 fixture 没有 control 授权；只对齐两个构造点，不更改生产权限 guard。
- 验证：来源时钟排序、handle、共享 intake、版本化 edit 和 ledger 共 61 passed；Ruff、ledger.py 的 mypy 通过。
- 实机：`.e2e-artifacts/browser-seven-b2-reread`，KoboldCpp Research 通过；固定采集时钟，操作前后共四次 browser_read，最终引用新正文 173 / QUARTZ_851。没有修改 prompt。
