# 第二批第二部分：原文档 pending 与明确结果关联

基线为第一部分 `64c1eb36`。直接使用当前工作区，无 worktree，无全量 pytest。

## 修改

- `pending_click()` 接受当前文档；没有实时输入时，使用原已提交浏览器文档观察。`interrupted_query()` 统一解释原基线与当前文档的差异。原点击保持 delivered/uncertain，不清空或改写历史；新文档不再被旧查询的 pending 拦截。执行、补读、呈现和完成门消费同一判断，各自保留原决策职责。
- 同文档仍保持保守拦截。重新快照取得新 ref 不会把原查询变成新动作；观察成功也不会自动解除 uncertain。没有新增可修改的查询状态表。当前仍不能自动辨别同文档中所有安全恢复点击，这是明确保留的限制。
- 普通控件引用记录文档和已观察结构标签，执行前核验。能识别的容器标签变化、同 URL 重载和原节点替换在点击之前拒绝；不要求整个区域正文不变，也不声称能识别没有页面标识的全部业务对象复用。
- 定向读回支持原生 summary/details 和唯一 aria-controls。首次绑定使用实际 DOM 关系，之后按 Chromium 原 backend node 身份读取，不按标题、DOM id 或 CSS 重新寻找替代节点。节点身份放在原 `query_baseline` 中，经原 browser_resource 恢复连接；没有另一套浏览器或区域解析器。
- 目标未更新、目标节点失效、文档更换返回稳定的结构化 query_read 原因。其他区域更新不能确认目标；目标错误文案仍只是新观察，不增加业务成功标记；相同正文没有额外完成信号时仍为未确认。
- 恢复入口先选择尚未完成的已声明等待，再补读，继续由原冻结合同校验。正文仍走原 gateway/source/passages；这一部分没有实现 passage 隔离，也不声称已消除串记录。

## 一并修复的确定性 bug

新增真实执行回归复现：pending 点击拒绝分支运行时构造 BrowserToolOutput，但该类此前仅在 TYPE_CHECKING 导入。原结果变成 NameError，丢失 delivery=not_delivered。现于实际使用分支导入，保留原拦截事实。

## 验证记录

`.e2e-artifacts/bc2/` 保存首次失败、定向复验、最终生产回归和严格类型配置。首次定向为 1 失败、24 通过；该失败即上述生产 NameError，附加诊断复验也保留，没有将它归为模型问题。修复后定向 13 项通过，明确节点的独立跨进程恢复测试通过，实际点击一次。

扩大相关范围后 154 项通过，未运行全量 pytest。随后新增“已经确认的历史查询换页”反例，发现本次新增中断提示误报；保留首次失败，将未确认点击集合统一投影后，执行/提示/完成相关 70 项复验通过。这两个集合有重叠，不相加计数。已确认的查询不会重新标为中断，原查询未知和已中断也不被写成成功。

Ruff 全树通过。生产树和修改模块严格 mypy 通过。进一步以 `--explicit-package-bases` 检查实际包名，发现实验脚本两处打印字典被 json.dumps 上下文推成隐式 Any；改成显式 JsonObject，最终生产树、两个实验脚本和三个新增测试共 691 文件通过。所有修改模块启用 `disallow_any_expr` / `disallow_any_explicit`，没有 cast、type ignore 或检查豁免。最终配置、首次包名检查及修正日志保存在 `.e2e-artifacts/bc3/`。

未支持的页面仍遵循原普通路径与原保守查询限制。原节点身份只证明节点连续性，不保证页面未将同一结果节点复用于其他业务对象；当前也不自动识别所有同文档恢复点击。

## 完整实机矩阵

首次完整轮 `.e2e-artifacts/bc2-live/` 为 **10/12**，生产摘要前后一致：`1b2a6aca66c89d5f64d8123bda7341fea329ecbc89766d35512bdc95144442e8`。

- basic 失败：模型点击后从 snapshot.elements 查正文的 `.text`，找不到后反复返回相同程序，没有执行真正等待和读取；没有生产工具拒绝或预期外环境错误。
- stale_ref 失败：程序找到输入框后提前 break，按钮 ref 为空，合同拒绝；后续还有畸形 JSON 和同类程序。实际 stale-ref 拒绝为 0，不能把参数校验拒绝算成旧节点拒绝。
- 其余 structured、legacy_structured、reread/恢复、Hybrid stale_edit、upload、download、strict Research、Research sequence 和两个来源失败场景通过。

此后修正上述“已确认查询误报中断”的生产呈现问题，继续完成后续负例与最终完整矩阵。首次轮未覆盖或改写。

补充负例发现本轮重排查询观察时遗漏了原有“文档身份不能为空”的判断，首次失败保存在 `identity-first.log`，已恢复该拒绝并校验观察后的身份。`bc2-final-live/` 因此主动中止，不计作完整矩阵。复查同时移除交接中重复的旧 pending 判断，等待提示也只消费当前文档的点击。

`pytest-identity-final.log` 有 1 项失败、24 项通过；失败单元的原回执显示读回、引用、点击次数均正确，但 `source_stable=false`，因为这次测试跨过了上述生产修改。该轮不作为最终稳定源码的通过证明，保留原始目录并在固定源码上复验，没有归因为模型或环境。

固定最终源码后的相关复验 **71 项全部通过**，263.19 秒：`python -m pytest tests/test_browser_query_binding.py tests/test_browser_handoff_next_steps.py tests/test_browser_followups_production.py tests/test_browser_completion_production.py -q`。日志 `pytest-stable.log`，与前述 154 / 70 项有重叠，不相加。最终新增目标绑定文件独立 13 项通过，审计另外 3 项通过。

最终命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bc2-verified-live --scenario all --timeout 180`。本地同一 Gemma4 12B、seed 123456789、temperature 0，**11/12**；生产摘要前后一致：`63fe444f7f05b97c20e49e588b43de7a09a5345e51fb17774d35b7c4a00cd3d7`。

最终轮 basic、structured、legacy_structured、reread/恢复、Hybrid stale_edit、upload、download、strict Research、Research sequence、source_failure、fixed_failure 通过。stale_ref 仍失败：原请求要求旧引用拒绝验证，模型直接采用填写后的新引用，正式读取和答案正确，但 `failed_stale_refs=0`，所以不计通过。这次没有将参数错误或普通新引用执行算成 stale-ref，也未放宽任何生产合同。

两次完整轮分别为 10/12、11/12；中止轮不参与完整统计。它们是少量、不同源码的本地观测，不能当作稳定成功率提升。最终首次失败、各次复验和原始请求/回执均保留。
