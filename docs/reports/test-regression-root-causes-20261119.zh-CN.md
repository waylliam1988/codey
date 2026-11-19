# 2026-11-19 回归失败根因与修复记录

本次依据已保存的全量 pytest 结果逐项定位：290 failed、13906 passed、52 skipped、683 errors。修复后使用相关用例及机器契约门禁验证，没有再次运行全量 pytest，没有创建或复制 worktree。

最初的 `ghost_directive(..., provider=...)` TypeError 来自旧测试替身的签名落后于当时的生产接口。当前生产路径已迁移到正式 memory 接口；该回归已在先前提交 `4d86fba9` 改为真实的提案、确认、删除流程，覆盖 chat/auto，不增加模型推理。此次没有恢复 Ghost 兼容代码。

| 分类 | 根因 | 修复与保留的约束 |
| --- | --- | --- |
| 生产缺陷 | 启动、调度器和生命周期请求并发发布同一个 `service.json`；Windows 协调器短暂持有文件时还可能拒绝替换 | 串行化状态快照及原子替换；仅对 WinError 5 有界重试，其他错误和持续失败仍向上传播。增加并发、失败解锁、短暂及持续拒绝的回归，并验证真实发布与回滚 |
| 生产缺陷 | Shell 审批先进入 claimed，随后发现命令契约无效且未创建进程；旧 abandoned 事实无法合法结算 claimed | 记录失败的 `spawn_error` 执行结算，验证不启动、不重放，重启后仍保留结果 |
| 生产缺陷 | 手动重试沿用同一个 UI 进度块，旧 attempt 的终态覆盖新 attempt | 请求活动携带先前 run IDs，进度渲染忽略这些 run 的状态覆盖；新增失败复现及修复后的 DOM 回归 |
| 生产缺陷 | evidence 阶段复用了最终综合报告的 done 指引 | 从该阶段冻结的 done spec 生成指引，保留原生 summary 调用的格式指导；验证证据阶段、原生答案协议和提示词基线 |
| 生产缺陷 | 已明确限定打开目标时，邻近 Source 背景行仍可能被解释为额外打开命令 | 在正向命令已指定 URL 时区分背景来源，保留否定命令与独立来源地址的规则；验证来源准入及实际预取 |
| 生产架构缺陷 | knowledge/runtime/kernel 的依赖越层、冷启动导入过重，部分已有大文件继续增长 | 使用 knowledge 自有 Protocol、runtime 自有数据类型及 provider 兼容边界；将参数校验、schema 校验和资源构建移至各自模块。保留架构检查并降低两个已缩小文件的上限 |
| 测试失配 | 共享 parity 探针的 Search 替身不再满足生产浏览器 provider 契约，导致 683 项 setup 连锁报错 | 当前探针使用正式 `BrowserSearchProvider` 基类；冻结旧版本仍使用旧替身。补充退役模块/事件的明确归属，不重写冻结基线，不放宽未分类差异检查 |
| 测试清单错误 | 两个退役事件模块的迁移归属指向不存在的 `codey/memory/store.py` | 根据实际事件写入及投影实现修正为 `codey/ghost/backend.py`，保留文件存在性及全部旧模块覆盖检查 |
| 测试失配 | 旧 Ghost patch、资源依赖、CLI 动作、回调参数和冻结快照字段仍留在测试工具中 | 改用当前接口和真实资源所有权；实验缩小工具集时同步移除悬空浏览器绑定，继续要求快照严格往返一致 |
| 测试失配 | 浏览器脚本仍使用旧 `read` 编辑参数、旧上传方式及旧 done.summary/paragraphs 形状，或漏读必需来源 | 更新脚本使用正式回执引用、结构化上传和对应 done schema；补足真实 browser_read。继续校验过期版本冲突、文件保留、原始回执及未验证效果，不绕过完成门禁 |
| 测试工具缺陷 | 自动 follow-up 已直接经过 host 的叶调用，原 public invoke 追踪漏掉等待和读取；事实组装也未转发当前文档身份 | 被动记录实际叶调用并与已提交回执核对，转发 completion context 的文档身份，不增加或代执行浏览器动作 |
| 测试失配 | 五个连续错误答案的脚本在正确答案之前触发生产无进展保护 | 将每种错误独立参数化为一次拒绝、一次正确修复；覆盖过期事实、非整数、转义换行、遗漏字段和段落格式，保留连续拒绝保护 |
| 测试失配 | UI 脚本用未准入的 SSE 事件模拟真实任务生命周期，或未等待异步校验完成 | 使用持久化的正式准入及实际浏览器交互；等待可观测状态，继续拒绝未知事件制造或结算任务 |

验证期间还排除了两个环境干扰：本地 RAM 临时盘空间不足导致的数据库/I/O 错误，以及仓库内临时目录继承父 Git 仓库而改变隔离/锁语义。后续 pytest 和机器门禁均使用仓库外的 `D:\codey-test-tmp`，临时设置 `GITHUB_ACTIONS=true` 跳过本地 RAM 盘配置，没有修改工作区锁实现或用户配置。

完成的验证：

- `python -m tools.machine_contract_gate`：1502 passed、14 subtests passed，无失败、无跳过。
- 核心相关回归：750 passed、47 subtests passed。
- 完整旧协议 parity、parity regressions、架构及 golden 回归：834 passed、416 subtests passed，包含全部 682 个冻结案例及迁移归属检查。
- 服务发布、原生答案协议与提示词等生产回归：64 passed；deep research、共享 chat 和隔离回归：48 passed。
- UI、Shell 审批、服务发布及来源准入/预取组合回归：298 passed。
- 浏览器原定向集合中的失败已分批修复验证，包括答案修复、输出与事实组装等 50 项通过，采用率文件全部 31 项通过。拆分答案错误场景后新增 8 个参数化用例。
- 前端相关单测 41 passed，TypeScript 检查及前端构建通过；已重新发布前端资产，前端和 runtime 清单检查通过。
- 全树 Ruff 通过；mypy 检查 677 个生产源文件通过；Pyrefly 0 errors（35 suppressed、164 条既有 warnings）；ty 通过。类型检查目标均为 Python 3.11，与 CI 的全生产树命令一致。
- `tools.kernel_type_precision_gate` 通过；`git diff --check` 通过。

CI 已有 pytest 步骤 `timeout-minutes: 240`，Windows job 为 300 分钟。保留了与本任务无关的文件及现有运行服务。本记录不表示修复后重新跑过或通过全量 pytest。
