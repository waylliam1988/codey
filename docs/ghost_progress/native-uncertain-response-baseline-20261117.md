# Ghost 原生回执不确定 HTTP 响应基线

- 状态：`EXPERIMENT_COMPLETE`（原始负结果，后续生产修复限定闭合）
- 日期：2026-11-17
- 关联：0.6.1 / 0.6.2；后续 provider outcome / World Model 的事实边界
- 生产基线：`365b5bfa`（生产源码仍为上一轮已核对的702文件）
- 生产改动：无；先测已有真实HTTP接收与恢复。
- 实验 commit：`c9487a65b88c25c34998122bfac5307a5ec70ec0`

## 冻结目标、对照与停止条件

已有[36项准入拒绝/恢复验收](native-refusal-recovery-20261117.md)证明明确未发送时可续传；
本轮测试相反方向：工具回执请求已经被实际服务端接收，响应丢失或不完整时必须为
error/MAYBE_SENT，保留原工具回执及未确认批次，恢复不得重新执行工具或重发请求。
HTTP声明长度未收齐时，即使已收到字节恰好构成合法JSON，也不能提前接受为完整响应。

72 cell，各一次：两个API协议×三个控制scope×成功/错误工具回执×暖/冷恢复×三个响应故障：

1. 接收请求后直接EOF，尚无响应头；
2. 正常200与完整声明长度，body为畸形JSON；
3. 正常200与合法最终done JSON，但Content-Length比实际发送字节多17，随后关闭连接。

只在第二个实际HTTP（原工具回执follow-up）注入故障；首个原生read_file响应正常。
复用已验证的typed Runtime与固定工具结果回调，调用生产provider/kernel/SQLite/Ghost控制。
主要变量是响应故障；不替换错误分类器、结算、reducer或HTTP返回状态。
正对照沿用同生产源码的上一轮enabled12项（两次HTTP且任务完成）及明确NOT_SENT的暖冷续传24项。

每个cell检查：恰好两次实际HTTP/工具仅一次；最终provider receipt为error/maybe_sent，
批次只有一个未替代attempt；持久工具成功/错误结果保留；实际关闭控制后恢复一次、显式重开后
再恢复一次，两次都应external_outcome_unknown拒绝，HTTP、effect receipts、工具次数不变。
冷恢复每次新建SQLite/provider并加载真实canonical snapshot，不伪造history或receipt。
72项收齐即停止；生产失败与夹具失败分别保存，不将不确定请求重分类为NOT_SENT来获得通过。

## 运行与证据边界

无外部模型服务，model ID为deterministic-native-receipt-fixture；本机隔离HTTP。
每次运行冻结全部codey/**/*.py的数量与SHA256并在结束核对；完整state和日志只留
.e2e-artifacts/ghost-native-uncertain-response-20261117，仓库保留fixture和脱敏报告。
不运行全量pytest，不重跑已停止的/api/run模型A/B。不能据此声称完整App恢复、硬退出、
repair/compaction、跨run或World Model已经实现。

## 结果与下一步

72项有效，48通过、24失败：EOF与畸形JSON的48项均正确MAYBE_SENT并拒绝暖冷重发；
short_frame_valid_json的24项全部错误接受为completed/done/delivery=success。
失败点是生产结果契约，不是夹具或工具审计名。HTTP服务发送的真实Content-Length比实际字节
多17，连接随后关闭；生产read1循环在EOF后直接解析JSON，没有核对声明长度。
全部702个生产Python文件运行前后hash一致。原始失败日志/XML保留本地baseline产物。
先提交原负结果，再单独修复生产JSON分支；本批结束后按用户要求暂停，留下交接入口。

后续[生产修复与暂停交接](native-uncertain-response-production-20261117.md)已把原矩阵闭合为
72/72；扩大417项及最终类型修订后155项通过，全树静态门及两个文件owned Any零诊断。
原48/24测量与日志不改写；本批结束按用户要求暂停，不展开下一批。
