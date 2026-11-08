# 执行、来源与输出事实收敛进度

## 目标与约束

工具产生事实，现有回执保存事实，现有投影恢复事实；提示词、完成检查与 UI 消费事实。
保留 KernelEffectSink、kernel_receipts、kernel_facts、TaskSession、ResearchLedger 与 completion_gate。
不新增任务运行时、回执库或输出服务；不自动重执行、切换解释器或扩大审批读取权限。
保持 shell/run 的既有 ok 语义；保持用户指定输出格式。

按五批分别提交。每批补直接相关测试、Ruff 与类型检查；不主动运行完整 pytest、覆盖率或安装包测试。
修改范围使用明确类型，不增加 Any 或防御性冗余分支。验证失败先查根因，区分旧测试契约与生产缺陷。

## 批次与状态

| 批次 | 内容 | 状态 | 提交 |
|---|---|---|---|
| 1 | 执行回执统一构造与解码，保留 ok 语义 | 已提交 | 501716a7 |
| 2 | 持久化来源要求、统一覆盖和完成检查、取消全量声明必填 | 已提交 | 78b4c0a7 |
| 3 | 输出解析、引用归属、完整性与分页 | 已提交 | c418b7e1 |
| 4 | Web 获取统一经过已配置 provider | 已验收，随本批提交 | 见 Git 日志 |
| 5 | 精简提示词、兼容恢复与会话回放、最终审计 | 进行中 | — |

## 已核实基线

- 起点提交：7ec6608a；开始时工作区干净。
- kernel_execution 在临时投影验证后持久化，成功后再提交可见投影；无需重建结算流程。
- kernel_receipts 已有完整 ToolResult、摘要验证和大型回执产物，继续复用。
- run 路径手写 canonical.execution；通用解码位于 execution_claims，导致读取器跨层依赖。
- 来源检查仅要求至少一个来源；source_gaps 在任意来源打开后提前退出，不能准确覆盖多个必需 URL。
- execution_claims 同时存在列全要求与 64 项上限。
- gateway 的 direct_urls 分支绕过 provider，直接 HTTP/PDF 下载。
- api.lihe.chat 的历史实机结果为 SPA 静态正文空白；统一获取入口不等于增加浏览器能力。

## 验收清单

- [ ] 首次返回、审批恢复、持久化重启恢复的执行事实一致。
- [ ] 非零退出码与工具交付分开，shell/run ok 语义保留。
- [ ] 两个必需 URL 一成一败或一个未尝试，不能完整完成；失败后成功解除缺口。
- [ ] 超过 64 条执行回执可由程序核验收尾；可选声明仍严格校验。
- [ ] 输出引用损坏、摘要或归属错误明确失败，不退回预览声称完整。
- [ ] 执行状态与采集、保留、当前读取完整性分开；分页指向原始回执。
- [ ] 指定 URL 和搜索来源使用同一已配置获取入口，保持聊天/Research 策略。
- [ ] 历史任务与提案兼容恢复；事实恢复、输出读取和上下文渲染没有执行或联网副作用。
- [ ] 提示词与 schema 同步收敛，保持原生与兼容协议一致。
- [ ] 直接相关测试、Ruff、类型检查通过；记录实机测试与其边界。

## 验证记录

### 第一批

- 原生进程通过 ExecutionReceipt 生成 canonical 与兼容 audit；审批恢复与旧入口同样生成结构化事实。
- 通用解码迁回 runtime/core，保留显式旧 audit 解码，不解析展示文字。
- 删除原生路径手写 execution 字段与恢复公式、读取器对完成检查模块的解码依赖。
- 清除 toolchain/runtime.py 的全部三处 Any，分别使用 ActionPolicyDecision、IncrementalFileIndex、ExecutionOutputStore；同步收窄 managed output 参数。
- 直接相关 7 个测试模块：68 passed。新增原生非零/零退出的真实回执持久化恢复、审批字段一致性与旧审计解码测试。
- 首次失败为新增测试漏传必填 ok，修复测试；类型检查发现 managed output 转发参数仍为 object，修复其真实类型边界。
- 修改文件 Ruff 通过；mypy 全生产树 657 文件通过；Pyrefly 0 errors；ty 通过。全生产树类型检查是仓库规定门禁，不运行全量 pytest。

### 第二批

- 来源要求在现有 durable admission 入库时生成 version=1 的 source_requirements 快照，由既有不可变任务字段机制保护；TaskSession 使用该快照，旧任务在入口从已保存原始输入恢复，不在完成阶段猜要求。
- 新增纯 SourceCoverage 计算，供上下文与完成检查共用。成功覆盖优先于历史失败；从成功回执保留请求别名与最终地址，兼容 ledger 合并别名。
- 普通聊天在全部必需来源均已尝试、仍有真实获取失败时自动 blocked，支持已交付部分内容；参数/策略错误不冒充获取失败。Research 保留自身收尾契约。
- execution_claims 变为可选局部声明；程序遍历所有回执，非法事实仍失败，模型声明仍校验。删除按任务关键词强制列全的逻辑及其与 64 项上限的矛盾。
- source_gaps 保留历史参数校验，覆盖不依赖模型抄写；删除任意成功即不显示来源缺口、仅全失败受阻等分支。
- 8 个直接相关模块 107 passed。因新增冻结入库字段，额外验证入库/分叉/检查点/来源恢复的 6 个相关模块，47 passed。
- 回归发现 required-check 提前返回遗漏来源缺口反馈，修复生产反馈路径；其余失败为测试异常类型、合并 ledger 取回入口与旧提示契约，按真实接口修正测试。
- Ruff、mypy（657 文件）、Pyrefly、ty 与类型精度门禁通过；没有增加 Any。

### 第三批

- 抽出无状态 ResolvedOutput/resolve_result_output。明确绑定的执行快照优先，其次 Managed output，否则回执正文；不读取即将被覆盖的低优先级附件。
- 新执行回执绑定输出快照的归属、revision 和 SHA-256；执行 ID 本身不强制外部读取。旧回执按原 retained/capture 标记解码，不虚构历史摘要。
- 分开返回展示省略、本页省略、采集损失、存储完整性与当前恢复能力。终止状态和完整保留的本次尝试输出独立；执行失败不等于读取失败。
- 外部内容、摘要、版本、归属错误与丢失均明确失败，错误回执仍携带已知执行事实；分页继续使用原 result_ref。
- 修复完整回执恢复强制读取无关输出附件的生产问题：回执自身仍完整验证，附件在读取时验证，附件损坏不抹掉退出码。
- 消除 kernel_receipts 的全部 Any，使用 ResultReceiptFields、真实存储/工作区/回执类型与严格 JSON 解码；将本轮已迁移模块纳入既有类型精度保护。
- 11 个直接相关模块 83 passed，覆盖输出损坏、重启、审批 HTTP、历史回执和真实原生进程成功/失败。因恢复职责调整，加入严格回执类型/损坏回归。
- 新原生进程测试最初使用不被 run 授权的 python -c/--version；保持生产授权规则，改为临时测试文件的真实 pytest 验证命令。最终运行有既有长路径 RAM 盘清理警告，测试断言全部通过。
- Ruff、mypy、Pyrefly、ty 通过；类型精度门禁随提交复查。

### 第四批

- gateway 删除 direct_urls 的独立下载后端；所有 open 都调用配置的 provider.fetch，direct_urls 仅影响候选选择。
- HTTP/PDF 分流集中为现有 source_download 叶子模块的 fetch_http_document，ChatWebProvider 复用。聊天默认 HTTP、Research 默认现有浏览器/连接器组合，显式 factory 在两种策略下均生效。
- 清除 source_download 全部 Any，明确 urllib 响应、Header、页面返回及重定向结果类型；删除重复宽泛异常兜底，使用标准 url/status 属性。
- 110 个相关测试通过，另选 test_research 中 8 个 HTTP/PDF 测试通过，未运行整个研究测试集。新增验证指定与搜索 URL 共用注入 provider，保持跳转网络策略。
- 类型检查发现 urllib 旧 geturl/getcode 弃用；切换标准属性后，旧响应替身缺 url/status，更新测试替身而未添加生产兼容分支。原测试“指定网址必须绕过 provider”的契约按新要求更新。
- Ruff、mypy、Pyrefly、ty、类型精度门禁通过；无新 Any，下载模块加入保护。
