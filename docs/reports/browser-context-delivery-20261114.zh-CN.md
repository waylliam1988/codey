# 第二批第一部分：结构上下文与回执恢复交接

基线 `27fcf28a`。直接修改当前工作区，没有创建、复制 worktree，没有运行全量 pytest。

## 变化与边界

- 普通快照从实际 DOM 采集最近的有意义语义容器标签，保留 `aria-label`、`aria-labelledby` 或直接标题依据。上下文在 `contexts` 中保存一次，元素只保存观察内的 `context_id`，分组文字由这份结构渲染。重复标签不合并；没有标签的控件继续提供原引用。这里只陈述包含关系，不声称业务归属、只读性或结果关联。
- 观察前后校验文档身份，发现换文档就废弃本次引用；没有增加第二套引用存储或浏览器运行时。正文与控件截断仍由原字段表达；当前只保留一个有意义上下文，多层祖先暂未增加。
- pending 查询的交接不再要求任务先有工具检查清单。已送达点击提供当前合同允许的读取；送达不确定提供观察。沿用 `_call_handoff()`、原 ToolSpec、原 CodeMode 转发与冻结合同，没有重写执行或放宽合同。
- 输出 schema 继续是唯一公开返回合同，新上下文字段可选，保留历史及文件专用快照兼容。顺带将修改模块中的既有 `cast` 改为既有严格 JSON 读取函数，没有用类型忽略或关闭检查。

## 验证

- 86 项相关生产回归通过，另 31 项类型精度与 full-surface 合同测试通过，两组不重复计数。相关范围包含观察、实际浏览器返回合同、交接、follow-up、生产执行、冻结恢复及提示无副作用。
- Ruff 全树通过。mypy 生产树、新测试及实验脚本 687 文件通过；修改模块额外启用 `disallow_any_expr`、`disallow_any_explicit`。未增加 Any、cast 或 type ignore。
- 首次测试有 2 失败、50 通过：新增测试没有建立真实浏览器任务/外层工具暴露，却断言有直接工具。修正测试前提后，3 项新增测试通过；最终相关回归为上述 86 通过。类型检查初次发现新增字段收窄问题，已修正；早期命令引号错误与静态检查记录均保留。

日志、类型配置和测试首次失败：`.e2e-artifacts/bc1/`。新增模块已在最终实机前纳入 git 索引，生产摘要覆盖该模块。

后续第二部分复查补充：tools 为隐式命名空间，必须以 `--explicit-package-bases` 确保模块级严格 Any 设置匹配。该复查发现本实验脚本一处打印字典的隐式 Any，已在第二部分改成显式 JsonObject；最终严格检查及日志见第二部分报告。前述第一部分初次检查不能单独证明该脚本已应用模块级额外 Any 门禁。

## 完整实机矩阵

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bc1-final-live --scenario all --timeout 180`。

本地 Gemma4 12B Q4_K_M，seed 123456789，temperature 0，真实 Chromium、生产入口/kernel/CodeMode/持久回执。完整轮 **10/12**，生产摘要前后一致：`804fe341f42751afb1d433703ff7f842d22c835cfb7234ac2a154ac4e4461114`。

通过 basic、structured、legacy_structured、reread/冻结恢复、Hybrid stale_edit、upload、strict Research、Research sequence、source_failure、fixed_failure。

首次失败保留：

- stale_ref：模型使用填写后的新引用，没有执行要求的旧引用拒绝测试；`failed_stale_refs=0`，却在最终文字中声称已测试。属于模型漏步骤及无回执支持的声明，没有将其计作通过。
- download：模型连续三次将 `await browser_snapshot()` 的 Result 外壳当 `.elements`，原合同拒绝；未调用实际下载，最终 `no_progress`。原请求明确要求 CodeMode，未为模型错误改写程序或合同。

未发现对应生产确定性故障。原始请求、模型回复、事件和 state 都在上述目录。更早 `.e2e-artifacts/bc1-live/` 是静态检查尚未完成时提前启动、随后主动中止的轮次，不计作完整矩阵。

## 独立开关实验

`tools/browser_context_delivery_ab.py` 分别关闭上下文呈现和恢复交接，并单独加入第一次查询读回故障。仅改变观察呈现/交接与故障边界；执行、授权和回执链继续使用生产实现。原始记录：`.e2e-artifacts/bc1-ab/`。

8 个单元全部完成，每个单元 4 次模型调用、实际点击一次、选对目标并取得对应答案。四种开关组合没有观察到请求数或正确率差异。模型全部选择普通点击再读取，没有进入自动查询读回，因此四个故障单元的注入都未触发，恢复收益没有被这次自主实验覆盖。不能把 8/8 当作恢复入口收益，也不能据此宣称普遍减负。恢复调用的合同与交付由真实浏览器相关回归验证。
