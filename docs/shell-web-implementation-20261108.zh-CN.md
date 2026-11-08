# Shell / Web 三批实施进度（2026-11-08）

## 目标与授权

执行用户目标文件 `attachments/b42c336c-14ff-4ef3-bc48-8cc2f5788943/goal-objective.md` 中的三批方案，全部验证后才完成目标。每批独立 commit；本地 Gemma4 实机通过后才使用 Solar Mini4，并在两者验证后进入下一批。不要把完成第一批当成整个目标完成。

用户约束：相关测试优先；不主动全量 pytest、覆盖率或安装包构建；扩大范围需解释原因；执行 Ruff 与类型检查；新增/重构边界使用明确类型、消除所触及的 Any，不新增 Any 或冗余防御；遇到失败先分清生产错误、测试过时和环境原因；UI/UX 遵循 DESIGN.md。

## 实施顺序

### 第一批：结果与网页入口

- [x] 公共、有类型的执行回执；初次显示实际状态、退出码及作用域。
- [x] 审批重建保留截断、执行输出引用；持久化/恢复、原生/兼容协议保持一致。
- [x] 联网意图覆盖“上网看看”及明确打开 URL，保留否定和只读约束。
- [x] 用户指定首页与研究候选筛选分离，涵盖重定向；复用现有获取与提取能力。
- [x] 修正诊断 gate 的契约预期，冻结旧回执基线，区分正常 Shell 语义与产品缺口。
- [x] 相关测试、Ruff、类型检查、本地实机、在线实机。
- [x] 更新报告并提交第一批（本次 commit）。

### 第二批：PowerShell 与编码

- [x] 显式配置 → pwsh → powershell → CMD；保持任务执行环境绑定。
- [x] 原生 argv 与 Shell 退出码明确区分，不擅自改写脚本语义。
- [x] 捕获/实时存储/最终回执统一解码，有限编码回退及正确字符边界。
- [x] 覆盖 PowerShell 内部转换已损失信息的边界，不假称可以从宿主恢复。
- [x] 相关测试、Ruff、类型检查、本地实机、在线实机。
- [x] 更新报告并提交第二批（本次 commit）。

### 第三批：读取、恢复与回答核验

- [ ] 明确展示/存储/采集截断与完整读回能力，复用已有存储。
- [ ] 回执声明可用视图，命令没有 value 时明确提示 output。
- [ ] 绑定回执的结构化执行结论与现有完成检查核对；执行状态 UI 取真实记录。
- [ ] 审核相关工具卡片状态；如需 UI 修改按 DESIGN 实施并验证。
- [ ] 增加恢复、否定联网、首页、两类退出码、回答事实、福州美食来源的验收。
- [ ] 相关测试、Ruff、类型检查、本地实机、在线实机。
- [ ] 更新文档、提交第三批，逐项审计完整目标后结束。

## 当前证据

- 工作区原有四个未跟踪文件均是本会话前期诊断产物：`tools/shell_web_diagnostic_gate.py`、其测试及两份 baseline 报告，保留并随相关提交纳入。
- 基线：`docs/reports/shell-web-baseline-20261108.zh-CN.md`，37 项矩阵完整；不能把 22/37 当作模型成功率。
- `DESIGN.md` 已读：UI 英文、安静且复用现有交互；普通聊天不自动启用 Research 的浏览器来源流程，需检查轻量获取路径。
- 第一批业务修改已落地：公共 `ExecutionReceipt`、有类型审批重建和恢复、普通聊天明确联网意图、指定 URL 的有界 HTTP 获取（含重定向和 PDF）。新增/重构模块无显式 Any。
- 相关回归 91 项通过；全生产树 mypy（654 文件）、Pyrefly、ty 与 kernel precision gate 通过。Ruff 有一个诊断脚本导入顺序待修。
- 本地 Gemma4 实机：CMD/PowerShell 两种生产 HTTP 审批与首次退出码投递通过；明确禁止联网通过。
- 原始站点 `api.lihe.chat` 实际返回 233075 字节 SPA HTML，大型内嵌图标占据开头，静态提取正文为空。首页质量筛选已绕过，失败现为真实 `page_blank`；不可声称已读取网站内容。普通聊天按 DESIGN 不自动启动浏览器。
- 本地首个 open_url 失败的 qN 原因需对照请求参数：`question_ref` 即使非必填，传入不存在的 qN 仍应拒绝，不能简单移除校验。
- 以实际可读 API 站点 `openrouter.ai` 补做同一真实入口的成功获取验证；原始失败证据保留。失败后如实完成的契约列入第三批，不能用静态空页宣称成功。
- 实机证据目录：`.e2e-artifacts/shell-web-batch1-local` 与 `shell-web-batch1-local-readable`。尚未使用在线额度、尚未提交。

## 继续工作位置

第一批验收完成，开始第二批：`shell_discovery.py` 默认顺序、`output_capture.py` 与 `execution_output_store.py` 共用编码/边界契约、原生 run 的 process 作用域。不要改写用户脚本的退出语义。

第二批实施中：Windows 顺序已改为显式配置 → pwsh → powershell → CMD。原生 run 保留参数数组，回执和 canonical 记录 process 作用域；Shell 不追加 exit。捕获优先 UTF-8，仅在无效时回退 Windows 主机代码页，并显示所用编码；不猜测任意字符集。采集与实时快照共用 `BoundedByteCapture`，保留原始字节计数、实际省略字节及编码信息，并迁移旧 SQLite 快照。

发现并修复生产确定性问题：旧观察队列满会丢字节；现在同步有界捕获、异步合并刷新通知，不丢字节。存储失败的 unknown 状态不再被后续 settled 擦除。旧 CMD 测试应显式选择 CMD，跨平台引用测试补 PowerShell 的调用运算符和显式退出，而非改写生产脚本迎合旧测试。

第二批验证：相关捕获/取消/执行测试 126 passed、1 skipped，补充存储/删除/状态/退出作用域回归 59 passed；未运行全套 pytest。Gemma 实机 14/14（离线观测及两种审批）通过；新增宿主实测回执核验通过。Solar 正确复述全部四种退出码及作用域，中文正文正确，但保留了编码说明，与要求的纯正文格式不同；记录 exact_format=false，事实核验=true，基于保存证据重分析，没有为此重发模型请求。生产审批复验进行中。

第二批已验收：Solar 的 PowerShell HTTP 审批与首次回执通过。新增编码 gate 回归与相关用例 19 项通过；最终全树 mypy/Pyrefly/ty、Ruff 和 precision gate 通过。汇总见 `docs/reports/shell-web-batch2-20261108.json`。当前无 UI 外观变化。

下一步第三批：输出恢复边界与 available views；done 结构化 execution_claims 绑定 result_ref 并接入现有完成检查；失败网页用 source_gaps 绑定真实失败回执完成，防止空页导致无限重试；审核状态卡片并添加福州美食真实来源验收。仍先本地后在线，第三批提交后逐项核对全目标。

第一批最终记录：相关旧回归 46 项、新增/诊断回归 46 项通过；Ruff、全树 mypy/Pyrefly/ty、kernel precision gate 通过。Gemma 和 Solar 都通过两种 Shell 审批、明确禁止联网和单页首页获取。汇总见 `docs/reports/shell-web-batch1-20261108.json`。

修正先前猜测：本地首次 open_url 传入未注册的 `question_ref=q1`，不是普通聊天强制需要研究计划；现有拒绝正确，模型可恢复，不修改校验。Solar 初次成功获取后反复扩展文档是模型范围控制问题；明确单页任务后两个模型都通过。
