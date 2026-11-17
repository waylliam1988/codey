# 项目审查与 Ghost / World Model 后续工作

- 日期：2026-11-17
- 状态：进行中；不表示全项目语义审查或 Ghost 路线图已完成。
- 工作区：本地 master；不创建、复制 worktree。

## 已覆盖的范围与证据强度

已阅读项目 README、pyproject、CI 工作流、类型检查说明、结构/Runtime 职责地图，
以及 Ghost 路线图、进度索引和相关实验记录。审查重心是 Ghost 的实际生产控制面、
SQLite/record/graph 写口、候选输入与派生来源、删除/reset/rebuild/重启和发送准入边界。
相关生产回归与静态检查结果按各实验记录保存，不能用局部结果代表整个项目。

结构枚举覆盖当前 699 个生产 Python 文件；AST 逐文件解析和 Any 名称盘点记录在本地
`.e2e-artifacts/project-review-20261117/source-inventory.json`。233 个文件出现 Any 名称，
这说明全树 checker 通过不等于全库零 Any；不是 233 个运行时 bug 的证明。
本轮修改文件按用户要求清理，不据此批量改写未审查文件。

source-size inventory 已完成，位于本地
`.e2e-artifacts/ghost-projection-fence-20261117/source-size-inventory.json`。
它用于安排阅读，不把文件长直接认定为应拆分或应删除。

前端有 143 个源码路径，另有 Go 内核、CodeMode JS、发布资源、工具和测试。
这些领域尚未完成逐行为审查；本轮无前端改动，未宣称完成前端构建或全平台验证。
本机 Python 3.11 静态检查不代替 CI 的其他 Python/OS 矩阵。

## 已确证并修复

| 问题 | 当前结果 | 证据 |
|---|---|---|
| 中文偏好有单模型净收益却缺普通 Chat 生产入口 | 受测模型/配置默认启用，显式关闭保留 | `6f0ddf7e`；[生产启用记录](ghost_progress/language-production-enablement-20261117.md) |
| 删除后的旧 candidate/run writer 重写 | 持久输入身份 fence 默认生效；限定 16 cell 复活 8→0 | `17eb2e3e`；[运行时删除实验](ghost_progress/deletion-runtime-acceptance-20261117.md) |
| 来源读取/删除/派生提交交错与 continuity 删除 replay | generation fence 与 replay 默认生效；修正 scorer 后 32 cell 复活 18→0 | `5e291b4f`；[投影删除实验](ghost_progress/projection-delete-interleaving-20261117.md) |
| 普通控制面将 store False 汇总为成功，SQLite 错误逸出 | 逐 store 结构化失败、继续其余 store；合法 0/None 和关闭保留 | `7eded52e`；[失败汇总实验](ghost_progress/control-mutation-failure-20261117.md) |
| 删除后重新读取旧来源生成投影 | continuity/work queue 的持久 source retirement 已验证，生产提交另见记录 | [来源生命周期实验](ghost_progress/deleted-source-lifetime-20261117.md) |
| Sleep 绕过 accept_events 消费外部来源 | 正常后台来源步骤默认复查当前 scoped 控制；关闭的真实任务 cell 泄漏 4→0，139 项回归通过 | [在途删除与来源控制](ghost_progress/inflight-deletion-source-controls-20261117.md) |
| 已进入执行入口但无投影的输入在删除后重学 | 默认登记无正文身份并持久退役，开启的 4 cell 复活 4→0；新身份、其他 scope、进程重开与旧 checkpoint 已验收 | [输入身份与剩余边界](ghost_progress/inflight-input-origins-20261117.md) |
| 方向文档仍把已上线中文偏好称为当前候选 | 区分历史测量与当前生产状态；旧测量不重写 | `7eded52e` |

## 有证据的未完成项

1. **输入寿命的剩余边界**：schema 6 已闭合真实 task entry→删除→结算的 8 cell，
   即使首次投影前删除也不会复活。登记边界为执行生命周期；已接收但尚未进入该入口的
   排队输入、升级前无 receipt 的输入和完整恢复仍未覆盖。已排队维护启动时的 run_maintenance
   复查、source step 控制撤回并发和 knowledge 独立来源矩阵仍待验证。另有两项未改动的
   Research cutover 旧 fixture 契约缺口，按输入身份实验记录列出，未让生产放宽校验。
2. **Deletion Manifest 默认调度**：候选代码和离线恢复有效，但普通控制面仍直接遍历 store。
   当前 partial mutation 无跨 store 回滚或 durable pending 恢复；需完成真实混合并发、
   crash/restart 和 tombstone 生命周期后接入，不能把已存在模块当作上线。
3. **确定性时间与 replay**：continuity/work queue 等仍使用 `_common.now_iso_z()`；
   0.6.4 的显式 maintenance_as_of、稳定投影 digest 和跨墙钟重放未完成。
4. **发送准入的剩余 acceptance**：当前 Attempt Gate 已闭合部分 expiry/revocation 和 lifecycle，
   全部发送、repair/resume/compaction 边界仍按进度索引逐项核对；不重复有效历史实验。
5. **World Model**：仍无已验证的实际 consumer，提案不是完成证据。依赖闭合后必须推进真实
   prediction→review→projection→consumer→评估；不能永久停留在 optional/shadow 描述。
6. **来源身份与成本**：已有字段长度约束、无 source_ref 的输入、永久 retirement 的增长与维护
   尚需专项验证。不能截断后把不同身份当作相同，也不能为省空间丢弃删除边界。
7. **其他 family / telemetry / canonical outcome / repair / Explain**：按进度索引推进；未证明
   收益者不扩大默认投递，已通过的单模型范围完成实际接线并默认启用。

## 待审查领域

app/control 与 durable/scheduler 的并发、取消和授权；operations/runtime/provider/protocol
的请求与收据生命周期；workspace/toolchain/storage 的路径、写入与恢复；research/completion/
knowledge/reviews 的来源与完成证明；automation/launcher/repairs 的生命周期；前端及发布链；
Go/CodeMode 的跨语言契约和运行成本。当前结构阅读和静态门只能用于定位，不能给这些领域
作出“没有损坏、不一致、低效或死代码”的结论。

工作区已有的 sdist 删除和 `codey-uiux-interactive.html` 不属于本轮，保留未处理。
全量 pytest 不运行；后续按每个实际改动补相关生产回归、完整静态检查、文档和独立提交。
