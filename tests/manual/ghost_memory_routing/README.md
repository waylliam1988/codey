# Ghost 偏好投递与预算路由实验

这里保存同 prompt 排序、扩槽位排序、分层路由、偏好族投递与预算压力实验的脚本和
历史报告。它们是 manual/evaluation 材料，不是生产功能或自动上线控制器。

2026-11-17 晋级要求修订：一个实际使用的本地模型及配置完成行为 on/off 对照与相关生产边界验收，
即可在受测范围正式启用。第二模型为可选扩展，不阻塞单模型晋级；新模型或配置各自验收。
对照、最低样本量、任务范围、成本/延迟和关闭、删除、恢复等相关边界要求继续适用。
历史 `reports/` 保留当时测量与判定，后续晋级以当前[晋级协议](../../../docs/ghost_future_direction.zh-CN.md#06-晋级协议)为准；本次修订不自动将历史候选改为已启用。

同日已根据既有结果完成 Gemma 12B 普通 Chat 中文子集的生产接线与默认启用，见
[启用及遗漏核对](../../../docs/ghost_progress/language-production-enablement-20261117.md)。用户已要求停止该语言批次新增实机 A/B；不继续补跑，其不完整数据单独留档。

2026-11-18 按当前 World Model 任务另行冻结并完成首个验证预测复盘消费者：
[实验与负结果](../../../docs/ghost_progress/world-model-verification-review-20261118.md)。
真实 check/proof→prediction→review→有界 ContextSource/admission→Gemma 共 40 次完整请求，
严格 action on/off 均 0/20（回复带 JSON 围栏），input tokens +16.3%；按冻结门终止该接口
候选，不放宽 scorer、不接生产。该批不重复语言实验；原始回复及 fixture 只留本地
`.e2e-artifacts/world-model-verification-review-20261118`。

同日完成[结构化传输对照](../../../docs/ghost_progress/world-model-structured-review-20261118.md)：
新入口 `world_model_structured_review.py` 在两臂共同设置冻结 JSON schema，原 scorer 和
任务不变；40 请求格式全有效，严格正确数 on/off 均 8/20，input tokens +16.3%。按冻结
条件终止重复当前事实的预测候选，不仅换提示词补跑、不接生产，前序结果不重判。
脱敏报告在 `reports/world-model-structured-review-20261118.json`，完整日志/wire/state
仅留本地 `.e2e-artifacts/world-model-structured-review-20261118`。

## 文件与数据位置

2026-11-18 当前[项目经历连续性](../../../docs/ghost_progress/project-experience-continuity-result-20261118.md)：
`project_experience_continuity.py`冻结20条独立合成流、40物理请求，19/20对0/20，成本门通过。
`project_experience_continuity_metrics.json`保留当时`EXPERIMENT_ONLY`，不重写实验历史；
后续正常入口已在master受测范围默认启用，`PRODUCTION_MERGED`，见
[生产记录](../../../docs/ghost_progress/project-experience-production-20261118.md)及`reports/project-experience-production-20261118.json`。
最终274项与类型整理后31项通过，指定全树门及14 owned审计结果见该记录，依赖诊断分列。
仅Gemma/native/非流式/output128，默认8192回退；没有新增学习机制/World Model完成主张。
生产故障注入与额外Any审计均无新增模型调用，原始日志/state/hash留本地。


2026-11-18 G0 [图与continuity逐记录维护](../../../docs/ghost_progress/selective-maintenance-production-20261118.md)：
入口 `python -m pytest tests/test_ghost_selective_maintenance.py tests/test_ghost_selective_maintenance_events.py tests/test_ghost_maintenance_record_scope.py -q --tb=short`。
原12格scorer不改，四领域12/12；另135项新边界。普通App/default daemon默认消费；
报告 `reports/selective-maintenance-production-20261118.json`，完整log/XML/state/hash仅留
`.e2e-artifacts/ghost-selective-maintenance-20261118/`。最终扩大/静态门及中间失败归属见记录。
checkpoint窗口有界化/未知父project/旧身份/混版本限制保留，无模型调用。用户要求本批
commit后停止，[接手说明](../../../docs/ghost_progress/selective-maintenance-handoff-20261118.md)为恢复入口。

此前独立批次的历史结果：
2026-11-18 G0 [Queue逐记录维护](../../../docs/ghost_progress/queue-record-maintenance-production-20261118.md)：
入口 `python -m pytest tests/test_ghost_queue_record_maintenance.py -q -s --tb=short`，46项通过；
最终20文件495项与全静态门通过，三个owned Any零诊断。普通App/default daemon及直接
owner默认消费；报告 `reports/queue-record-maintenance-production-20261118.json`。
原四领域矩阵不改仍6通过/6失败，queue子比较12/12，其余三个领域未修；scope限制下queue
checkpoint暂缓/窗口可能超预算、未知父project时session暂停的限制均保留。
原始state/log/XML在`.e2e-artifacts/ghost-queue-record-maintenance-20261118`，未调用模型。

2026-11-18 G0 [Sleep 维护撤回与独立控制兼容](../../../docs/ghost_progress/maintenance-control-revocation-20261118.md)：
入口 `python -m pytest tests/test_ghost_maintenance_revocation.py tests/test_ghost_control_materialization.py tests/test_ghost_maintenance_control_boundary.py -q -s --tb=short`。
撤回18格9→18、纯控制96格92→96，44项追加边界；生产 `06c8cfc4`/`5407430f` 在普通
App/默认daemon及原控制面直接生效。报告 `reports/maintenance-control-{baseline,production}-20261118.json`
与 `reports/control-materialization-baseline-20261118.json`。最终29文件733/2子测试与全树
静态门通过，五owned Any零诊断；原始夹具/调度/类型失败保留，不把跨scope全局维护及
pending/硬退出称为通过。无模型调用；完整SQLite/日志/XML只留
`.e2e-artifacts/ghost-maintenance-revocation-20261118`。

2026-11-18 G0 [正常 App 硬退出与 HTTP 恢复](../../../docs/ghost_progress/app-http-crash-recovery-20261118.md)：
入口 `python -B tests/manual/completion_operation_resume_smoke.py --self-test --state-root <isolated-dir>`；
pytest 等价入口 `tests/test_ghost_app_http_crash_recovery.py`。三 scope×四窗口×enabled/revoked
24 格基线 18 通过/6 个保存响应恢复失败，生产 `86ad9a83` 后原矩阵 24/24 通过。
报告 `reports/app-http-crash-{baseline,production}-20261118.json`；新增 API 两协议契约
边界在 `tests/test_api_restored_tool_contract.py`。普通 App/kernel 默认恢复原声明工具，
保存响应不再重发 HTTP 来完成；unknown 不重试、旧失败任务不自动重开。
真实 SQLite/硬退出/默认 App scheduler/loopback HTTP，无模型调用；完整日志/请求/state
仅留本地 `.e2e-artifacts/ghost-app-http-crash-recovery-20261118`。旧脚本接口及夹具失败
单独保留；历史 release smoke 的通过不代表当前版本已通过此矩阵。

2026-11-18 G0 [节点容量裁剪与跨 scope 重建](../../../docs/ghost_progress/graph-capacity-scope-replay-20261118.md)：
入口 `python -m pytest tests/test_ghost_graph_capacity_scope_replay.py -q -s --tb=short`，
追加边界在 `tests/test_ghost_graph_pruning_boundaries.py`。原 12 项 6→12，生产
`34852638` 默认写入/消费裁剪差量；基线与生产脱敏报告为
`reports/graph-capacity-scope-replay-{baseline,production}-20261118.json`。完整日志、
XML 和 SQLite 仅留本地 `.e2e-artifacts/ghost-graph-capacity-scope-replay-20261118`。
无模型调用；旧缺边界/降级限制和压缩夹具失败归属见记录，不扩大到全部历史或 G0。

2026-11-18 G0 [图同分裁剪与重建](../../../docs/ghost_progress/graph-tie-replay-20261118.md)：
复现入口是 `python -m pytest tests/test_ghost_graph_tie_replay.py -q -s --tb=short`，生产
验收另见 `tests/test_ghost_graph_tie_lifecycle.py`。原 24 项 9→24，原始基线与生产脱敏
报告在 `reports/graph-tie-replay-{baseline,production}-20261118.json`；真实测试 DB、
完整日志和 XML 只留本地 `.e2e-artifacts/ghost-graph-tie-replay-20261118`，不调用模型。

| 文件 | 作用 |
|---|---|
| `seed_arms.py`、`run_arm.py`、`build_report.py` | 相同 prompt 条件下比较生产排序、新近排序与空白对照 |
| `round2.py`、`dump2.py` | 扩语言/命令等槽位，在条目上限下比较入选差异 |
| `round3.py` | 分层收益排序与冲突替换消融；新近/生产/空白数据复用前一个实验 |
| `round4.py` | 语言/命令 on/off、当前请求覆盖、预算压力及离线生命周期检查 |
| `world_model_verification_review.py` | 首个 World Model 验证复盘候选；隔离真实 check/proof、纯预测/review、独立 admission 与冻结单模型 on/off；已保留格式负结果，不被生产 import |
| `summarize.py` | 从本地原始数据导出无回复 metrics，或独立复算随目录保存的快照 |
| `scope_authority_smoke.py`、`scope_authority_report.py` | 真实 planning builder/kernel/ApiProvider 的 on/off 结构 smoke、baseline/candidate payload 摘要与脱敏报告，不作 language 晋级 |
| `history_expiry_probe.py` | 固定 text/native 响应及显式时间，复现后续 HTTP history 的过期 hint 重发；保留负结果，不接生产 guard |
| `protocol_source_lifetime.py`、`protocol_source_lifetime_report.py` | 真实 task/planner/window/HTTP/SQLite 的两任务生命周期矩阵及重复 export 消融；报告可从脱敏指标独立复算，warm 泄漏仍未修 |
| `protocol_source_lifetime.py --checkpoint ...`、`checkpoint_unification_report.py` | 六任务真实压缩/预算裁剪/恢复矩阵；仅删 export 的信息丢失负结果与 canonical context_edit 统一候选，独立 schema 和严格完整性复算 |
| `canonical_source_fence.py`、`canonical_source_fence_report.py` | 42-cell 来源事务三臂对照；实际 task / 辅助 HTTP 后注入追加、分支、编辑与删除重建；runner 需候选分支，脱敏报告可在 master 复算，不计作模型行为分数 |
| [reports/](reports/) | 历史报告、合成偏好 directive 快照、parity/audit 与脱敏测量 |
| `artifacts/ghost_memory_routing/`（仓库根目录下） | 完整本地归档、原始 JSONL、隔离 state、生成报告；已被根目录 `/artifacts/` 规则忽略 |

`reports/REPORT.md` 对应同 prompt 排序，`REPORT2.md` 对应扩槽位排序，
`REPORT3.md` 对应分层路由，`REPORT4.md` 对应偏好族与预算压力验收。
`reports/REPORT4-frozen-evaluation.md` 是 0.6.0 冻结 evaluator 重新复算的脱敏报告；其
完整性规则和 `PROMOTE-MATCHED` rank 映射由 `evaluation_contract.py` 固定。
`reports/REPORT4-admission-live.md` 是 0.6.1 真实 runtime/provider-send smoke 的脱敏报告；
它只覆盖 admission smoke suite，不构成 language 晋级对照。
`reports/scope-authority.md` 记录请求作用域安全修复及新 live payload parity；
`reports/history-expiry.md` 记录完整 history 仍有 expiry 缺口，不能把初次准入通过当作完整发送验收。
`reports/attempt-gate-history-expiry-20261021.json` 和
`reports/attempt-gate-revocation-20261021.json` 记录当前唯一 Attempt Gate 在 loopback
HTTP 上对过期和首轮后撤回的闭合验证；它们不包含 live 模型行为评分，也不替代完整
repair/resume/compaction acceptance。
`reports/language-live-20261021.md` 记录当前 commit 的 modelA language 20/20 on/off
矩阵；它是单模型条件候选，不是跨模型或默认启用结论。
数字保留为历史文件标识；长期路线使用实验名称和具体行为描述。

归档迁入日期为 2026-10-19。原目录共 1,491 文件、48,217,898 字节，迁移时逐文件
验证 SHA-256；来源文件摘要见 [reports/archive_manifest.json](reports/archive_manifest.json)。
manifest 仅保存 25 个代表文件的摘要，不含全部 1,491 个文件的逐文件清单；全量计数和
字节数是迁移时的验证记录。原始文件名保留在 `files` 键中；`snapshot_path_overrides`
记录快照命名修正（`parity/triiggers.json` → `parity/triggers.json`），原始归档不改名。
本目录 `.gitattributes` 禁止报告快照的自动换行转换，以保留原始字节摘要。
原始脚本与数据也完整保留在本地归档中；这里的可运行脚本仅调整路径、导入、输出文件名及静态检查问题，
保留历史 treatment 与 scorer。历史 capture 的 Git revision 未记录，不用迁移时 revision 冒充。

`modelA` 是数据中的 suite 标签，其记录模型 ID 为
`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；没有第二模型的测量。

## 不需要模型的复查

从仓库根目录执行：

```powershell
python tests/manual/ghost_memory_routing/summarize.py
python tests/manual/ghost_memory_routing/round4.py --help
```

`summarize.py` 默认读取随目录保存的 `reports/measurements.json`，仅使用 Python 标准库，
没有本地 state、原始回复或模型服务也能复算各 cell 的分子、分母与字符总数。
其中分层实验只有新增的 18 次运行，复用基线没有重复计入。

完整本地归档存在时，也可以复算原格式报告：

```powershell
python tests/manual/ghost_memory_routing/build_report.py
python tests/manual/ghost_memory_routing/round2.py report
python tests/manual/ghost_memory_routing/round3.py report
python tests/manual/ghost_memory_routing/round4.py report
```

如果 live 运行中断，恢复后可以只追加缺失 index，不覆盖已有 JSONL：

```powershell
python tests/manual/ghost_memory_routing/round4.py run --suite <suite> --cells lang_on_d2 --indices 14,15,16,17,18,19 --endpoint <URL> --model <MODEL_ID>
```

生成文件写到 `artifacts/ghost_memory_routing/`，不会覆盖随目录保存的历史 `reports/`。
设置 `GHOST_EXPERIMENT_HOME` 可将所有生成物和隔离 state 重定向到另一个目录。
重新导出 metrics 时显式执行 `summarize.py --export --input <新快照路径>`，不要覆盖历史快照。

## 目标单模型的新批次或配置运行

从仓库根目录执行；模型服务需已由使用者配置并启动。新 suite 标识独立批次，不要求更换模型；
可选的后续模型扩展也使用此流程，并独立评估：

```powershell
python tests/manual/ghost_memory_routing/round4.py seed
python tests/manual/ghost_memory_routing/round4.py run --suite <NEW_SUITE> --endpoint <URL> --model <MODEL_ID>
python tests/manual/ghost_memory_routing/round4.py report
```

迁入的归档已有 `R4` seed，`seed` 会跳过。新数据目录需要重新 seed。
`round4.py offline` 的冲突/删除检查会自行创建 `R4C`，不依赖 `round3.py t5`；
但同一命令里的过期检查仍读取 `T5`。新目录运行完整离线检查时，先执行一次
`python tests/manual/ghost_memory_routing/round3.py t5` 准备过期 fixture，再执行
`python tests/manual/ghost_memory_routing/round4.py offline`。归档已有 `T5` 时无需重种。
不能把缺少该 fixture 时的空 directive 当成过期验证通过。
同名 suite/cell 的 `run` 会重写对应原始 JSONL，应为新批次、新模型或配置使用新的 suite 名。
这些是显式 live 运行，会启动本地测试 HTTP server 并调用指定模型；报告复算不调用模型。
现有冻结 evaluator 固定读取 `modelA`；新 suite 的正式评估须在运行前另行冻结该目标模型的
suite、任务、样本与对照配置，不能把缺少 `modelA` 的新 suite 直接当作现有 evaluator 已通过。

## 历史结论的使用边界

- language 的 19/20 对 0/20 只支持受测模型和配置的有条件生产候选；生产 allowlist 与真实
  Context Epoch 发送链路尚未通过同一 treatment 验收。
- command 主 on/off 各 0/20，预算臂存在零星成功；intent 层宿主尚未被证明。
- 行数、内容和顺序同时变化，不能把预算曲线的变化全部归因于位置。
- cap8 分层无自述，但 cap12 仍有三条；零先验不等于显式排除。
- 冲突检查的总节点数不能证明 supersede 失败，应核对 active/status、superseded_by 与发送集。
- 历史 `family_verdicts()` 会跳过缺失 suite、把缺失 off 视作零，未强制最低样本量，
  且 `PROMOTE-MATCHED` 缺 rank 映射。它的 PROMOTE 不是生产 gate；未来使用前必须按
  [晋级协议](../../../docs/ghost_future_direction.zh-CN.md#06-晋级协议)修正，并冻结必测矩阵。
- 0.6.0 已将这些完整性问题移到 `evaluation_contract.py`：modelA 单模型试点固定为
  language/command 各自 on/off、4 个问法、每问法 5 次；缺失 suite、control 或样本量输出
  `INCOMPLETE`，不会把缺失当成 0。该结果仍是 `EXPERIMENT_ONLY`，没有改变默认 prompt。

长期结论与实现顺序见 [Ghost 方向](../../../docs/ghost_future_direction.zh-CN.md)和
[Roadmap](../../../ROADMAP.zh-CN.md)。原始报告保持历史判定，后续解释不回写成新测量。
