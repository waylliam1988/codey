# Research 局部工作流复查后的分批修复与验证

基线：`ef3904f5`。继续使用同一个 kernel、原计划和结算回执、原 KnowledgeStore 与唯一接受后的 FindingReport。本文区分协议/恢复验证与人工内容验收；不把工具执行成功当作语义正确。

## 第一批：修复类型边界

为 ResearchIssue 的既有序列化结构声明 TypedDict；冻结 scope 逐字段编码、校验后显式构造，保留 v1/v2 格式。可空计划在调用边界明确拒绝，控制层复用已收窄的计划变量；补齐消息与预算容器类型。没有新增忽略规则或弱化类型检查。

验证：Mypy、Pyrefly、ty 的 Python 3.11 全生产树检查全部通过。局部 scope、关系目标、报告要求、实际请求投影、provider 回执五个相关测试文件共 73 项通过（115.06 秒），包含旧格式往返、非法字段、缺计划和进程中断恢复；ruff 与 diff 检查通过。

实机：原 12B、原 writer_limit 材料与 seed 74，经生产入口得到 done，回答包含单写者及唯一 WAL 文件原因。原件：`.e2e-artifacts/research-local-followup-20261101/batch1-writer-smoke/`。本次是类型修复后的冒烟，尚未固定时钟，不用于与历史结果作因果比较。

## 第二批：按实际观察时间选择候选

在原回执投影中按 observed_at 排序后取四条候选，显式重新读取早期 finding 会刷新可选资格；修复阶段原选择继续独立保留。新增 fN、object_id、note_id 三种读取身份与 JSON/native 两种冻结声明的六组边界回归，检查恢复后的 schema、协议归一化、真实执行提交及精确版本。工作提示明确已有完整候选时允许直接提交。

验证：8 项直接边界回归通过；阅读恢复、引用修复、选择、归属、上下文及请求投影共 60 项通过（15.72 秒）；补充提示与实机探针后，局部 scope（排除本批未改动的进程退出组合）、请求投影、fixture 共 40 项通过、8 项退出组合取消选择（38.15 秒）。三套 Python 3.11 类型检查、ruff、diff 检查通过。

实机探针通过原 kernel 的测试 setup 先保存六条候选，再显式读 f1，随后由原 12B 自行生成和选择。两次均 done，内容正确；真实请求中的 f1 已恢复为可选。模型仍另存 f7，补充复用提示后也没有避免这次重复，不能声称已证明复用行为改善。原件：`batch2-reread-smoke/`、`batch2-reread-reuse/`；后者可用原 evidence harness 的 `--candidate-refresh-probe` 重现，脚本准备回执与模型动作分开记录。尚未固定时钟，不作提示收益的因果判断。

## 第三批：持久化同错误修复预算及补齐冷恢复入口

ProgressSnapshot 在第一次有领域错误身份的拒绝时升级为 v2，在原 checkpoint 中记录当前错误 key 与拒绝次数，不再从十六条工具历史推测累计次数。达到既有 limit 后停止；读取、新 finding 和新的反馈字数不能重置该计数。正式校验报告不同错误时开始新周期；无领域身份的旧合同继续使用原重复/周期规则。旧 v1 快照保留原序列化形状，v2 拒绝非法版本、布尔计数、错误身份与计数不匹配等载荷。

新增中断回归首次暴露另一处真实缺陷：从 control_ready 冷恢复直接准备修复请求时，进程内事实尚未重建，下一请求会丢失修复 scope。现于原 retry 准备入口重放既有回执，随后冻结请求。没有新增恢复执行路径或可写研究状态。

验证：计数与恢复首次相关回归 95 项及 40 项子测试通过；补齐恢复入口并扩大至 driver 退出后，105 项及 40 项子测试通过（183.23 秒）。包含每次拒绝之间 0/5/20 次新观察、快照往返、批次重复交付、JSON/native 两协议在第二次拒绝 control_ready 和下一请求 provider_request_ready 的四组真实子进程退出；恢复后按原 limit 停止，接受前不发布，终止后重放不再请求模型。三套 Python 3.11 类型检查、ruff 与 diff 检查通过。

实机：原缺证据题 seed 75 在四次请求后 no_progress，未发布；两次选择同一不支持结论触发重复保护。原件：`batch3-gap-smoke/`，失败保留。这只能验证停止行为，缺口判断仍不合格，不能计作内容通过。新增长修复周期由 scripted provider 验证，未把它冒充模型能力。

## 第四批：固定模型可见时钟及分维度实机验收

时钟使用版本控制中的单一夹具 `tests/fixtures/research_local_clock.txt`，实验只替换原 request_context.clock_context 的返回值，原 provider、kernel、持久化时间和耗时计量保持真实。每次运行在 fixture-control 记录完整字面量与 SHA256；matrix 从实际 admitted_payload 校验每个请求恰有一个相同的时钟观察。错误时钟不回退为主机时间。没有增加生产时钟开关或替代运行时。

原 rubric 不变，matrix 保存 rubric 原字节快照和 SHA256。每个原始问题分别人工记录 condition_or_relation_omission（条件/关系遗漏）、unsupported_addition（无依据附加结论）、state_misreport（完成/缺口声明错误）、gap_termination（缺证据时的正常结束），不适用项明确记为 not_applicable。状态误报在人工验收时判断，不冒充执行器具备语义认证能力。四维错误可能重叠，不能相加当作失败次数。

离线：固定时钟、两种 API 协议的首轮及工具续接实际载荷、token 计数一致性、原历史日期保留、旧诊断脚本等 28 项通过（4.89 秒）。三套类型检查的 Python 3.11/3.12/3.13 目标全部通过；这不是在三个 Python 解释器上运行 pytest 的声明。

实机原件：`.e2e-artifacts/research-local-followup-20261101/batch4-fixed-minimal/`。仍为原 12B、temperature 0.3、seeds 74/75/76、候选顺序 seed 0、原来源与窗口、12 轮上限，五题共 15 次、38 个真实请求。所有请求时钟核验通过；每题三个 seed 的首轮 messages SHA256 完全一致。未删除服务正常但内容失败的运行。

| 原始题目 | seed 74 | seed 75 | seed 76 |
| --- | --- | --- | --- |
| 单写者及原因 | 通过 | 通过 | 通过 |
| 只读版本及完整 OR 条件 | 通过 | 通过 | 通过 |
| checkpoint 停止及原因 | 漏原因 | 漏原因 | 通过 |
| checkpoint 恢复 | 附加错误停止边界 | 附加错误停止边界 | 把 WAL 与 wal-index 位置混淆 |
| 只有邻近证据 | 错误因果、no_progress | 错误因果、no_progress | 最终 unresolved，但先保存缺口占位并误报 answered |

按原完整问题及附加错误不得放过的标准为 7/15 通过，12 次 done 不等于 12 次内容通过。分维度失败数：遗漏 2、无依据附加 5、状态误报 5、缺证据结束不合格 3；具体依据在 matrix.json 的 semantic_review 中。第 6 步仍未通过，禁止据此推进候选干扰、自主检索、完整 WAL 推广验收或强制切换全部 strict research。后续如重跑四组对照，只是同层诊断。
