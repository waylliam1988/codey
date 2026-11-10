# 浏览器生产接入：七批收尾

起点：`7ebb62c2`。每批必须通过直接相关测试、Ruff、生产修改文件类型检查，以及对应的 KoboldCpp + Chromium 实机 gate，记录后提交，才能开始下一批。

约束：同一 ToolSpec、TurnSnapshot、kernel、回执和 ledger；不另建运行时或双写。机械协议由程序处理，语义决策留给模型。修改的生产文件不保留显式 Any。不运行完整 pytest、覆盖率或打包安装测试。

| 批次 | 工作 | 状态 |
| --- | --- | --- |
| 1 | 完整恢复工具元数据；验证恢复后的执行与 catalog/replay 身份 | 完成 |
| 2 | 同时间采集版本排序、旧 handle 与乱序 replay；对齐 CodeMode 授权 fixture | 完成 |
| 3 | 子调用失败的原回执反馈与 Hybrid 修复验收，保留显式错误处理 | 完成 |
| 4 | 浏览器各工具实际输出的 schema 与 CodeMode 声明 | 完成 |
| 5 | 上传的用户选定附件授权与真实文件回执 | 完成 |
| 6 | 下载启用、持久化存储、摘要与回执 | 完成 |
| 7 | 完整能力 gate 迁移到生产合同；Chat/Research/Hybrid 集成验收 | 完成 |

## 批次 1

- 修复 `_restore_spec()` 丢失 namespace/effect/evidence_route/preconditions；使用已有冻结定义，拒绝缺失/错误字段，不从实时注册表补值。
- 新增六组 Chat/Research/Hybrid × text/native roundtrip 测试，核对完整快照、catalog digest、repair digest 与浏览器动作分类。
- 生产 gate 新增可选序列化 roundtrip，强制用恢复后的原合同执行；不替换授权或 executor。这是恢复合同验证，不宣称模拟了操作系统进程崩溃。
- 验证：`test_browser_snapshot_recovery`、`test_frozen_tool_views`、`test_codemode_repair_replay` 共 20 passed；Ruff、provider_receipts.py 的 mypy 通过。
- 实机：`.e2e-artifacts/browser-seven-b1-restored`，KoboldCpp Hybrid 通过；4 份序列化恢复后的快照，完整浏览器链和真实 edit，结果 173 / QUARTZ_851。没有修改 prompt。

## 批次 2

- 根因：Windows 时钟可连续返回相同微秒，正文 hash 被误当成先后关系。新采集在原 ledger 内给同一来源分配严格递增的采集时间：时钟相同或回退时仅推进一微秒；原回执保留该时间，merge/replay 不重算。没有另建版本计数器、账本或时间状态。
- 新测试固定/回退时钟，校验旧 handle 失效，以及三份原回执的全部排列和重复 replay 都收敛到最新正文。
- 五项 CodeMode 写入失败是 fixture 没有 control 授权；只对齐两个构造点，不更改生产权限 guard。
- 验证：来源时钟排序、handle、共享 intake、版本化 edit 和 ledger 共 61 passed；Ruff、ledger.py 的 mypy 通过。
- 实机：`.e2e-artifacts/browser-seven-b2-reread`，KoboldCpp Research 通过；固定采集时钟，操作前后共四次 browser_read，最终引用新正文 173 / QUARTZ_851。没有修改 prompt。

## 批次 3

- 从父回执引用的原始子回执派生失败摘要，同时显示该父调用真实的文件读取引用；不另存错误账本，不改变程序主动处理错误时的父调用结论。
- 实机发现 JS 参数中的 undefined 被 JSON.stringify 静默删掉。统一严格 JSON 编码用于参数和返回值，参数发出前拒绝 undefined/非有限数/函数等，并从冻结 schema 取字段说明。没有添加 Hybrid 专用提示词。
- 首两次实机分别停在漏传、错误读取 result_ref；保留失败记录。最终 `.e2e-artifacts/browser-seven-b3-receipts` 通过：首次读取后外部修改文件，1 次 edit 失败，随后真实写入 173 / QUARTZ_851 并 done。受控实验不代表任意模型均能稳定修复。
- 验证：子失败反馈、版本化 edit、repair/replay 共 35 passed；扩大检查 guest 编码涉及的 runtime/rejection 用例也通过；Ruff 与 kernel_prompt.py 的 mypy 通过。

## 批次 4

- 各动作、snapshot 可空字段、tabs、eval.value 与下载字段有完整输出 schema；宿主严格校验 JSON，CodeMode 声明直接从冻结 ToolSpec 生成。任意 eval JSON 以 unknown 表达，不引入 Any。
- 实机暴露浏览器预留 about:blank 是 tabs 第一项；原列表没有当前绑定页，增加 active 后模型仍猜第一项。最终直接从原绑定派生顶层 page_url，避免要求模型自行查表。
- 补明 browser_eval 的参数语法：表达式或函数，不是 CodeMode 函数体。模型曾连续传入裸 return；这是必要的工具语法合同，未加任务专用 prompt，也未对失败脚本自动改写重执行。
- 相关 schema/恢复/unwrap 共 21 passed；通用 schema/递归校验/合同漂移另 10 passed（扩大原因：共享 schema 增加 anyOf/null 支持）。Ruff、三份生产文件 mypy 通过。
- 最终实机 `.e2e-artifacts/browser-seven-b4-eval-contract` 通过；所有要求的工具均有成功原回执，脚本 token、当前页 URL、正文结果都正确。此前失败记录保留，不将 marker 命中误当成完整通过。

## 批次 5

- 复用 InputAttachmentStore 的会话授权、不可变内容、摘要校验和既有体积/格式限制。browser_upload 接受当前会话用户已导入的 attachment_id，不接受任意宿主路径；通过内存字节选择文件，避免另建文件授权名单和存储。项目文件须由用户通过已有附件导入入口选定。
- 回执校验真实 file input 的文件名、大小和 MIME，SHA256 是实际传递字节（文本 BOM 沿用原导入规范化）；不宣称服务端已收到或提交。跨会话、路径、内容损坏均拒绝，操作不产生引用。
- 实机暴露成功 snapshot 被父异常遮住，以及 Result<T> 当作数据直接取字段。原回执派生最近控件观察，原 guest 对返回 envelope 的未声明字段访问给出准确合同错误；保留 unwrap、optional 元数据和程序显式处理失败。
- 文件摘要进入已有 operation_receipts；非法 execution_claims 返回真实可用 shell/process 回执，避免把浏览器操作误当成进程执行。未放松完成检查或增加 Hybrid prompt。
- 验证：上传/附件存储/注册合同 26 passed；上传/子反馈/执行声明与恢复 43 passed；guest 相关 runtime/unwrap/repair 等 60 passed。Ruff、七份生产文件 mypy 通过，无显式 Any。
- 最终实机 `.e2e-artifacts/browser-seven-b5-file-receipts` 通过；真实选入 fixture.txt，页面读到 UPLOAD_BYTES_82，答案摘要与实际字节 SHA256 一致。中间的失败记录保留。模型最后引用了较冗长的原文，属于回答质量限制，不把它宣称为任意上传任务的稳定成功率。

## 批次 6

- 启用生产 Chromium context 的 downloads；browser_download 不再向模型暴露 Playwright 临时路径作为最终事实。文件完整复制到现有 ManagedOutputStore，生成受运行/会话范围约束的 artifact handle、路径、字节数和 SHA256。
- 原有 RuntimeEffect settlement 通过 `file_artifact_record` 注册同一个文件 artifact；冷恢复读取 durable artifact 后仍验证内容和摘要。下载失败、超出既有大小/数量策略、跨范围和损坏均 fail-closed。没有第二个下载存储或文件账本。
- 实机初次失败根因是 Chromium 对被 route 拦截的 `<a download>` 响应报告 canceled；独立 Playwright 复现后改用真实 data URL fixture，未改变生产下载逻辑。最终 `.e2e-artifacts/browser-seven-b6-final2` 通过：18 bytes、fixture-download.txt、SHA256 与保存文件一致。
- 验证：下载/managed output/schema 共 23 passed；Ruff、七份生产文件 mypy 通过。

## 批次 7

- 删除旧的实验 full-surface gate：不再临时注册同名工具、不替换 `ANSWER_TOOLS`、不安装自定义 executor。新 gate 只调用正式 `browser_production_live_gate.run_case`，场景、KoboldCpp 连接和断言由入口负责，工具注册、授权、CodeMode、回执、Evidence 全走生产路径。
- 场景矩阵包含 basic、structured（eval.value/tabs）、reread（恢复合同与重复读取）、stale_edit（Hybrid 写入恢复）、upload 和 download。生产入口没有第二套浏览器运行时。
- 相关生产浏览器回归 30 passed，gate wrapper Ruff/mypy 通过。首次全矩阵的 download 因模型乱码偏离任务去运行 `python -m unittest`，被 shell/no-progress 正确拒绝；将该 gate 的任务合同改成等价 ASCII 后单独重跑通过，最终 `.e2e-artifacts/browser-seven-b7-download-en` 成功工具集合为 browser_navigate/snapshot/download/read/codemode。前五个场景在 `.e2e-artifacts/browser-seven-b7-all` 已通过。

七批工作均已提交；每一批的失败实机记录、根因和修复均保留在对应 artifact 目录。实验脚本现在只保留生产入口调用，不再能证明实验自定义 executor 的行为。

## 后续收尾批次

### 批次 A：页面所有权与 stale 动作回执

- BrowserSearchProvider 记录任务拥有页和借用页。CDP 中已有的用户标签只解除路由，不在任务结束时关闭；任务创建的搜索、读取和新标签仍由 provider 清理。
- `browser_upload`、`browser_download` 与 click/fill 使用相同的 stale 元素回执，旧引用返回 `reason=stale_ref`，不会进入文件操作。
- 相关 Chromium 生产测试 29 passed；Ruff、browser_search.py 和 browser_session.py 的 mypy 通过。
- KoboldCpp + Chromium：`.e2e-artifacts/browser-followup-a` 的 structured full-surface gate 通过。

### 批次 B：页面响应事实、错误页和导航 URL 策略

- 浏览器获取与 `browser_read` 共用页面响应/正文验收：HTTP 4xx/5xx、非文本内容、登录页和无可用正文均在证据投影前失败；响应状态与 content type 保留在导航回执和当前页事实中。
- `browser_navigate`、`browser_tab_new` 在 `page.goto` 前执行统一 http(s) URL 策略，拒绝 `javascript:`；重定向后的目标再次校验。
- 新增 HTTP 503 不进入 ledger 的生产回归，以及 javascript 导航不执行的回归。
- 验证：浏览器 schema/生产执行 18 passed，新增错误页与导航回归通过；Ruff、三份生产文件 mypy 通过。
- KoboldCpp + Chromium：`.e2e-artifacts/browser-followup-b` 的 basic full-surface gate 通过。
