# UI、维护与可移除连接器核查（2026-10-13）

## 已修复的 UI 问题

- 删除输入框上方重复的 `Approval required / Review command` DOM 和逻辑。审批仅使用命令卡片的 Allow / Deny。其他会话的 Running / Open 仍可跳转；它表达运行归属，不能代替审批，也不会自动批准。
- 模型菜单原来按“模型按钮左边到 effort 箭头右边”计算宽度。因此 MiMo、Big Pickle 会压窄整个目录；Muse Spark 恰好把按钮撑宽，并不是不同模型使用了不同菜单。现在默认 240px，按窗口剩余宽度收缩，保留标签省略和右侧勾选留白。
- `model-notice` 仍保留：它提示当前模型被移除或不可用，Settings 是修复入口，与命令审批不是重复事项。其文字左边距已与 Choose folder 的文字对齐。
- 没有批量删除失败、停止、保存失败或运行归属提示。它们对应不同事实。原来的 MiMo 重复执行和重启后 Failed/exit 0 问题见[前一份诊断](provider_ci_diagnosis_2026-10-13.zh-CN.md)。

## UI 持久化的同类缺陷

检查前端事件到消息、消息白名单、磁盘写入和恢复渲染这一整条路径，确认并修复：

| 字段 | 原来的问题 | 现在的行为 |
| --- | --- | --- |
| shellStatus | 白名单丢失，成功结果恢复为 Failed | 保留真实状态；历史缺失状态显示未知，不猜成功/失败 |
| commandSha256 / commandChars / commandTruncated | 恢复后丢失命令预览截断说明及完整命令摘要 | 有界保存上述展示字段 |
| exitCode | 通用非负整数清洗把 POSIX 的 -15 等退出码变成 0；非整数也可能被强制转换 | 仅接受真实 int，保留符号；非法值恢复为 null |
| status | 工具结果的状态被剥掉，只能依赖另一个 error 字段 | 保留状态，避免恢复后信息残缺 |

以上 UI 字段都只是展示。执行授权仍来自服务端的持久审批事实，不从聊天历史或命令摘要推导授权。增加了真实磁盘重启回归，包含成功、信号退出、非法退出码、截断预览和工具失败状态。

## 对八项审计的复核

下表“原始统计”使用本轮整理前的 git 跟踪文件，按字节统计，不把 ignored 缓存/参考项目混入仓库体积。

| 审计项 | 核查结论及处理 |
| --- | --- |
| 文档膨胀 | 属实。TEST_REPORT 1,223,612 bytes / 20,957 行 / 407 个二级段落；双语 changelog 800,371 / 720,124 bytes，均有 258 个二级段落。英文 103 个 Unreleased；中文版 103 个未发布段落（不能用英文字符串计数中文版）。Markdown+JSON 共 9,094,087 / 23,458,069 bytes，即 38.8%；生产 Python 4,466,686 bytes，即文档约 2.04 倍 Python，而非 6.4 倍。 |
| 无覆盖率度量 | 属实。之前 CI 和项目配置没有 coverage；本机已安装 coverage 不等于项目在测量。现在 Windows/Python 3.12 的完整 pytest 使用语句和分支覆盖率，并测量 Python 子进程，上传 HTML/JSON/XML 和原始数据。其他 Python 版本保留正常完整 pytest。实际基线和门槛见当前 TEST_REPORT。 |
| 复制残留 | 8 行窗口确实重复，三种浏览器驱动也有共同片段。但连续滑窗会把同一重复块重复计数；tool_result_delivery 中很多窗口只是不同事实类型的相同 schema 字段，并非同一业务重复执行。暂不按窗口数量机械抽基类；优先真实行为重复、共同修改点和稳定契约。 |
| 配置有水分 | Ruff 的 line-length=120 不等于启用了 E501，质疑成立。它仍有复杂度、分支数、语句数和其他 lint 门。Pyrefly 并非纯默认：1.3.2 会把 tool.mypy 配置迁移到内存；已把该次迁移的等效配置显式固定到 tool.pyrefly，未放宽错误级别。生产 Python 的 COMMENT token 行比例确实约 1.22%，但不含 docstring；注释数量不是质量门槛，应解释关键约束，而非为比例增加噪声。 |
| 单点及磁盘占用 | 本地 HEAD 确实 852 个提交、一个作者；根目录确实 89 个 ignored .log。13.6GB 工作目录不能等同约 23MB 跟踪内容，参考项目、缓存和运行事实应单独盘点。未自动删除运行历史、审批事实或用户缓存。第二位维护者和备份策略属于流程投入，无法靠拆文件解决。 |
| CI 薄 | 一个 workflow 本身不是问题。Windows 3.11/3.12/3.13 已跑完整测试、全树类型检查和 machine gate；Linux 不只是四文件，还跑完整 machine gate。本轮增加覆盖率及资源自动发现。依赖安全扫描、发布流水线和 formatter gate 仍未加入，不能称为已解决；发布流程需要真实发布策略，formatter 要先统一历史格式。 |
| machine gate 硬编码 | 当前是 175 个选择条目，其中仅 2 个含类/函数身份，其余主要是模块路径。改名确有维护成本。显式选择是有意保证必要契约不丢失，任何 skip 都失败；改为自动发现也可能悄悄减少强制检查。暂保留清单，不用“少维护”换取漏跑风险。 |
| except Exception | 整理前 651 个 / 411 个生产文件；当前 650 个 / 413 个文件，38 个 pass-only、107 个包含 raise、312 个包含 return（分类可重叠）。数量不能证明全部静默吞错，也不能证明全部安全。本轮去掉 ToolOutcome 退出码投影中无意义的宽泛 try/pass，并简化严格整数归一化。安全关键路径仍须按失败后的授权、结算和重放行为审计。 |

Pyrefly 配置行为依据：[官方配置说明](https://pyrefly.org/en/docs/configuration/)；子进程覆盖率依据：[coverage 配置说明](https://coverage.readthedocs.io/en/latest/config.html#run-patch)。覆盖率只表明执行过哪些语句/分支，不证明断言质量、线程调度、真实上游或危险写入始终安全。

### 文档整理方式

- TEST_REPORT 改为当前验证状态和复现命令入口，不继续逐次追加实验散文。
- 两份 changelog 均只留一个当前未发布段落，153 份有版本号的正式发布记录原文保留。
- 旧开发实验全文用固定 Git commit 链接归档，没有再复制一份巨型 archive 文件。三份首页文档合计从 2,744,107 降到约 681KB，减少约 2MB。
- docs/reports 的 2,759,797 bytes JSON 历史证据暂留，仍有文档引用。新增完整覆盖率和测试日志进入 ignored .e2e-artifacts/ 和 CI artifacts，不追加到跟踪文档。
- 历史报告中曾把 Ling 429 直接断言为上游模型故障，这个结论不足：后续 Node/Python 对照证明客户端传输差异也影响结果。使用新的诊断结论，不把旧记录当作当前证明。

## 传输选择与删除 Zen 的边界

Local 继续使用共享的 Python HTTP 传输。Zen 专用 Node fetch、进程生命周期、端点约束、partner 身份、目录、使用量解释和短暂拒绝重试都留在 codey/providers/zen/。

共享 ApiProvider 只接收通用 opener 和附加 request-id header 配置，不导入 Zen，不根据模型名选择 Zen，不执行连接器返回的项目/Shell 工具。X-OpenCode-Log-Id 也已改为 Zen 注入，而非写死在共享传输里。

打包通过通用 *.mjs 资源规则发现资源；CI 通过 tools.check_javascript 发现实际存在的 JS/MJS，删除 Zen 后不会因缺少 node_http.mjs 而报错。生产和工具源码指纹排除无关可选连接器。

删除时移除 catalog.API_CONNECTIONS 中的 Zen 注册及 Zen 目录；PROVIDER_LABELS 从注册生成。Zen 实机专用工具和测试可以一并移除。保留旧会话文字及运行事实可读，但拒绝对已移除连接器继续发送，不把旧 Zen 会话偷偷切到 Local。

实际 fresh-process absence probe 阻断全部 Zen import，验证 Local Chat Completions / Responses、CLI、目录、旧历史、使用量解析、workspace/Shell 授权和本地 gate 都工作；这比仅 grep 没有品牌字符串更有证明力。

未来 DeepSeek 在线 API 应增加自己的可选连接器，使用区别于网页 deepseek 的 connection ID，复用 ApiProvider 和 Python 传输，独立保存凭据、目录能力、thinking 参数、request-id 和重试策略。其 API 已提供兼容协议：[官方 API 文档](https://api-docs.deepseek.com/guides/codex)。兼容协议不意味着可照搬 Zen 的 partner 身份、特殊错误重试或 thinking 字段；新连接器仍需要多轮工具调用及审批回归。不因为“在线”就默认增加 Node fetch。

## 验证记录

- UI / 持久化回归：51 passed。
- 移除 Zen、类型门配置、错误解释和 Node 传输边界：17 passed。
- coverage gate 的缺失测量、双门槛及生产目录完整性回归：9 passed；连接器日志 ID 注入回归及已有错误展示回归：7 passed。
- Ruff：passed；mypy：413 source files，0 errors；Pyrefly：0 errors（原有 suppressed/warnings 数量保持）。
- 实际 shipped 页面截图：.e2e-artifacts/ui-maintenance/approval-card.png、model-menu.png，已经目视检查。
- 首次完整覆盖率测量：8563 passed、7 skipped、1510 subtests，2 个旧测试失败；失败分别为旧 Failed 文案断言、Node window 测试桩缺少 resize 监听。仅修改测试，相关回归 86 passed / 2 subtests。最终完整复测 **8574 passed / 7 skipped / 1510 subtests（995.82s）**；machine gate **1125 passed / 14 subtests，无 skip（289.44s）**。完整结果已同步 TEST_REPORT。
- 真实 Zen 全 free 模型测试尚不能宣称全部通过，仍保留两项 Ling thinking=true 的未决证据，未重新发起无界实机重试。

## 覆盖率的实际基线

完整 source=codey、branch=true 测量包含全部 413 个生产 Python 文件（未导入文件也进入分母），没有另设 omit 或新增排除来抬高分数。

- 语句：55,370 / 62,536，**88.54%**。
- 分支：17,332 / 22,154，**78.23%**。
- CI 初始门槛分别为 **85% / 75%**；报告必须包含整个生产目录，不能只测 tests 或让未导入模块消失。Windows 路径统一后校验文件清单。
- HTML、JSON、XML 在 .e2e-artifacts/coverage/，不进入 Git。CI 上传相同证据。浏览器 JavaScript 不计入这份 Python 度量。

| 模块 | 语句 | 分支 |
| --- | --- | --- |
| providers/api_transport.py | 87.73% | 71.38% |
| providers/api_stream.py | 86.96% | 76.79% |
| toolchain/runtime.py | 90.46% | 85.11% |
| operations/kernel_recovery.py | 80.97% | 87.50% |
| storage/ui_state_store.py | 91.87% | 82.86% |
| runtime/effects/tool_result_delivery.py | 91.72% | 79.21% |
| app/shell_service.py | 89.14% | 78.85% |

这次度量已经说明“测试多”不等于所有路径都覆盖充分。例如 DeepSeek 网页驱动约 65.24% 语句 / 42.86% 分支，StepFun 驱动约 66.33% / 52.44%；传输错误、网页生命周期和恢复结果重建适合下一轮按缺失分支补行为测试。全局门槛是起点，不能用别处的高分替代危险操作的具体失败路径验证。
