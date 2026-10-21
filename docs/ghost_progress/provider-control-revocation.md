# Ghost 实验：Provider Control Revocation

- 实验名称：Provider Control Revocation
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 / 0.6.2 即时生效
- 实验 commit：待记录
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

验证首轮有效发送 hint 后，真实 GhostInboxStore 的 session `admit_hints` 被关闭，
第二次完整 HTTP payload 是否仍携带它。假设 task entry 只取一次控制快照，
后续发送不能发现撤回；旧 history 也不能由仅检查新 prompt 的 gate 排除。
这是控制即时生效的结构实验，不测模型语言收益。

## 实验配置

- 模型 / suite / endpoint：synthetic-control-revocation / 待运行 / 临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：一个实际 read_file→done 任务；text/native × 开启 / 初始关闭 / 首轮后关闭，6 项各一次。
- on/off 对照：固定 synthetic directive candidate，真实 AppContext、scoped GhostInboxStore、
  task entry、workspace builder、kernel 和 ApiProvider；只有控制转换时点变化。
- 初始关闭必须首轮无 hint；一直开启必须第二轮仍保留 hint。
- 首轮后关闭发生在第一条 HTTP request 被捕获之后、返回 read_file 响应之前；读取实际生效值。
- context 32768 / reserve 8192 / keep recent 12000 / output 128 / temperature 0.3 / timeout 5s / max turns 2。
- 不改 scorer、不以缺失项为零；异常立即保存 cell 摘要并停止，不重试。
- 原始 request/reply、state、events、manifest 只存 ignored artifacts；只提交脱敏指标。

## 验证命令和结果

待运行；脚本为 `tests/manual/ghost_memory_routing/control_revocation_probe.py`。
exit 0 仅表示矩阵完整，不能作为控制 gate 通过结论。

## 当前生产边界

- 是否经过真实 provider-send：计划 ApiProvider → loopback HTTP。
- 是否改变默认 prompt：否；仅本地 synthetic candidate fixture。
- 支持模型 / 任务 / 配置：受测合成响应；不作模型效果推广。
- 生产合并结论：EXPERIMENT_ONLY；不新增 validator、兼容层或历史删除实现。

## 未完成或阻塞 / 下一步

先记录完整矩阵，再与 Provider History Expiry 一起决定最终 payload 的唯一准入宿主。
需要覆盖 native result、repair、receipt、resume、compaction 和拒绝后的恢复语义。
World Model 仍保持 SHADOW。
