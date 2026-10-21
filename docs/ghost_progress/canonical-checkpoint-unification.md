# Ghost 实验：Canonical Checkpoint Unification

- 实验名称：Canonical Checkpoint Unification
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：随实验脚本提交；正式矩阵后补完整 hash。
- 关联 Roadmap 验收门：0.6.1 / 来源、恢复、撤回
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

用户要求架构收敛、单一事实来源。本阶段验证：早期事实经 live checkpoint 保留、但原始
durable replay 因预算裁剪时，直接删除 exporter 是否造成信息丢失；能否通过已有 durable
context_edit 范围替换统一恢复，而非新增 journal、fallback 或另一条生产 writer。

三臂：baseline；durable_replay（只删除额外 export）；canonical_edit（摘要只读 canonical
条目，以已有 admit_write/context_edit 事务提交范围替换，再删除额外 export）。
不能直接把带 Ghost 的协议摘要作为用户事实持久化；canonical_edit 的来源不经过 Ghost 注入。
派生摘要角色为 assistant，不改 Evidence、Permission、CompletionProof 或工具结果事实。

## 实验配置

- 模型 / endpoint：synthetic-source-lifetime / 临时 loopback HTTP；不调用 KoboldCpp。
- Completions/Responses，native tools；window_reopen/app_restart；final on/scoped revoke。
- full_replay：context 65536 / reserve 8192；trimmed_replay：16384 / 2048。
- keep_recent 1000 / output 128 / temperature 0 / timeout 5s / max_turns 1。
- 每 cell 五个 prelude StartTask 加一个 final StartTask。仅第一任务含早期事实，五个 prelude
  各含 210 行固定合成历史；原生 done 不回显早期事实。最终任务为短任务。
- counter=ceil(serialized characters/4)，标记 estimated；固定容量 fixture，不声称 tokenizer。
- 原生 done 走完整 task/planner/builder/admission/recorded provider/HTTP/settlement。
- 实际 foreground/background compaction、auxiliary HTTP 与 checkpoint commit；每任务等待
  现有 maintenance 完成，不 patch 压缩、裁剪或恢复结论。
- summary fixture 仅复制实际 source 中存在的 fact/hint 标记；不是语言质量测量。
- canonical_edit 额外生成一次只读 canonical source 的辅助摘要，替换前四任务的消息范围、
  保留第五任务尾部。复用既有 SQLite writer / context_edit / codec / snapshot / activation。
- 每臂/预算 8 cells，一次重复；正式矩阵共 48 cells / 288 个任务，auxiliary HTTP 另计。
- 首五任务均 on；final 保持 on 或实际 session-scoped admit_hints=False。

## 本次完成

扩展既有 protocol_source_lifetime.py，独立 checkpoint schema，不重写旧测量。
setup 已验证真实窄预算下 bounded replay 丢失早期事实、live checkpoint 仍保留；
canonical_edit 在 Completions 的 reopen/restart on/off 四项 smoke 均保留事实且 off 不送 hint。
这只是 setup，不纳入正式矩阵，不算生产通过。

setup 初次 8192 window 不足以容纳单个构造请求，0 HTTP 即 overflow；改用能容纳当前任务的
16384 window，并在冻结前将 prelude 定为五轮，确保测到的是历史裁剪。全量 scorer、生产
门槛不变，不把这个 fixture setup 错误算作模型阴性。失败及所有 setup 留本地归档。

## 验证命令和结果

正式六个 suite、回归和结果待本阶段补充。Ruff 通过。

## 当前生产边界

- 是否经过真实 provider-send：实际 task recorded sends 和辅助 HTTP；不使用 live 模型。
- 是否改变默认 prompt：否，生产源码不变；三臂仅实验 monkeypatch/范围提案。
- 支持模型 / 任务 / 配置：确定性 codec/压缩/恢复结构；无模型语义推广。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞

context_edit 的 metadata 记录 source IDs/digest/revision，不代表已有原子的 source fence。
实验只在摘要后比较 head；并发检查与提交之间的 TOCTOU、fork/rebranch、source edit、
delete/reset、重复提交、summary failure、辅助 effect settlement 和真实模型安全尚未验收。
既有全局 compaction 会截断之前上下文，不可直接替代局部范围 checkpoint。
canonical_edit 暂只验证重新打开的窗口，warm 窗口不自动消费新 edit；不可冒充已统一生产。

## 下一步

完成宽/窄预算 × 三臂 × 两协议 × reopen/restart × on/off 矩阵；严格保留信息丢失和泄漏负结果。
随后沿既有 durable 事务补齐 source binding、提交 fence 和 live/replay 一致性，再考虑唯一
provider-send gate 的迁移。World Model 保持 SHADOW，不为统一增加第二套历史。
