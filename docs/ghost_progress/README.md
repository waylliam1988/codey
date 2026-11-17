# Ghost / World Model 进度板

这是长期维护的状态索引。连续大型实验按实验名称或候选功能维护记录；路线图中的版本号只作为可选的验收门关联，不要求每次实验都创建一个版本号。

状态含义、KoboldCpp 故障流程、commit 约定和交接模板见 [团队交接说明](../ghost_handoff.zh-CN.md)。当前流程允许同事直接提交主分支，不要求 PR；生产代码仍必须满足交接说明中的生产合并门槛。

2026-11-17 晋级要求修订：一个目标本地模型及配置的行为收益与相关生产边界通过，即可在受测范围
正式启用并按合并事实标记 `PRODUCTION_MERGED`。第二模型与跨模型验证是可选扩展，不阻塞单模型晋级。
本次只修订验收范围，历史测量与已有完成、合并状态不自动变化。

## 当前状态

[正常 App 硬退出与 HTTP 恢复](app-http-crash-recovery-20261118.md)接续审查：旧
headless smoke 原样一次因过时 constructor 的 TypeError 失败，未进入恢复断言；
全树静态门通过，非生产丢状态证据。继续按 durable owner/正常恢复入口修正材料并
冻结完整 App×physical HTTP 组合，复用已有 kernel/SQLite/App 证据。

[节点容量裁剪与跨 scope 重建](graph-capacity-scope-replay-20261118.md)已默认生产修复，
提交 `34852638`：原 12 项 6→12，实际裁剪差量与强化事务独立身份供普通 writer/rebuild/
CLI 消费，无新开关。同秒反复重学追加回归 0→3；新增 44 项通过。扩大 34 文件
1474 passed/3 个旧压缩夹具身份冲突/34 子测试，修正夹具独立 ID 后 63 项复验及全树
静态门通过；四个 owned 文件 Any 零诊断。两批生产字节相同，不把它写成一次全通过。
旧缺边界日志仅做受测拒绝保护，不保证任意历史或无缝降级。继续完整 App/physical HTTP
硬退出/跨 run 恢复；旧 headless smoke 的 JSONL/重复 run 假设待先复现审查。

[Hebbian control payload](hebbian-control-replay-20261118.md)已默认生产修复：原
168 项 36→168 通过，严格 counts/identity/keys 校验保留最早 v1 两字段 decay，
record-only 标记不重复执行，生产 `24c79933`。基线 `9b90e1e7` 保留；新增 153 项边界，32 文件
1421 项/34 子测试及全树静态门通过，三个 owned 文件 Any 零诊断。普通 CLI 直接消费，
无新开关或模型调用；其余容量/恢复、完整 G0 与 World Model 仍未完成。

[图同分裁剪与重建一致性](graph-tie-replay-20261118.md)已默认生产修复：原 24 项
9→24 通过；共享节点 id/边语义身份稳定 tie，普通 writer/rebuild/维护/快照沿用同一
helper，无新开关。新增 30 项生命周期/Affinity 验收，相关 32 文件 1153 项/34 子测试、
全树静态门及四个 owned 文件 Any 零诊断；原始负结果 `596c2ee0` 保留。其余 G0 与
World Model 未完成，继续记录型 control payload 和其余排序/恢复边界。

World Model [结构化复盘传输对照](world-model-structured-review-20261118.md)已完成：
同一 Gemma 配置新 40 请求两臂各 20/20 格式有效、正确数均 8/20，输入 tokens +16.3%。
按冻结门终止重复当前验证事实的候选，不只换提示词补跑；格式改善不归因于 World Model，
前序负结果不重判，无生产晋级。相关 206 项/31 子测试及全树静态门通过，owned Any 零诊断。
后续先继续 G0 实际缺口；World Model 新候选须提供新增预测信息，整体仍未完成。

World Model 首个[验证复盘消费者实验](world-model-verification-review-20261118.md)已完成真实
check/proof→prediction→review→独立投影/admission→Gemma 的 40 次请求。严格 action
scorer on/off 均 0/20（全为带围栏 JSON），输入 token +16.3%；按冻结门终止本候选接口，
不放宽 scorer、不误记服务失败、不加生产空壳。World Model 整体仍未完成。

2026-11-18 新完成：[Hebbian 内部记录时间与普通消费者](hebbian-row-time-20261117.md)。
原 54 cell 18→54 通过；坏时间默认拒绝，合法旧时间/fallback 保留，普通 reader/Sleep/
directive 与真实 App→HTTP 故障隔离及恢复通过。最终 27 文件 1031 项/34 子测试与全树
静态门通过；原始负结果 `0cc80548` 保留。无新增默认关闭开关，G0/World Model 仍未完成。

2026-11-17 用户已恢复持续推进。新完成：
[Hebbian 损坏事件原子重建](hebbian-replay-integrity-20261117.md)，原 54 cell 12→54 通过；
普通 rebuild-state 默认拒绝坏 envelope/node/edge/status 并保留有效投影和原日志，
不再复活旧 active 或静默丢状态。26 文件 891 项/34 子测试通过，测试类型修订后最终
81 项及全树静态门通过；实验负结果 `ab68edb7` 保留，完整 G0 与 World Model 未完成。
本轮未停止目标；继续剩余依赖，不重做已闭合的 HTTP/continuity 矩阵。
以下暂停说明为此前批次历史，不再阻止本次已授权工作。

按用户要求，本批完成后暂停，不自动启动下一批。最新完成：
[原生回执不完整HTTP响应生产修复与暂停交接](native-uncertain-response-production-20261117.md)。
JSON响应默认先核对Content-Length，未收齐为MAYBE_SENT并禁止重发，不再错误接受任务完成。
原72项48→72通过，扩大39文件417项通过，冗余类型转换修订后最终7文件155项通过；
全静态门与两个修改文件owned Any零诊断。本批进程已退出，原始负结果`c9487a65`保留。
完整G0与World Model仍未完成；恢复任务前先读上述交接，不重做已闭合矩阵。

此前完成：[原生回执准入拒绝与恢复组合验收](native-refusal-recovery-20261117.md)。
两协议×三控制scope×成功/错误回执×enabled/暖恢复/冷恢复36项全部通过；
真实共享kernel/SQLite/HTTP验证两次拒绝均NOT_SENT、原回执保留、重开后不重复执行工具。
相关12文件187项通过；本轮未改生产，现有唯一gate及续传已默认接线，无新增默认关闭开关。
Thread回调类型修订后36项复验与全静态门通过，新增测试owned Any零诊断，两批不相加。
首轮夹具误用工具审计名的36失败保留，不算生产负结果；完整0.6.1/0.6.2及World Model仍缺。

此前完成：[continuity过期持久事实与消费者](continuity-expiry-production-20261117.md)。
普通Sleep/post-turn维护记录可重放过期事实，reader/builder/真实队列共用逻辑时间筛选；
同来源可重新出现，原始历史保留。原18项3→18通过，旧日志9项3→9通过；75项新增，
最终44文件1064/4子测试及全静态门通过，五受检文件额外Any零诊断。无新增默认关闭开关。
其余维护/排序/digest/replay、完整准入/控制/生命周期及World Model仍待继续。

最新完成：[Sleep显式维护时间](sleep-maintenance-time-production-20261117.md)。
普通Sleep默认捕获一次maintenance_as_of，衰减、continuity刷新、租约回收与Affinity来源过期
筛选共用；原15-cell矩阵0→15通过。63项新增，扩大1047/36子测试、最终281/2子测试及
全静态门通过，九受检文件owned Any零诊断。continuity过期随后闭合见上，继续其余维护/排序/digest/replay；
完整0.6.4与World Model未完成，原始负结果保留。

最新完成：[重放历史时间校验](replay-timestamp-validation-production-20261117.md)。
原12个矩阵项3→12通过，九项坏时间默认拒绝并保留事务前有效投影；71项新验收、候选211、
最终523/36子测试及全静态门通过。扩大1194/1的旧测试在临时目录销毁后读DB，已修正并
保留原行为断言；五个修改/新增文件及原基线共六文件额外Any零诊断。完整0.6.4仍未完成。

最新完成：[observation 原始历史与兼容消费视图](observation-consumption-view-production-20261117.md)。
schema9 默认原地保留 observation_history，旧名为窗口约束的原生只读视图；原 reader
15项13→15通过，旧writer42项继续通过，另19项真实schema7/8迁移/回滚/序号/向量外键/
缓存SELECT/损坏/空写入验收通过。候选273项、扩大1122项/136子测试及完整静态门通过，
16个owned文件额外Any零诊断。首轮266/1的空视图触发器问题保留并由原生只读修正；
不撤销旧快照或旧进程内存，不证明整个混版本App/模型发送、磁盘purge或World Model完成。

此前完成：[原始事件物理删除防护](history-delete-guards-production-20261117.md)。
真实warm schema7旧压缩/reset原42项14→42通过，另13项旧缓存SQL/读快照/事务回滚/既有schema8
补装验收通过；扩大1088项/136子测试、测试行工厂类型修正后13项复验及完整静态门通过，
四个owned文件额外Any审计零诊断。默认owner构造即安装三表约束；旧reader/其他混版本事务、
磁盘retention/purge及完整生命周期仍缺，不能称为完整0.6.3或World Model完成。

此前完成：[普通reset历史保留生产修复](reset-history-retention-production-20261117.md)。
原63项由21→63通过，另50项reset恢复/容量/回滚/新输入/开关/向量交错通过；扩大906项/
136子测试、import格式修正后的125项复验及完整静态门通过，六个owned文件额外Any审计零诊断。
正常单版本runtime的普通reset默认生效；混版本旧进程、磁盘retention/purge等仍待验。

此前完成：[容量检查点与历史保留](capacity-history-retention-production-20261117.md)。
原78项矩阵48→78通过，另55项窗口验收；扩大776项/136子测试、最后SQL复查后的366项复验及完整静态门通过。
正常单版本runtime和冷启动升级默认生效；普通reset随后修复见上，混版本旧进程和磁盘策略仍待验。

| 路线图轨道/实验主题 | 状态 | 说明 | 记录 |
|---|---|---|---|
| 节点容量裁剪与跨 scope 重建 2026-11-18 | `PRODUCTION_MERGED`（受测 writer/replay/生命周期） | 生产 `34852638`；原 12 项 6→12，同秒重学 0→3，44 项边界；1474/3 旧夹具身份冲突/34 子测试，修正后 63 项复验及全静态门通过；差量事件默认消费，旧缺边界/降级有明确限制 | [基线、生产、失败归属与剩余审查](graph-capacity-scope-replay-20261118.md) |
| 图同分裁剪与重建 2026-11-18 | `PRODUCTION_MERGED`（共享 bounds 与受测生命周期） | 原 24 项 9→24；真实 degree cap 三 scope/warm/cold 一致，新增 30 项维护/删除/恢复/Affinity 验收；1153/34 子测试及全树静态门通过，无新开关 | [基线、生产与剩余审查](graph-tie-replay-20261118.md) |
| World Model 结构化复盘对照 2026-11-18 | `ABANDONED`（受测持久性预测消费者） | 新 40 请求格式全有效；on/off 各 8/20，input tokens +16.3%；无增益，终止重复事实候选；206/31 子测试及全树静态门通过，无生产新增 | [冻结、wire 与无增益结果](world-model-structured-review-20261118.md) |
| World Model 验证预测复盘 2026-11-18 | `ABANDONED`（受测文本→严格 action 接口） | 原型闭环与 40 次 Gemma 实机请求完成；on/off 各 0/20，40 个带围栏回复未过冻结 JSON scorer，额外 input tokens +16.3%；保留负结果，未生产晋级 | [冻结、原型与负结果](world-model-verification-review-20261118.md) |
| Hebbian 内部记录时间 2026-11-17/18 | `PRODUCTION_MERGED`（解码及普通消费者） | 原 54 项 18→54；最终新增文件 140 项，扩大 1031/34 子测试与全树静态门通过；坏历史保留有效投影，坏投影省略提示且可重建恢复，关闭/删除/reset/冷恢复及实际 App→HTTP 已验 | [基线、生产与剩余审查](hebbian-row-time-20261117.md) |
| Hebbian 损坏事件原子重建 2026-11-17 | `PRODUCTION_MERGED`（普通显式重建） | 原 54 项 12→54 通过；另 27 项 CLI/关闭控制下删除与 reset/冷恢复/写入失败/状态一致性验收；扩大 891/34 子测试、类型修订后最终 81 项与全静态门通过；原日志及有效投影保留，无新增默认关闭开关 | [基线、生产与剩余审查](hebbian-replay-integrity-20261117.md) |
| 原生回执不完整HTTP响应修复 2026-11-17 | `PRODUCTION_MERGED`（普通JSON发送默认路径） | 原72项48→72通过，扩大417项、类型修订后最终155项与全静态门通过，两个文件owned Any零诊断；未收齐帧为MAYBE_SENT、不完成不重发；按用户要求本批后暂停 | [生产修复与暂停交接](native-uncertain-response-production-20261117.md) |
| 原生回执不确定HTTP响应基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 原72项48通过/24失败；合法JSON但Content-Length未收齐误完成，EOF与坏JSON对照正确；后续修复闭合，原测量保留 | [冻结矩阵与负结果](native-uncertain-response-baseline-20261117.md) |
| 原生回执准入拒绝与恢复 2026-11-17 | `EXPERIMENT_COMPLETE`（现有默认生产链路） | 两协议/三scope/成功错误回执/enabled暖冷36项与相关187项通过；回调类型修订后36项复验、全静态门与owned Any零诊断；真实HTTP/SQLite证明NOT_SENT续传不重复工具；生产无需重复修复 | [组合验收与限制](native-refusal-recovery-20261117.md) |
| continuity过期持久事实与消费者 2026-11-17 | `PRODUCTION_MERGED`（普通维护/读取/队列消费） | 原18项3→18通过，旧同步日志9项3→9通过；75项新增，最终44文件1064/4子测试及全静态门通过，五文件owned Any零诊断；历史/来源可更新性保留，无新增默认关闭开关 | [生产接线与边界](continuity-expiry-production-20261117.md) |
| continuity过期持久投影基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 三scope×六场景18项有效，原3通过/15失败；renderer控制正确，相关135/2子测试及静态门通过；后续生产接线闭合本矩阵，原测量保留 | [冻结矩阵与结果](continuity-expiry-baseline-20261117.md) |
| Sleep显式维护时间 2026-11-17 | `PRODUCTION_MERGED`（普通Sleep及实际消费者） | 同一逻辑时间贯通衰减/刷新/租约/来源筛选；原15项0→15通过；63项新增，扩大1047/36子测试、最终281/2子测试及全静态门通过，九文件owned Any零诊断 | [生产接线与边界](sleep-maintenance-time-production-20261117.md) |
| Sleep维护周期取时基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 15 cell全有效，恒定复验全部相同；原前跳/后跳各15差异，影响权重、TTL、租约及来源过期筛选；后续显式时间闭合本矩阵，原负结果保留 | [冻结矩阵与结果](sleep-maintenance-clock-baseline-20261117.md) |
| 重放历史时间校验 2026-11-17 | `PRODUCTION_MERGED`（限定历史时间/原子重建） | 原12项3→12通过；坏时间默认拒绝，保留原日志及事务前投影；71项新增，候选211、最终523/36子测试及全静态门通过，六受检文件额外Any零诊断；原扩大1194/1夹具失败保留 | [生产修复与失败归属](replay-timestamp-validation-production-20261117.md) |
| 固定日志重建的隐含时间基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 12 cell全有效，原3通过/9时钟相关失败；另一个时钟夹具通过；前两轮夹具scope/控制断言错误保留且不计分；后续历史时间校验闭合本矩阵 | [冻结矩阵与失败归属](replay-clock-boundary-baseline-20261117.md) |
| observation 兼容消费视图 2026-11-17 | `PRODUCTION_MERGED`（迁移后重新执行的查询） | schema9 默认保留原始历史，旧名原生只读且受窗口约束；原reader15项13→15通过、旧writer42通过，另19项验收；候选273、扩大1122/136子测试、全静态门及16个owned Any审计通过 | [生产修复与失败归属](observation-consumption-view-production-20261117.md) |
| 旧 observation reader 容量窗口基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 真实 schema7 reader 跨当前升级；15 cell 全有效，原13通过、2个capacity项目/会话读取并渲染旧记录；后续兼容视图闭合本矩阵；原始测量保留，不证明实际模型发送 | [冻结矩阵、归属及结果](legacy-observation-reader-baseline-20261117.md) |
| 原始事件物理删除防护 2026-11-17 | `PRODUCTION_MERGED`（限定破坏性旧写口） | 默认构造安装三表DELETE约束；原42项14→42通过，另13项验收、扩大1088项/136子测试、测试类型修正后13项复验及完整静态门通过，四个owned文件额外Any审计零诊断；不隔离旧reader | [生产修复与边界](history-delete-guards-production-20261117.md) |
| 混版本warm owner历史保留基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 真实Git schema7 owner先启动再升级同库；42项全部有效，原14项旧schema正对照通过、28项升级处理在最后历史比较失败；后续物理删除防护见上行 | [冻结矩阵与结果](mixed-schema-history-baseline-20261117.md) |
| 普通reset历史保留 2026-11-17 | `PRODUCTION_MERGED`（正常单版本runtime） | 原63项21→63通过，另50项恢复/容量/回滚/新输入/开关/向量交错全部通过；扩大906项/136子测试、import格式修正后125项复验及完整静态门通过，六个owned文件额外Any审计零诊断 | [生产修复与失败归属](reset-history-retention-production-20261117.md) |
| 普通reset历史保留基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 63项全部有效，原21项只读通过、42项reset物理删除原始历史；未观察到本次重开/重建复活；后续生产修复见上行 | [冻结矩阵与结果](reset-history-retention-baseline-20261117.md) |
| 容量检查点与历史保留 2026-11-17 | `PRODUCTION_MERGED`（单版本runtime/冷启动升级） | 原78项48→78通过；原日志保留，schema8有界重放/检索窗口及独立确认，Sleep按count/bytes推进窗口不复制旧report；55项验收、扩大776项/136子测试、最后SQL复查后366项复验及完整静态门通过，13个owned文件额外Any审计零诊断 | [生产修复与失败归属](capacity-history-retention-production-20261117.md) |
| 容量压力下的历史保留基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果，后续限定闭合） | 真实阈值共78项有效，原48通过/30历史丢失；39个低压力对照全通过，保留原测量，后续生产修复见上行 | [冻结矩阵与负结果](capacity-history-retention-baseline-20261117.md) |
| 普通 scope 删除的历史保留 2026-11-17 | `PRODUCTION_MERGED`（低于compaction阈值） | 原63项39→63通过；追加领域删除事件、保留observation事实并退役过滤、同事务清派生向量及写回/检索复查；86项验收，独立ID修改前853项扩大回归/141子测试、最终源码454项复验/133子测试及完整静态门通过；九个owned文件额外Any审计零诊断 | [生产修复与失败归属](scope-history-retention-production-20261117.md) / [原始负结果](scope-history-retention-baseline-20261117.md) |
| 升级旧投影的消费者准入 2026-11-17 | `PRODUCTION_MERGED`（默认reader/claim） | 原24项8→24通过；未知/false/退役来源在排序与容量裁剪前过滤，原始审计记录保留；47项验收、553项相关回归及8个subtest、最终50项复验、完整静态门通过，五个owned文件额外Any审计零诊断 | [生产修复及夹具失败归属](legacy-projection-admission-production-20261117.md) / [原负结果](legacy-projection-admission-baseline-20261117.md) |
| 删除操作发现与显式恢复 2026-11-17 | `PRODUCTION_MERGED`（普通 summary/export/action） | 默认有界分页，12个响应前硬退出案例可从普通入口找回原ID并恢复；completed不重复删、blocked不自动重试；329项相关回归、类型收窄后50项复验及完整静态门通过，四个owned文件Any审计零诊断 | [生产接线与限制](deletion-manifest-discovery-production-20261117.md) |
| 删除响应丢失后的操作发现基线 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果） | 三个真实硬退出窗口均无法通过普通 export 发现原操作；后续生产发现入口已闭合本缺口 | [冻结复现与边界](deletion-manifest-discovery-baseline-20261117.md) |
| Manifest 默认生产调度 2026-11-17 | `PRODUCTION_MERGED`（普通 API/CLI） | delete/reset 默认持久调度；同 ID 重试跳过 completed、blocked 保留；新增 34 项含真实 CLI/认证 HTTP；扩大回归 741 passed / 141 subtests，类型收窄后 123 项复验，完整静态门与 owned Any 审计通过；完整 retention/升级输入仍缺 | [接线、失败归属、恢复及限制](deletion-manifest-production-wiring-20261117.md) |
| 图精确替换与混合删除 2026-11-17 | `PRODUCTION_MERGED`（限定 graph owner） | 同一 36-cell 矩阵 28→36 通过；保留完全未变节点，变化节点及边继续拒绝退役来源；新增 23 项，相关回归 675 passed / 141 subtests；完整静态门通过；默认调度随后接线见上行 | [实现、复验与限制](graph-exact-replacement-20261117.md) |
| 不同 Manifest 操作混合生命周期 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果） | 36 cell 全有效，28 通过、8 个不同项目交错失败；图整体重插退役保留节点导致 blocked；原始证据保留，后续 graph 修复闭合本矩阵 | [冻结矩阵、探针修订与结果](deletion-manifest-mixed-lifecycle-20261117.md) |
| Manifest 原子删除/确认 2026-11-17 | `EXPERIMENT_COMPLETE`（历史控制候选） | 同库 pending 复查、mutation/ack 同事务；68 项新增验收含七 store 故障、同操作两进程和三窗口硬退出；652 项相关回归及全树静态门通过；后续普通控制面已接线 | [实现、失败归属与限制](deletion-manifest-atomic-mutation-20261117.md) |
| 同一 Manifest 操作并发恢复 2026-11-17 | `EXPERIMENT_COMPLETE`（原始负结果） | 24 cell 全有效；8 个顺序重试通过，16 个并发失败；8 个误删 completed 后新输入，6 个事件冲突；原始证据保留，后续原子性与默认调度另有验收 | [冻结矩阵与结果](deletion-manifest-same-operation-20261117.md) |
| canonical 输入接收结果 2026-11-17 | `PRODUCTION_MERGED`（限定正常应用 runtime） | 原始 canonical commit 保存应用 receipt；实际 observation/run-source 写口与新消费者默认检查；31 cell 全有效，双故障开启删除旧输入 12→0；最终 296 项 Ghost 与 48 项恢复/启动复验通过，全树静态门通过；完整 0.6.3 仍缺 | [生产接线与失败归属](canonical-input-receipt-20261117.md) |
| 接收登记与隔离双故障 2026-11-17 | `EXPERIMENT_COMPLETE`（负结果，后续限定闭合） | 31 cell 全有效；基线双故障开启并删除的 12 cell 全部重学旧输入；原始负结果保留，后续 canonical receipt 修复正常应用 runtime 的本次矩阵 | [矩阵、报告与限制](intake-double-fault-20261117.md) |
| 输入接收登记与删除接线 2026-11-17 | `PRODUCTION_MERGED`（限定新接收输入） | canonical task commit 前默认登记；原 8 cell 开启状态旧输入 4→0，27 项验收覆盖 busy queue、重启、崩溃、普通提交与 headless；全树静态门通过；升级前无 receipt、双重故障与完整 manifest 仍缺 | [生产接线与回归归属](received-input-admission-wiring-20261117.md) |
| 已接收未执行输入的删除 2026-11-17 | `EXPERIMENT_COMPLETE`（负结果，后续限定闭合） | InputQueue placed/pending intake 后删除的原 8 cell 全有效，开启的 4 cell 重学；基线保留，后续接收登记关闭本次可登记新输入的缺口 | [有效矩阵与无效 fixture](received-input-deletion-20261117.md) |
| 首次投影前输入身份 2026-11-17 | `PRODUCTION_MERGED`（限定执行入口） | 相同 8 cell 中开启状态旧输入 4→0，48 项新验收与最终 162 项相关回归通过；schema 6 输入登记、持久退役与实际 Sleep 默认接线，全树静态门通过；当时未覆盖的 intake 见后续接收登记 | [输入身份与边界](inflight-input-origins-20261117.md) |
| 首次投影前在途删除与 Sleep 来源控制 2026-11-17 | `PRODUCTION_MERGED`（仅 Sleep 来源控制） | 本轮关闭的 4 个真实任务 cell 旧输入泄漏 4→0，139 项相关回归与全树静态门通过；当时开启的 4 cell 负结果由后续执行入口身份实验闭合，排队 intake 等边界仍缺 | [复现、修复与限制](inflight-deletion-source-controls-20261117.md) |
| 删除后重新读取来源 2026-11-17 | `PRODUCTION_MERGED`（限定来源退役） | 初始 12 项旧来源复活已闭合；continuity/work queue 来源身份默认持久退役；新 run、新手工确认和其他 scope 可用；本轮 425 项相关回归与完整静态检查通过；无投影输入见后续限定执行入口实验 | [来源生命周期与候选负结果](deleted-source-lifetime-20261117.md) |
| 控制面删除与重置失败汇总 2026-11-17 | `PRODUCTION_MERGED`（限定结果表达） | 七 store × delete/reset 的 False 与 SQLite 失败已闭合；合法 0/None 与用户关闭保留；231 项相关回归和完整静态检查通过，durable manifest 调度仍未完成 | [负结果与修复](control-mutation-failure-20261117.md) |
| 派生投影与删除交错 2026-11-17 | `PRODUCTION_MERGED`（限定投影 fence） | 修正 scope 读取后的 32 cell 基线复活 18、生产修复 0；四个派生 writer 与 continuity replay 默认生效；357 项相关回归与完整静态检查通过；Git 对象复现不复制 worktree | [矩阵、scorer 修正与验收](projection-delete-interleaving-20261117.md) |
| 删除与旧 writer 交错 2026-11-17 | `PRODUCTION_MERGED`（限定输入 fence） | 基线 16 cell 中 8 次删除后复活；生产候选/run 身份作废 gate 后复活 0；284 项相关回归通过，完整 manifest 调度和混合生命周期仍待完成 | [冻结矩阵、负结果及修复](deletion-runtime-acceptance-20261117.md) |
| 已验证中文偏好生产启用 2026-11-17 | `PRODUCTION_MERGED`（受测模型与普通 Chat） | 中文 typed field、UI、语言优先与真实发送前复查已接线并默认启用；沿用既有单模型正结果，新增实机批次按用户要求停止；已逐项核对其他实验消费者 | [启用与遗漏核对](language-production-enablement-20261117.md) |
| 历史 memory-routing 归档 | `EXPERIMENT_COMPLETE` | 脚本、脱敏测量、报告和来源摘要已提交；这是实验基线，不是生产晋级 | [实验目录](../../tests/manual/ghost_memory_routing/README.md) |
| 0.6.0 评估冻结与所有权审计 | `EXPERIMENT_COMPLETE` | modelA 单模型 pilot 的矩阵、完整性规则、rank 映射和 admission 所有权已冻结；不改变默认 prompt | [进度记录](frozen-evaluation-ownership-audit.md) |
| 0.6.1 Context Epoch admission | `INCOMPLETE` | 请求 scope、过期和首轮后撤回已在当前 Attempt Gate 上完成 loopback HTTP 拒绝；完整 provider/send 矩阵、repair/resume/compaction 边界和 live/off 对照仍缺口 | [进度记录](context-epoch-admission-wiring.md) |
| Provider Send Scope Authority | `PRODUCTION_MERGED` | `3d013af1` 修复候选自授权、混合 scope 误拒绝与长身份截断别名；有效 on/off 请求 payload 不变 | [子实验](provider-send-scope-authority.md) |
| Provider History Expiry | `EXPERIMENT_COMPLETE`（负结果已由当前 Attempt Gate 闭合） | `bf7f1b8f` 的旧实现负结果保留；`3d025cce` 在当前 commit 上以 text/native × 过期/不过期 4 cell 验证，过期请求在 HTTP 前拒绝 | [负结果与闭合记录](provider-history-expiry.md) |
| Scoped Control Failure Closure | `PRODUCTION_MERGED` | `34e201dc` 移除 unscoped/learning fallback；缺 authority 不学不写，settlement 尊重项目/会话 deny；实际 entry→HTTP on/off/error 与扩展回归已验证 | [修复与验证](scoped-control-failure-closure.md) |
| Provider Control Revocation | `EXPERIMENT_COMPLETE`（负结果已由当前 Attempt Gate 闭合） | `c39f0897` 的旧实现负结果保留；当前 commit 上 text/native × 开启/初始关闭/首轮后关闭 6 cell 均验证，撤回后第二次请求在 HTTP 前拒绝 | [负结果与闭合记录](provider-control-revocation.md) |
| Final Payload Admission Placement | `EXPERIMENT_ONLY` / `INCOMPLETE` | 60 项定位 lease/retry/辅助发送时隙；`3b04b8a6` 的独立 66 项证明显式 JSON 表示可修复编码误拒，但 history-only 摘要仍缺来源继承，暂不合并 | [宿主实验](final-payload-admission-placement.md) |
| Admission Refusal Settlement | `EXPERIMENT_ONLY`（历史负结果；同run native恢复后续限定闭合） | `b1a78723`原8项保留；现有生产gate已采用RequestPrepError，后续36项真实HTTP/SQLite组合验收证明成功/错误回执暖冷恢复；跨run/完整App等仍缺 | [历史拒绝](admission-refusal-settlement.md) / [当前恢复验收](native-refusal-recovery-20261117.md) |
| Protocol Source Lifetime | `EXPERIMENT_ONLY` / `INCOMPLETE` | `603f01f` / `36566995`：真实 task/restart 证明 hint 不在 durable context；去掉重复 export 的消融将 reopen 撤回泄漏 2/2→0/2，warm 仍 2/2；checkpoint 语义待验，不加第二套 journal | [归属、消融与复现](protocol-source-lifetime.md) |
| Canonical Checkpoint Unification | `EXPERIMENT_ONLY` / `INCOMPLETE` | `ec89d516` / `0760ed08`：48 cell 证明仅删 export 在窄预算会丢事实；canonical-only 摘要→既有 context_edit 保留事实且无撤回泄漏，生产事务/消费者待接 | [统一实验与生产边界](canonical-checkpoint-unification.md) |
| Canonical Context Source Commit | `EXPERIMENT_ONLY` / `INCOMPLETE` | 隔离候选补齐 head + revision 来源事务与顺序折叠；42 cell 否定仅 revision 的删除重建安全，扩大回归 648 passed / 5 既有 skip；默认 compaction / warm / attempt gate 仍待接 | [候选源码、负结果与复现](canonical-context-source-commit.md) |
| Canonical Context Production Wiring | `INCOMPLETE`（生产代码已在本地 master） | `3d025cce`：durable context_edit、Responses 来源映射、cutoff folding、唯一 physical Attempt Gate、dispatch binding 已进入本地 master；代码验证通过，但完整 0.6.1 acceptance 仍缺 | [生产接线与边界](canonical-context-production-wiring.md) |
| Attempt Gate history / revocation closure | `EXPERIMENT_COMPLETE`（限定边界） | 当前 Attempt Gate 在 4 项 expiry 与 6 项撤回 loopback 对照中均于 HTTP 前闭合；完整 0.6.1/0.6.2 仍未完成 | [实验记录](attempt-gate-history-revocation-closure.md) |
| Attempt Gate lifecycle acceptance | `EXPERIMENT_COMPLETE` | 生产 ApiProvider 的 native follow-up、错误 receipt、恢复上下文、compaction auxiliary 和 terminal receipt 均验证统一 gate 边界；仍是 fixture 离线证据 | [实验记录](attempt-gate-lifecycle-acceptance.md) |
| Language Live Evaluation 2026-10-21 | `EXPERIMENT_COMPLETE`（受测中文子集已生产启用） | modelA/config 的 language on/off 为 20/20 对 0/20；2026-11-17 接入普通 Chat 并默认启用，无需第二模型 | [实验记录](language-live-evaluation-20261021.md) / [生产启用](language-production-enablement-20261117.md) |
| 0.6.2 scope 与四个开关 | `INCOMPLETE`（控制及 Manifest 已生产接线） | 四开关默认接线，显式关闭仍生效；delete/reset 已默认持久调度；完整维护撤回、生命周期及 acceptance battery 仍缺 | [进度记录](independent-controls.md) / [默认调度](deletion-manifest-production-wiring-20261117.md) |
| Deletion Manifest Candidate | `EXPERIMENT_COMPLETE`（历史候选，后续已接线） | 持久化、七 store 失败恢复与冷启动原始证据保留；后续原子性、混合矩阵和普通 API/CLI 默认调度已完成，完整 retention/升级输入仍缺 | [候选记录](deletion-manifest-candidate.md) / [默认调度](deletion-manifest-production-wiring-20261117.md) |
| Research parity state contract | `PRODUCTION_MERGED`（state 契约） | 核对确认 research 生产依赖已显式携带 state；缺失 Ghost control fail closed，无待开的实验开关；不改变默认 prompt | [进度记录](research-state-contract.md) |
| 0.6.3 冲突生命周期与持久删除 | `INCOMPLETE`（含 observation 兼容消费隔离） | 默认删除调度/恢复、来源退役与旧投影准入、删除/reset历史保留、容量窗口、原历史DELETE约束和schema9 observation只读消费视图已生效；其他领域旧reader/旧快照及内存、其他事务、磁盘retention/purge、其余身份/scope异常及完整生命周期仍缺 | [兼容消费视图](observation-consumption-view-production-20261117.md) / [物理删除防护](history-delete-guards-production-20261117.md) / [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.4 确定性 replay 与显式时间 | `INCOMPLETE`（含控制 payload 与受测裁剪边界） | Affinity 坏时间拒绝、Sleep 显式时间、continuity 过期与 Hebbian 坏事件/内部时间/三个控制 payload 拒绝、图语义 tie 和受测 node/degree 裁剪事实已接线；任意旧历史/更新组合、其他维护入口/排序/digest及其余领域重放仍缺 | [裁剪边界](graph-capacity-scope-replay-20261118.md) / [控制 payload](hebbian-control-replay-20261118.md) / [图 tie](graph-tie-replay-20261118.md) / [内部时间](hebbian-row-time-20261117.md) / [Hebbian 重建](hebbian-replay-integrity-20261117.md) / [过期接线](continuity-expiry-production-20261117.md) / [显式时间](sleep-maintenance-time-production-20261117.md) |
| 0.6.5 类型化 language 与最小遥测 | `INCOMPLETE`（中文子集已接线） | 中文 typed field、UI 与模型/配置拒绝诊断已生产接线；其余遥测计划不作为本次已验证子集启用前置 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.6 生产 shadow parity 与排位实验 | `INCOMPLETE`（已有生产发送回归） | 真实 entry/HTTP 验证通过；新增完整实机与排位矩阵未完成，按用户要求不再补跑 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.7 language 有条件启用与回滚 | `INCOMPLETE`（受测中文子集已默认启用） | Gemma 12B/受测预算的普通 Chat 已启用并可关闭、删除和回退；不代表完整规划项完成 | [生产启用](language-production-enablement-20261117.md) |
| 0.6.8 command/verification intent shadow | `NOT STARTED` | 先找正确宿主，不默认投 directive | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.9 provider outcome canonical event | `NOT STARTED` | 只接真实 send/repair/settlement 的 bounded outcome | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.10 correction/repair projection shadow | `NOT STARTED` | 未验证 outcome 只能是 hypothesis/unjudged | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.11 Ghost Explain | `NOT STARTED` | 解释选择过程，不重新计算、不产生 evidence | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.6.12 World Model review/shadow context | `INCOMPLETE`（首个实机消费者候选负结果） | 只读预测复盘原型与 40 次实机评估已完成；受测接口因格式无净收益而终止，尚无已验证生产消费者，不改默认 prompt | [实机负结果](world-model-verification-review-20261118.md) / [研究建议](world-model-shadow-proposal.md) |
| 0.7 留出任务与可选模型扩展 | `NOT STARTED` | 逐模型独立验收；第二模型为可选扩展，不阻塞已通过单模型的生产启用 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.8 Protocol Adaptation | `NOT STARTED` | 独立下游轨道，不扩大 Ghost 权限 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |
| 0.9+ 可塑性研究 | `NOT STARTED` | 独立 research benchmark，可随时终止 | [Roadmap](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量) |

## 更新规则

- 只更新事实：commit、测试命令、报告路径、模型/config、样本量、结果和限制。
- 同事可以直接提交主分支；实验 commit、报告 commit 和生产 commit 必须在提交说明中区分。
- `EXPERIMENT_COMPLETE` 必须有实验记录、报告、复现命令和实验 commit；它不自动产生生产合并。
- 只有满足交接说明中的生产合并门槛，才能使用 `PRODUCTION_MERGED`。
- 生产合并门槛只要求一个目标模型及配置；未测试的其他模型不算该模型的缺失数据，也不将其限制为试点或 `SHADOW_ONLY`。部署范围必须注明模型、任务和配置。
- 目标模型缺失、缺对照、缺样本或 KoboldCpp 故障时使用 `INCOMPLETE` / `BLOCKED`，不能用 `EXPERIMENT_COMPLETE`。
- `SHADOW` 是有意不改变默认行为，不等于失败，也不等于生产启用。
- 阴性结果保留；无效实现使用 `EXPERIMENT_ONLY`，如果决定停止一个方向，使用 `ABANDONED` 并写明证据。
- 原始回复、KoboldCpp 输出、state 和完整日志只留本地归档；仓库保存脚本、报告、脱敏指标、fixture 和负结果。
- 研究验证优先实验必需测试、受影响生产入口和适度扩展回归；按用户 2026-10-21 最新要求，
  不把每轮全量 pytest 设为研究前置。发现失败仍保留证据、区分 fixture/production，不放宽门槛。

## 实验记录模板

复制下面内容到 `docs/ghost_progress/<experiment-id>.md`：

```markdown
# Ghost <experiment-id> 进度记录

- 实验名称：
- 状态：
- 负责人：
- 开始日期：
- 最后更新：
- 关联 Roadmap 验收门（可选）：
- 实验 commit：
- 生产合并结论：EXPERIMENT_ONLY / SHADOW_ONLY / PRODUCTION_MERGED

## 本次目标

## 实际改动

## 验收命令与结果

## 实验配置

## 结果与限制

## 已知问题和回滚方式

## 下一步
```
