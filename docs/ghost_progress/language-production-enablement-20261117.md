# Ghost 已验证功能生产启用与遗漏核对

- 日期：2026-11-17
- 状态：`PRODUCTION_MERGED`（本次 master 生产提交；仅下述中文偏好范围）
- 生产提交：见 `git log --grep="enable validated local Chinese preferences"`。
- 依据：[历史 language on/off](language-live-evaluation-20261021.md) 为 20/20 对 0/20；更早的冻结评估为 19/20 对 0/20。
- 用户决策：采用已有单模型结果接入生产并默认启用；停止新增实机 A/B，不以补跑完整矩阵为本次启用前置。

2026-11-17 后续回归维护：canonical receipt 落地后，旧测试直接写 observation 的 fixture
已过时；[Manifest 默认调度接线](deletion-manifest-production-wiring-20261117.md)改为真实
InputQueue/任务执行后再人工确认语言偏好，并清理本测试文件的隐式 Any。22 个原有语言
行为用例保留发送、禁用、模型回退、删除/重启与发送前撤回断言，纳入最终 741 项相关回归
及类型收窄后的 123 项复验。仅为 loopback/确定性测试，没有恢复停止的实机批次或新增模型收益主张。

## 默认行为

普通 Chat 的中文偏好已接入现有 `task_entry → ghost_directive → ContextSource → Attempt Gate → ApiProvider`。
`style_preference:language=chinese` 进入生产类型白名单，渲染为 `- Prefer: reply language = Chinese.`，
并优先于其他记忆排列，避免在有界预算中被挤出。其余字段延续原有排序。

默认启用范围为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，本机 loopback
`http://…/v1`、OpenAI Completions、native tools，context/reserve/keep recent 为
262144/32768/32000。新模型、不同协议或预算配置使用原基线。没有新增后台模型调用或全局晋级控制器。

需要已有的、用户确认过且 scope 匹配的中文偏好；不会自动替用户创建偏好或重新开启用户已关闭的控制。
本地上下文面板的偏好选择新增 `Chinese answers`，仍经“从保存请求提议 → 人工确认”流程。
CLI 预览可以显示中文字段；实际投递另受上述模型与配置限制。当前明确请求仍优先。

每次 physical attempt 复查 `admit_hints`；携带中文偏好的请求还重新读取当前偏好及模型配置。
组装后删除、撤回、关闭检索或切换模型导致原 directive 失效时，在 HTTP 前拒绝旧 payload。
回滚可关闭 `admit_hints`、关闭本地更新、拒绝/删除该偏好，或回退本次生产提交；不删除原始归档。

## 已有结果与生产代码逐项核对

| 项目 | 核对结论 | 本次处理 |
|---|---|---|
| language 中文投递 | 唯一有明确模型净收益、此前仍缺普通生产白名单和 UI 入口的候选 | 补齐保存、确认、渲染、排位、模型限定和发送前复查，默认启用 |
| 经历记录/检索、已确认偏好、continuity、work queue/affinity、sleep | `task_entry`、`ghost_post_turn`、Research 和后台维护已有消费者；开关已接线 | 保留既有生产行为，不添加第二条路径 |
| Scope Authority、过期/撤回、Canonical Context/Attempt Gate、approval context snapshot | 已在生产发送、durable context 和恢复路径接线 | 保留；历史候选实现不重复接入 |
| Research state contract、四个 Ghost 控制 | 已有生产消费者，部分文档仍写 `SHADOW_ONLY` | 同步实际生产状态；完整并发/reset 验收的未完成记录继续保留 |
| Frozen evaluator、Attempt Gate lifecycle fixtures | 评估规则及回归材料，生产 gate 已存在 | 保留测试职责，不把实验 harness import 到生产 |
| Deletion Manifest | 本次核对时仍为候选；后续已完成混合生命周期限定修复、普通 API/CLI 默认调度及操作发现恢复，见[生产调度](deletion-manifest-production-wiring-20261117.md)与[发现恢复](deletion-manifest-discovery-production-20261117.md) | 已默认接入控制面；完整 retention、升级/异常输入等边界仍按进度板推进，不是新增模型行为晋级 |
| command / verification intent、复杂 Hebbian 排序、format/length 精确服从 | 没有证明可新增默认启用的稳定净收益 | 不扩大已有投递范围 |
| World Model、Protocol Adaptation、可塑性研究 | 无已验证的可上线消费者或尚属提案 | 保留研究状态 |

核对覆盖 `docs/ghost_progress` 全部记录、memory-routing 各轮报告、现有 Ghost manual A/B 入口及
2026-10-30 approval snapshot。未发现其他“已有模型收益、仅缺生产接线或默认开关”的功能。

## 本次验证

- 首个回归在保存中文偏好时重现 `400 unsupported preference`；修复后真实 entry/loopback HTTP 验证通过。
- 新增 22 项生产语言回归，覆盖未确认、跨会话、禁用、模型/配置回退、预算排位、删除/重建/重启、
  以及组装后的删除/撤回/模型变更。故障注入暴露的旧 payload 发送已由同一 Attempt Gate 复查闭合。
- 受影响回归：`196 passed, 40 subtests passed`，覆盖 directive、manual memory、provider-send、controls、admission、lifecycle、control surface、关闭故障和 UI。
- Ruff 通过；4 个生产 Python 文件 mypy 通过；前端 TypeScript 检查、Vite build、生产静态资源发布与一致性检查通过；`git diff --check` 通过。

新增实机批次已按用户要求停止，未补跑：本地产物
`.e2e-artifacts/ghost-language-production-20261117-01/summary.json` 标记 `STOPPED_BY_USER`。
仅完成 on 8 次（7 次中文判定通过、1 次未通过），off/override 未运行；这是不完整的新样本，
不作为新的完整对照或替代历史报告。原始回复、provider 日志、隔离 state 和 runner 只保留本地。
本次不宣称新的完整实机通过率、成本/延迟收益或全模型支持。
