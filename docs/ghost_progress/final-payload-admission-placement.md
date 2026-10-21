# Ghost 实验：Final Payload Admission Placement

- 实验名称：Final Payload Admission Placement
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：待记录
- 关联 Roadmap 验收门：0.6.1 / 0.6.2
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

历史 expiry 和运行中撤回均已在真实 HTTP 复现。验证最终 gate 必须经过 configure_request、
compaction 和 request lease 等待，且覆盖 retry 与辅助模型。假设仅移动到 `_admit_request`
仍有 lease/retry 时隙；移动到 `_generate_attempt` 可封住时隙，但原 validator 的精确文本契约
未必能接受被 JSON 编码的辅助摘要输入。不得为了通过而添加 decoder fallback 或跳过辅助发送。

## 实验配置

- 模型 / suite / endpoint：synthetic-payload-placement / 待运行 / 临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：一个 done 任务；两种真实 API codec × 10 个边界案例 × 3 种 gate 位置；60 项各一次。
- on/off 对照：初始开启 / 初始关闭；正常与 lease 时 expiry/revoke、503 retry expiry、wire rewrite、
  compaction 正常/lease expiry、中文多行含引号的正常/compaction。
- 三种位置：现有 kernel 基线；完整 payload 准备后；lease 获取后每次 `_generate_attempt`。
- 候选只在实验进程 monkeypatch 移动现有 validator；替换 kernel predicate，不叠加第二份判定。
- builder、kernel、ApiProvider、codec、真实 HTTP、compaction 辅助发送与 scoped store 均走真实代码；
  history、响应、时间、pressure counter、lease/retry policy 是显式 deterministic fixture。
- context 65536 / reserve 8192 / keep recent 12000 / output 128 / temperature 0 / timeout 5s / max turns 1。
- source 在 T00:00:01Z 到期，首次 lease 或 retry 前显式将时间从 T00:00:00Z 推进到 T00:00:02Z。
- 不测语言效果，不把合成 counter 写成真实 tokenizer，不修改 scorer 或样本门槛。
- 原始 payload/reply、state、policy、manifest 只存 ignored artifacts；只提交脱敏指标。

## 本次完成 / 验证命令和结果

待运行。任何 exit 0 仅表示矩阵完整，不代表候选通过生产晋级。

## 当前生产边界

- 是否经过真实 provider-send：计划两种 codec 的 ApiProvider → loopback HTTP，包括辅助摘要。
- 是否改变默认 prompt：否，实验进程中的固定 fixture，不合并候选 guard。
- 支持模型 / 任务 / 配置：确定性合成边界，不作模型/语言效果推广。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

先验证 gate 位置和健康对照，再决定是否需要在 canonical history 保留 source lineage。
本实验不证明跨 run/resume、远端 web history 可见性或完整 trace/wire parity。
World Model 继续 SHADOW。
