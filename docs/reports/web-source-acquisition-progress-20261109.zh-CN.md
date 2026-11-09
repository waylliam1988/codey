# 网页获取入口收敛实施进度

日期：2026-11-09（Asia/Taipei）。起始提交：`1884e3d1`；起始工作区干净。此前8秒等待及真实浏览器生命周期回归已经提交，本批保留。

## 授权与边界

用户授权将 `ChatWebProvider` 改名、迁移为 Chat / Research 共用的 `WebSourceProvider`，全局普通网页采用 HTTP 优先、必要时浏览器兜底。允许修复本次发现的生产确定性缺陷。只复用现有 kernel、BrowserWorker、Session、来源网关、正文快照、passage、staged/commit 与恢复合同，不增加 agent loop、重复账本、双写、提示策略或默认交互工具。

改动文件清除显式 `Any`，不增加类型豁免或防御性兼容分支。测试失败按真实合同定位，区分旧测试与生产缺陷。优先直接相关测试，不主动运行完整 pytest、覆盖率或全套安装包构建；若模块迁移涉及运行时清单，补对应清单/安装边界验证并说明范围。

## 本批计划

1. 将现有 Chat 获取层迁移到共用模块，统一 HTTP → browser 选择、总期限与 acquisition 合并；保留 connector 与显式注入。
2. 浏览器 provider 移除反向 HTTP 兜底；PDF发现/下载沿原实现统一收尾。HTTP 保留瞬时状态码，区分空壳、401/403、429、404/410、策略拒绝与任务停止。
3. 补直接相关测试，验证失败不生成证据、单次来源接收、恢复/续读不重取、搜索诊断、页面/会话所有权与取消。
4. 通过生产 headless/provider/kernel 运行 KoboldCpp 实机；保留真实请求、响应、回执和获取经过，修复确定性问题后复核。
5. Ruff、Mypy、Pyrefly、ty、precision gate；更新文档并提交。

## 当前进度

- 已核对现有入口：Chat 只在 `render_required` 时升级；Research 基础 provider 为浏览器，并保留内部 HTTP 反向兜底。
- 已确认 HTTP/浏览器后端不应写来源账本；网关 `_receive_capture` 和共用 `render_source_read` 已承担接收/阅读，无需重建。
- 已将旧 `chat_web.py` 迁移为 `web_sources.py`，不保留旧类/模块兼容入口。Chat 默认直接使用共用层，Research 在共用层外保留原 connector 包装；显式注入仍优先。
- 获取策略集中在 `_should_try_browser`：普通网页 HTTP 可读直接返回，空壳/验证页、401/403、服务端错误或请求失败按规则尝试一次浏览器。404/410、429、策略拒绝不升级；取消/总期限异常直接传播。HTTP 状态码只用于本次决策，不扩展 acquisition v1 持久化合同。
- 浏览器后端删除反向 HTTP HTML 兜底和 PDF 下载职责，只导航/读取/报告 PDF 发现。共用层复用现有下载器完成 PDF；已知 PDF 不先做 HTML 探测。PDF 下载/解析失败保持原失败语义，不作为普通空白网页升级。
- `combine_acquisitions` 合并真实尝试顺序，只向原网关交付最终捕获；不新增来源账本、正文缓存或证据写入点。搜索继续委托同一惰性浏览器实例并透传诊断。
- 本次获取共享原 deadline scope，HTTP 阶段计入30秒获取上限，外层更早的期限保持有效；沿用原 BrowserWorker/Session/关闭路径，保留此前8秒正文等待上限。
- `task_execution.py` 整文件消除显式 `Any`，改用已有 TaskSession、ResearchTools、ToolSpec、ToolOutcome 等合同；纳入整文件类型精度检查。未修改生产 prompt。

## 修复与失败归因

- **生产缺陷：停止信号被执行代理吞掉。** 网关已经传播 `TaskCancelled` / `DeadlineExceeded`，但 `_execute_research` 的通用异常捕获转成了普通工具失败。新增两项回归修复前均失败；现在明确传播停止信号。
- **生产缺陷：浏览器重定向被网络策略拒绝时归因不准、读取页未立即释放。** 改为 `policy_denied`、`delivery=confirmed`，同时通过原所有权路径释放读取页；新增回归验证页面关闭与句柄清空。
- **旧测试合同：** 调整已移除的 `render_required` / `http_fallback` 假设、严格 acquisition 回执、真实来源 passage、Session 页面所有权。测试不能因为兜底扩大而意外连真实网络；明确提供需要的后端失败 fixture，不放宽生产合同。
- **补充边界测试：** `observed_tool_result` 原来只声明打开正文，没有描述实际交付 passage，导致 kernel 正确拒绝成功回执；辅助器现在复用 `describe_captured_window` 为明确 fixture 正文生成描述。hybrid 知识验收原来拦截 Research 专用工厂，现拦截实际共用入口；生产来源校验不放宽。对应失败选择修正后94项全部通过。
- **CDP 测试断言：** observer 与 provider 使用不同 CDP 连接，Python 对象身份不能跨连接比较。改为在原浏览器 worker 内，验证读取页与 provider 自己的会话页不同且属于同一 context；原用户页面内容、标签数和浏览器连接仍从 observer 验证。
- 重启前出现浏览器获取超时和进程清理超时，用户确认同期电脑卡死并重启。重启后4项真实 CDP 生命周期测试全部通过；不据此引入超时放宽或冗余重试。

## 验证记录

实现与核心验收完成。已确认：

- 重启后直接相关选择 **220 passed**（网页获取、Chat、SPA、失败不入证据、来源合同、恢复、取消、版本读取、共用接收、交互/观察和运行时清单）；真实 CDP 生命周期另 **4 passed**。不将此前重叠运行次数相加宣称唯一测试数。
- KoboldCpp 原始记录根目录：`.e2e-artifacts/web-source-acquisition-20261109/`。通过原生产 `run_headless` / 默认 provider 工厂 / kernel / 持久化回执运行；仅网络响应和 headless 浏览器配置为 fixture，不是任意互联网网站成功率实验。
- `chat-a`：生产执行 `done`，取得61/17/37克及原文。最初分析器漏读内联回执，原失败 summary 保留；修正为复用现有持久化回执恢复后，同一原始运行的 `receipt-review.json` 通过。
- `research-a`：长步骤诊断任务中模型重复读取，最终 `no_progress`，原失败记录保留，不算通过。未修改生产提示。
- `research-b`：直接研究三份原始记录并提交，**通过，done**。按顺序 `HTTP static → HTTP rendered(403) → browser rendered → HTTP PDF`，各最终捕获只接收一次，交付61/17/37克与 `[e1]/[e2]/[e3]`，注明不含包装；动态正文在5.5秒后出现。
- `chat-b`：**通过，done**。再次验证相同获取顺序，实际执行 `source_search → open_hit → reopen_source`，三份最终捕获，定位/续读没有额外网络获取。此轮之后只补了策略拒绝重定向的页面释放，没有改变正常获取分支；该释放分支由直接回归覆盖。
- 执行代理项目/知识/回执、停止信号、失败页和精度门禁选择：**94 passed**；Research 文件中 browser/PDF/default factory 选择：**41 passed / 112 deselected**。与220项选择有重叠，不相加宣称唯一测试数。
- 三个全生产树类型检查器串行通过：Mypy **664 source files 无问题**；Pyrefly **0 errors，35 suppressed，143 warnings**（既存警告/抑制不当作消除）；ty **All checks passed**。日志在上述根目录 `checks/`。精度门禁通过，没有新增类型豁免。
- Ruff 首轮发现4处迁移后的导入排序，已修正；提交前对全部改动 Python 文件轻量复核，**All checks passed**。生产提示没有修改。

## 机器资源与明确停止的验证

- 用户先后报告本机卡死并重启；第二次明确指出 `test_wheel_cold_start_reconnect_and_upgrade_without_repository` 导致整机卡顿，要求不要运行。**该测试不再运行，本批安装包验证未完成、不宣称通过。** 重启后原测试进程会话已不存在。
- 最后一组辅助器其他调用方/CDP复核与 wheel 检查使用同一顺序命令；重启前只收到部分进度，未取得完整结论，不计入通过数量。已有独立4项 CDP 通过记录有效。
- 按用户最新要求停止新增测试和实机任务，只更新记录、轻量检查并提交。剩余未完成的安装检查不作为继续占用机器的理由。

未运行全量 pytest、覆盖率或全套安装包矩阵。模块从 `chat_web.py` 迁移为 `web_sources.py`，因此更新 `runtime.json` 并通过相关运行时清单测试。曾尝试上述单项 wheel 检查，但因整机资源问题和用户明确要求终止；未完成安装验证。没有修改打包配置，不扩展为 sdist/跨平台构建验证。
