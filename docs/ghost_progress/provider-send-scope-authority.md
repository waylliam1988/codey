# Ghost 实验：Provider Send Scope Authority

- 实验名称：Provider Send Scope Authority
- 状态：EXPERIMENT_COMPLETE（仅请求作用域子实验，完整 0.6.1 仍 INCOMPLETE）
- 负责人：Codex
- 开始 / 更新日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1
- 实验 commit：`7d76b812`（首次 red 复现与 live harness）；`d7c6bace`（完整边界、trace 与报告脚本）
- 生产 commit：`3d013af1`（请求作用域、完整身份摘要、单一准入 owner）
- 生产合并结论：PRODUCTION_MERGED（仅上述安全修复）

## 目标和假设

候选 source 不能自行提供请求授权范围。假设当前 kernel 从首项 source 取得
request scope 会漏过跨项目或跨会话候选。先通过真实 builder → kernel → ApiProvider →
loopback HTTP capture 复现，再修改统一 workspace admission 的调用位置及权威 scope。
不恢复已删除的 `codey/ghost/admission.py`，不新增重复准入或兼容 fallback。

## 预先固定的实验配置

- 离线 suite：`tests/test_ghost_provider_send_boundary.py`；合成任务及合成偏好；
  text/native 两种协议，同域/异域、预算、过期、缺字节、禁用、重复 key、trace 故障对照。
- 权威范围来自 entry 的用户请求 project/session，经 transport 显式传入 kernel；候选 scope 只作为待检查字段。
  工具自动分配的 workspace 不是记忆的项目授权，未绑定项目的聊天继续使用 session scope。
- 实际 ApiProvider 与 loopback HTTP 服务，返回固定 done；非模型行为评分。
- live suite：`scope_authority_20261021`；baseline/candidate 各运行正常 hint on/off 一次（合计四次）结构 smoke，
  不改 frozen evaluator，不作为 language 收益评估。入口回归后增加 `candidate-final` attempt，
  保留原 candidate 归档；最终比较使用 baseline / candidate-final 共四次，原 candidate 两次不重复计样本。
- 已确认 endpoint：`http://127.0.0.1:5001/v1`；KoboldCpp 1.117.1；
  model：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；context 262144。
- live 预算、超时、采样与具体命令在运行前写入本地 manifest；首次故障停止，不自动重试。

## 本次完成

真实 HTTP capture 已复现 scope 自授权及全局/项目混合候选误拒绝：29 项中 9 failed / 20 passed。
其中跨项目、跨会话以及 trace 故障下跨项目来源均进入了 wire；正常全局+项目候选被误拒绝。
初版 fixture 误用已排除 Ghost 的 coding_writer，修正为 planning_readonly 后才形成上述正式 red 结果；
不修改生产 permission profile 来迁就测试。

进一步复现长 project/session 的共同前缀被截断为同一 scope（两项 red）；改用完整身份 SHA-256。
首次实现从 execution workspace 推导 scope，在入口回归中误拒绝合法 unbound chat；这是生产实现问题，
没有修改聊天测试预期，改为入口传入 logical request scope 后该回归通过。
统一 admission 现在要求显式 `request_scope`，空 scope 拒绝；trace 只消费 admission 结果。
`codey/ghost/admission.py` 保持删除，没有恢复转发层。

## 验证命令和结果

`python -m pytest tests/test_ghost_provider_send_boundary.py -q --tb=short`：修改生产前 9 failed / 20 passed。
完整日志：本地 `artifacts/ghost_memory_routing/scope-authority-red.log`。
原始 HTTP payload/reply、state、完整日志只归档到 ignored `artifacts/`。

- 最终 targeted battery（含真实 RunTraceStore metadata 与 wire 对齐）：91 passed。
- 最终扩展回归：290 passed、399 subtests passed，包含 architecture、task entry、trace、research、
  unbound chat、HTTP boundary、controls 和 durable scheduler。
- 最终 kernel parity、guidance、typed entry、headless lifecycle、durable kernel：750 passed；
  research iteration 已纳入上面的扩展回归，不将重叠测试重复相加。
- 首次 architecture/entry/trace 综合回归：312 passed、410 subtests passed、1 failed（上述 unbound chat 作用域）；
  修复后相关入口 targeted 通过，没有降低标准。
- `python -m mypy codey --python-version 3.11`：495 source files clean。
- `pyrefly check codey --python-version 3.11`：0 errors；29 suppressed、74 warnings，不声明零警告。
- `ty check codey --python-version 3.11`：All checks passed。
- Ruff：本轮修改文件 clean；`git diff --check` 通过。
- 初次全量运行约 14% 时停止，进度有两处 F：一处按收集顺序映射疑似 scheduler 用例，未取得
  traceback，单项、整文件及扩展回归均未复现；另一处 architecture 行数限制已独立复现并修复，
  仅移除重复局部 import，未抬高上限。该首次运行不是通过结果，完整日志留本地。
  第二次 fail-fast 全量检查已通过 scheduler 所在范围，按用户最新要求在约 4% 停止，转为上述扩展回归。
  不以全量 pytest 作为每轮研究前置；原 scheduler 线索保留，不擅改生产或延长等待阈值。
- live：baseline / candidate-final 的 on/off 均 done，各一次真实请求；对应完整 request SHA-256 均一致。
  baseline 1.461 / 0.642 秒，candidate-final 0.914 / 0.684 秒；每 cell n=1，不作延迟改善结论。
  provider 未报告 token usage，保持 missing，不记为 0。

## 当前生产边界

- 是否经过真实 provider-send：是，离线 loopback HTTP capture 与实际 KoboldCpp 均有证据。
- 是否改变默认 prompt：否，受测有效 on/off 请求完整 payload 摘要均保持一致。
- 支持模型 / 任务 / 配置：离线固定响应与上述单模型 smoke；无跨模型推广。
- 安全收益预期：阻止候选自行授权；trace 仅记录已经完成的 admission。
- 生产合并结论：PRODUCTION_MERGED，仅 `3d013af1` 的 scope 安全修复。

## 报告、复现及回滚

- [离线 / live 报告](../../tests/manual/ghost_memory_routing/reports/scope-authority.md)
- [脱敏 live metrics](../../tests/manual/ghost_memory_routing/reports/scope-authority-live-metrics.json)
- 本地归档：`artifacts/ghost_memory_routing/scope_authority_20261021/`，包含 baseline、candidate、
  candidate-final，旧候选结果未覆盖；最终四次比较不重复计入旧 candidate。
- 三个类型检查命令见上；扩展回归与 parity 的完整命令保存在上述报告。
- 受测 on/off payload 不变，无新增模型调用；token usage 缺失，不以估算或 0 冒充测量。
- live 后只做了重复 import 清理，未修改 prompt/render/codec；candidate-final manifest 的源码摘要
  对应清理前的候选，运行父提交是 `7d76b812`，最终生产提交为 `3d013af1`。
- 回滚：单独 revert `3d013af1`；会恢复已知 scope 缺陷，应先关闭 hint admission。
  保留测试 commit，不恢复旧 scope 格式兼容层。

## 未完成或阻塞

当前改动只证明新 prompt 的请求授权。完整历史 native payload 的 expiry/revocation、
后续 tool-results / protocol repair 的历史上下文、四开关、并发和生命周期矩阵不在本轮结论内。
完整 0.6.1 仍 INCOMPLETE，不能把此次 scope 修复和小样本 smoke 写成整个验收门完成。
World Model 继续 SHADOW 设计，尚无足够证据接入默认 prompt。

## 下一步

完整 history expiry 已被下一实验复现，见 [Provider History Expiry](provider-history-expiry.md)。
下一步找到完整 payload 的统一准入位置，覆盖 native results/repair/resume 和失效恢复，
再继续四开关的 runtime 控制矩阵；scoped control 的旧 fallback 也需要单独验证，不能保留越权降级。
