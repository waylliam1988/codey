# 两项旧失败：根因与修复

基线：55e105a8。只处理压缩异常测试和 Shell HTTP 续跑，不放宽请求超时，不运行全量
pytest，不改变生产上下文预算、时间上下文或超限准入策略。

## 压缩异常测试：测试没有进入要验证的分支

LocalPrepareRequestTests 的默认上下文为 200 token，保留输出 50，输入上限只有 150。
真实请求现已带 request_context 时间观察；本机最小 hello 请求估算 158，已超过 150。
完整请求约 916。ApiProvider._admit_request 先检查不可压缩的当前输入和声明，这是正确的
生产保护；因此测试在调用被 mock 的 CompactionCoordinator.compact 之前就抛出了
ContextOverflowError，无法得到它原本期待的 RequestPrepError。

修复只调整这一测试的构造：上下文 512、保留输出仍为 50，输入上限 462，使最小请求能
放下、历史仍放不下。保留真实时间 renderer 和正式计数器，不伪造计数。增加断言：
最小请求确实能放下、压缩器恰好调用一次、异常保留 RuntimeError 原因、没有 HTTP
发送、原历史不变。其他测试仍使用原来的 200 token 小窗口。

这项修复恢复了故障测试的有效性，没有修改生产压缩算法或绕过超限保护。

## HTTP 续跑：旧派发快照覆盖了已提交的恢复通知

DurableTaskScheduler._reconcile 在 dispatch 返回后读取 current，再取得调度锁更新
_pending/_scheduled。原实现的可重现时序：

1. 派发已接受，调度器读到 RUNNING/WAITING 快照。
2. worker 停在审批处；真实命令执行并提交回执，任务以同一个 ID 恢复到 PENDING。
3. 正式提交观察者 notify(task_id) 清除 scheduled 并加入 pending。
4. 调度器取得锁，把第 1 步的旧快照应用到集合：删除 pending，加入 scheduled。
5. SQLite 中任务仍待运行，但后续 reconcile 把它视为已派发，第二次模型请求不再发生。

这解释了为什么重跑可能通过；在独立重复 8 次、HTTP 文件和相关组合中，也都可能不
触发该窗口。不能靠这些成功次数证明竞态已修复。

新增确定性 SQLite 测试，在读取旧派发快照后用正式 wait/advance/unclaim/notify 重现
交错，修复前失败：数据库为 PENDING，但只派发了一次。进一步对原 HTTP 测试使用调度
屏障，强制后台调度器赢得首次准入，并等真实审批结算提交后才放行旧快照。模型使用
原确定性本地 HTTP peer，命令、审批、worker、SQLite、回执与续跑都是原生产链路。

修复前强制 HTTP 复现：shell_result 已有真实成功回执、最后 task_done 仍为 approval、
busy=false，只有一次模型请求，随后触发原 5 秒未完成检查。没有注入 I/O 失败或替代
执行器。修复后相同屏障、相同超时下，两种原生协议均收到第二次模型请求并完成 done，
每次仍只有一个已结算 Shell 结果；run_shell_case 同时检查 durable tool_result 恰好
一次、原 call/window 身份和重复审批幂等。

生产修复只有原调度器的 8 行差异：派发前消费已有 pending；因此派发期间重新出现的
pending 表示后来的恢复通知。派发后的旧快照不能清掉它，下一轮仍通过 SQLite 的正式
claim_task 竞争新 attempt。没有持锁读取数据库，没有新增 epoch、持久事实、恢复循环
或第二权限/执行系统。测试工具额外保存超时的审批和最后终态，避免只报“未完成”。

## 验证与材料

- 修复前确定性调度测试失败：.e2e-artifacts/context-shell-roots/stale-wakeup-before.xml。
- 修复前强制 HTTP 失败：.e2e-artifacts/context-shell-roots/http-race-before.xml；该目录下
  provider.jsonl、events.json、continuation-timeout.json 保留请求、真实回执和停滞状态。
- 修复后定向组：5 passed、16 deselected，含四种 Zen/协议/交错组合与 SQLite 通知测试；
  材料 race-fixed.xml 及同名目录。
- 请求准备测试类：5 passed；材料 preparation-fixed.xml。
- 调度、所有权、唤醒、取消、HTTP 续跑、请求准备、计数、真实时间和压缩事务相关组合：
  102 passed、4 skipped；材料 related-after.xml。四个跳过来自已有 scheduler ownership
  单元夹具的显式 skip，原因涉及 Windows SQLite 句柄清理和既有服务所有权验证边界；
  没有为本轮修复新增 skip，也没有将这些项计作通过。
- Ruff、mypy（517 源码文件）、Pyrefly、ty 全 codey 源码检查通过，目标 Python 3.11；
  Pyrefly 仍有既有 29 抑制和 80 未展示警告。runtime 描述检查通过。未跑全量 pytest。

本次没有依赖真实大模型的随机行为判断竞态消失，也没有把确定性模型 peer 冒充真实
模型质量评测。Windows 实测不证明其他平台的物理进程特性。
