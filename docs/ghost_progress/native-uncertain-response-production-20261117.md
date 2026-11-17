# Ghost 原生回执不完整 HTTP 响应生产修复与暂停交接

- 状态：`PRODUCTION_MERGED`（普通JSON generation默认路径；本批已完成，按用户要求暂停）
- 日期：2026-11-17
- 关联：0.6.1 / 0.6.2
- 原始实验 commit：`c9487a65b88c25c34998122bfac5307a5ec70ec0`
- 生产修复 commit：`36e97ea4078c3930c26e46139d0d8be757623f50`
- 原始测量：[72项冻结基线](native-uncertain-response-baseline-20261117.md)，48通过、24生产失败。
- 用户要求：本批处理完后暂停，维护交接文档并commit，不启动下一批。

## 问题与生产改动

真实服务端已接收原生工具回执，返回合法最终done JSON，却在Content-Length声明的字节
尚未收齐时关闭连接。普通JSON分支用read1读到EOF后直接解析，导致两协议/三scope/
成功错误回执/暖冷共24项均提前completed/done/delivery=success。

api_generation.generate的非SSE分支现在在解码与on_event前检查声明帧长度，长度不一致
抛GenerationUnknownError。现有kernel把它结算为error/MAYBE_SENT；现有reducer保持
external_outcome_unknown，禁止自动重发及重新执行工具。没有放宽NOT_SENT准入，
没有第二gate或手工伪造settlement；原始回执和未确认批次继续保留。

普通ApiProvider的Completions/Responses JSON发送直接消费该逻辑，包含共享kernel与
生产API/CLI上层入口。没有新开关、模块、迁移或额外模型调用；正常默认入口直接生效。
SSE仍以已有终止事件契约结束，不要求等待HTTP EOF。

本轮整个api_generation.py清除原有显式/隐式Any与cast：观察回调参数具体化，
响应context/headers、stream factory与错误filter使用声明的Protocol；外部JSON验证
收窄为JsonObject。http_session作为不透明传输句柄向既有owner转交，没有动态访问。
保留原有外部双参数transport兼容契约，只有unexpected keyword argument才允许该兼容路径。

## 实验和验证

沿用冻结72项与上一轮36项明确未发送/正常投递对照，不使用新的模型行为样本。
每个fault cell验证两次真实HTTP、工具执行一次、原回执保留、MAYBE_SENT、未确认批次唯一
attempt，控制关闭与重新开启后的两次暖冷恢复均不发送。

原始72项48通过/24失败，修复后72/72通过；39文件扩大回归417项全通过。
定向mypy首先发现两个分支的declared_length名称重复声明，已区分为frame_length；
扩大回归后的owned Pyrefly发现filter已返回str后的冗余str转换，已删除。
最终源码7文件155项全通过，零失败/错误/跳过，包含72项原矩阵及上一轮36项正/NOT_SENT对照；
417与155是不同源码阶段，不相加。没有放宽scorer、删除断言或跳过失败。

最终全树Ruff、mypy、Pyrefly、ty及precision gate通过；两个修改文件三个类型检查通过，
owned Pyrefly无警告，额外Any审计零owned诊断。额外审计exit1仅来自未修改依赖的
5871项/229文件诊断；未豁免或修改检查范围。生产Pyrefly保持0 errors、35既有suppressed、
158 warnings，不表示整个仓库无Any。各runtime批次均冻结并验证全部702个生产文件。
完整检查命令、两个最终源码SHA256和逐cell状态见[脱敏报告](../../tests/manual/ghost_memory_routing/reports/native-uncertain-response-production-20261117.json)。

```powershell
python -m pytest tests/test_ghost_native_uncertain_response.py tests/test_ghost_native_refusal_recovery.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy codey/providers/api_generation.py tests/test_ghost_native_uncertain_response.py --python-version 3.11
pyrefly check codey/providers/api_generation.py tests/test_ghost_native_uncertain_response.py --python-version 3.11
ty check codey/providers/api_generation.py tests/test_ghost_native_uncertain_response.py --python-version 3.11
python -m mypy codey/providers/api_generation.py tests/test_ghost_native_uncertain_response.py --python-version 3.11 --disallow-any-expr
```

本地完整日志/XML、每批702个生产文件SHA256冻结及检查明细：
.e2e-artifacts/ghost-native-uncertain-response-20261117。仓库仅提交测试、脱敏结果和文档。
原始失败RAM测试状态另归档至该目录的baseline-state；最终通过的测试临时目录由pytest正常清理。
本次不运行全量pytest，不重跑已停止/api/run 20/20/12模型批次。

## 暂停交接

本批验证、文档和生产修复提交已完成，按用户要求停下。所有本批测试/检查进程已退出，无待轮询句柄。
继续工作须由用户重新启动；不自动展开下一批。没有新增模块，无需runtime发布清单更新。

接班先读本文、[进度板](README.md)、[路线图](../../ROADMAP.zh-CN.md#06-之后的可执行路线一个版本只推进一个可验证增量)、
[团队交接](../ghost_handoff.zh-CN.md)及[World Model提案](world-model-shadow-proposal.md)。
核对git status和最新提交，保护既有tar.gz删除与未跟踪codey-uiux-interactive.html。

本轮限定证明共享生产kernel的真实HTTP响应完整性与同run回执恢复边界。完整App恢复、
硬退出、跨run、repair/compaction、并发scope及其余G0验收仍未整体闭合。
World Model仍只有提案，没有真实event→prediction→review→projection→consumer→评估闭环。
下一位在用户恢复任务后从真实未完成依赖继续；不要重做已有唯一gate、NOT_SENT续传、
明确撤回暖冷恢复36项或本次72项。继续遵循单模型有收益且边界通过就正式默认启用、
每个实验/接入独立commit、同步文档、修改文件零Any和相关回归/全树静态门，不运行全量pytest。

回滚生产修复会重新暴露24项HTTP帧缺口；排障优先保留MAYBE_SENT并让服务端恢复，
不能通过把不确定结果改成NOT_SENT或反复续传来绕过边界。
