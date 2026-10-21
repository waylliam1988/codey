# Ghost 实验：Provider Send Scope Authority

- 实验名称：Provider Send Scope Authority
- 状态：INCOMPLETE
- 负责人：Codex
- 开始 / 更新日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1
- 实验 commit：待记录
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

候选 source 不能自行提供请求授权范围。假设当前 kernel 从首项 source 取得
request scope 会漏过跨项目或跨会话候选。先通过真实 builder → kernel → ApiProvider →
loopback HTTP capture 复现，再修改统一 workspace admission 的调用位置及权威 scope。
不恢复已删除的 `codey/ghost/admission.py`，不新增重复准入或兼容 fallback。

## 预先固定的实验配置

- 离线 suite：`tests/test_ghost_provider_send_boundary.py`；合成任务及合成偏好；
  text/native 两种协议，同域/异域、预算、过期、缺字节、禁用、重复 key、trace 故障对照。
- 权威范围来自 kernel execution 的 project/session；候选 scope 只作为待检查字段。
- 实际 ApiProvider 与 loopback HTTP 服务，返回固定 done；非模型行为评分。
- live suite：`scope_authority_20261021`；baseline/candidate 各运行正常 hint on/off 一次（合计四次）结构 smoke，
  不改 frozen evaluator，不作为 language 收益评估。
- 已确认 endpoint：`http://127.0.0.1:5001/v1`；KoboldCpp 1.117.1；
  model：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；context 262144。
- live 预算、超时、采样与具体命令在运行前写入本地 manifest；首次故障停止，不自动重试。

## 本次完成

真实 HTTP capture 已复现 scope 自授权及全局/项目混合候选误拒绝：29 项中 9 failed / 20 passed。
其中跨项目、跨会话以及 trace 故障下跨项目来源均进入了 wire；正常全局+项目候选被误拒绝。
初版 fixture 误用已排除 Ghost 的 coding_writer，修正为 planning_readonly 后才形成上述正式 red 结果；
不修改生产 permission profile 来迁就测试。

## 验证命令和结果

`python -m pytest tests/test_ghost_provider_send_boundary.py -q --tb=short`：修改生产前 9 failed / 20 passed。
完整日志：本地 `artifacts/ghost_memory_routing/scope-authority-red.log`。
原始 HTTP payload/reply、state、完整日志只归档到 ignored `artifacts/`。

## 当前生产边界

- 是否经过真实 provider-send：待验证。
- 是否改变默认 prompt：计划保持有效输入字节一致。
- 支持模型 / 任务 / 配置：离线固定响应与上述单模型 smoke；无跨模型推广。
- 安全收益预期：阻止候选自行授权；trace 仅记录已经完成的 admission。

## 未完成或阻塞

完整历史 native payload 的 expiry/revocation、四开关、并发和生命周期矩阵不在本轮结论内。
World Model 继续 SHADOW 设计，尚无足够证据接入默认 prompt。

## 下一步

保存 red/green 证据；独立提交实验、生产变更和结果文档；运行相关回归及三个类型检查器。
