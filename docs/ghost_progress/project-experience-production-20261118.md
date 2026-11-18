# 跨会话项目经历：生产接线验收

日期2026-11-18。状态：`PRODUCTION_MERGED`，当前master本生产提交，受测范围默认启用。
生产提交可用`git log --grep="enable validated project experience continuity"`定位。
[脱敏验收报告](../../tests/manual/ghost_memory_routing/reports/project-experience-production-20261118.json)。
用户可见行为：新会话继续同项目时，能接续此前选项及其记录理由。
不把历史助手的“尚未验证”改成已通过验证，不从经历产生任务授权。

## 收益依据与范围

[冻结结果](project-experience-continuity-result-20261118.md)：20条独立合成经历流、
40次物理请求，on19/20、off0/20，两选项9/10与10/10；严格scorer未改，
额外输入最多280tokens，p95总延迟2547ms对2594ms。不是新增学习机制收益。

默认启用范围为普通`coding_writer`的独立`ghost_experiences`来源：
Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M、loopback HTTP `/v1`、
OpenAI Completions/native tools、temperature0、非流式、output128、
context/reserve/keep_recent=262144/32768/32000，无input_limit覆盖。
没有新增实验开关；符合该配置且retrieve/admit开启即投递。
其他模型、配置、planning/research/reviewer及缺本地状态authority的直接adapter请求回退。
**默认8192等其他输出上限也回退**；本批不把128输出实验推广到它们。
既有中文普通Chat启用保持独立，未扩展语言或其他偏好。

## 实际改动与所有权

- `observation_index`在总字符预算内保留用户/助手两角色，短字段归还预算；
  空间不足时整条跳过，不把完整经历变成只有用户原话。非查询感知摘要。
- 真实项目路径为dispatch→completion flow→writer phase→adapter→shared kernel。
  writer早已按project scope检索；这次补profile和adapter的实际render/source缺口。
  原先把Chat默认session scope归因到该路径是错误的，`task_entry.py`未修改。
- `project_experiences`从workspace state home重新读取独立控制和同项目已提交行，
  复用消费窗口、retired identity与durable输入authority；以有界本地词法检索验证
  原候选逐字仍成立。最多200行、3项、1800字符，不做第二次embedding或生成请求。
- adapter将同一经过校验的经历同时用于渲染和独立ContextSource；发送前通过既有
  physical Attempt Gate再次校验。初始读取故障回退无经历；组装后删除、关闭或
  配置变化拒绝发送，不补发回答。kernel增加可选的实际来源校验回调；恢复更换
  context/sources后必须将实际元组交给adapter验证，不能借新entry另一份合法经历放行。
  其他entry默认无该回调，不改变原始admission规则。
- 为消除该文件已有Any，adapter使用具体TaskPolicy/TaskSession及policy输入数据类，
  completion context在边界验证字符串键；保留写权限与共享session授权校验。
- `GhostInboxStore.control_enabled`将底层SQLite读故障规范化为已有Ghost设置错误，
  正常项目结算按关闭处理；不扩大为整个App数据库故障恢复。
  三个kernel文件的类型清理限于persisted result键值边界及具体ToolCall.call_id读取。

## 验收与失败归属

本地证据：`.e2e-artifacts/ghost-experience-role-budget-20261118/`。
原始回复、wire、完整日志及state不入仓。

原22格基线在夹具修正后9失败/13通过；双角色修复后21通过/1失败，剩余是实际
跨会话项目遗漏。接线首轮37项通过（原22格加15项实验契约），未删失败断言。
新增边界的第一次种子直接写observation，被既有durable凭据契约拒绝；改为真实
App/InputQueue完成种子。第二次夹具误设只读context_budget、误用Chat终态及在整个
App注入原始SQLite故障；按实际接口修正预算字段、保留项目`error`终态和HTTP拒绝
断言，读故障限定到新消费者句柄。第三次17项全部通过。

最终补observation读故障、真实inbox设置查询故障及旧request来源恢复注入。
后续复查冻结“不同但都非空”的新entry/旧恢复来源：旧布尔闭包基线1格失败，旧内容
实际发送并done；修复为实际来源元组回调后原4拒绝格通过，另补同来源成功对照。
恢复注入只模拟来源在新entry组装后恢复，kernel与物理gate真实执行；不冒充完整App冷恢复。
完整native receipt/暖冷恢复和App crash恢复复用受影响的既有回归。
在实际来源回调之前，扩大25文件442项/19子测试通过，随后14文件278项/153子测试通过；
这些是相应阶段的证据，不冒充最终源字节的一次结果。晚期来源回调的19文件回归先出现
271通过/3失败；将7个生产模块从`git show HEAD:<path>`加载到内存复测，同3项失败，
确认不由本批生产改动引入，不复制目录/worktree。

失败归属及修正：shell receipt的`ok`严格布尔拒绝行为有效，更新旧错误消息断言；
native审批恢复夹具误用已经live的ApiProvider，改为每次连接新建provider，再使用真实
worker队列等待原任务恢复。保留原task、两次generation、call id/result、执行次数、
两次task_start、单一durable任务及公开事件断言；任何新任务提交直接使测试失败。
两测试文件15项通过，完整文件补齐类型并在边界验证旧helper输出，不扩修依赖helper。
该次focused检查结束有RAM盘旧SQLite句柄清理警告；随后最终19文件批次无该警告。

最终源字节：相同19文件范围**274 passed**，`consumer-final.log/xml`，500.24秒，无跳过。
没有重跑实机：生产边界和类型整理新增模型请求0，原40请求及scorer保持不变。

复现最终相关范围（不运行全量pytest）：

```powershell
python -m pytest tests/test_project_experience_production.py tests/test_ghost_experience_role_budget.py tests/test_project_experience_continuity_trial.py tests/test_kernel_domain_contracts.py tests/test_provider_wrapper_contracts.py tests/test_provider_attempt_gate.py tests/test_ghost_attempt_gate_lifecycle.py tests/test_ghost_provider_send_boundary.py tests/test_ghost_context_admission.py tests/test_ghost_native_refusal_recovery.py tests/test_kernel_recovery_context_split.py tests/test_research_convergence_compatibility.py tests/test_research_report_snapshot.py tests/test_chat_programmatic_citations.py tests/test_shell_approval_native_identity.py tests/test_chat_shell_approval_preserves_workspace_and_intent.py tests/test_production_kernel_calls_use_typed_request.py tests/test_kernel_dependency_boundary.py tests/test_kernel_type_precision_gate.py -q
```

最后两处仅测试类型调整：contextmanager返回改Generator、text done消息明确字典元素类型；
受影响31项再次通过（`typing-final.log/xml`，105.91秒）。703生产文件与274项时字节一致。
`consumer-final-source-hashes.json`保留原709文件；`release-source-hashes.json`锁定最终709文件。

最终按顺序执行并通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

703生产文件完整类型门通过；Pyrefly的158个未显示警告与35个既有suppressed不作错误清零。
14个owned文件专项Pyrefly零错误（12个警告未显示）、ty通过；mypy与额外
`--disallow-any-expr --disallow-any-explicit`的owned诊断均为0，无新增豁免/cast/ignore。
专项完整命令仍分别返回1：未修改测试依赖8文件269项常规诊断、351文件14107项额外诊断，
没有隐藏或改检查范围；原始输出与按路径分列结果留本地，**不声称全仓Any清零或专项命令全零退出**。
这些未修改依赖属于后续类型审查事项，不扩成本消费者的无界helper清理。

owned范围为8个生产文件（observation_index、inbox、project_adapter、project_experiences、
kernel_contracts、kernel_request_builder、kernel_actions、permissions）及6个测试/脚本
（role_budget、production、continuity_trial、manual continuity、两份shell测试）。
本地`owned-python.txt`、`static-final-2-status.json`与`owned-type-audit-summary.json`保留命令及计数。
`git diff --check`通过；无前端变更、无全量pytest、无worktree或副本。

## 限制、关闭与回滚

只证明合成项目选择/理由的连续性，不代表所有真实用户、规划、流式或其他模型收益。
经历没有新增TTL；消费窗口与持久删除继续由原store所有，continuity TTL另有回归。
没有本地理解自动写回、反馈适应或新增信息World Model完成的结论。
检索重新排序导致候选变化时保守拒绝，不在旧prompt上偷偷换来源。
动态验证额外本地读取的成本未计入原冻结实机p95，不声称该p95是生产重测。

关闭retrieve_projections或admit_hints（user/project/session）停止该scope投递。
撤销coding_writer的ghost_experiences profile项即可回滚新候选，保留全部合法经历；
已组装的旧字节也会在发送前被拒绝。无需迁移数据库或切换worktree。

下一批推进条件化用户理解与可归因反馈，并选择有新增预测信息的World Model消费者。
既有两个重复验证事实的负结果与Hopfield `EXPERIMENT_ONLY`不重跑。
