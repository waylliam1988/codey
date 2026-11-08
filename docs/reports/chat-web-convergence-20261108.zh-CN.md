# Chat 联网收敛与原始会话验收

后续批次已将普通 Chat 搜索切换为真实浏览器，并修复相同正文跨任务的产物句柄冲突；见[浏览器搜索与冲突修复报告](chat-browser-search-20261108.zh-CN.md)。下文保留切换前的历史验收，失败样本没有改写为通过。

日期：2026-11-08。生产代码验收版本：`bd5d0025`（含前四批与独立失败修复）。原定第五批保存本报告与多轮记录器。没有创建 worktree、复制源码树或修改用户的 runtime.json。

## 修改结论

Chat 默认可用 Web 工具，模型按语义选择；明确的关闭设置优先，恢复沿用冻结策略。普通 Chat 不再承担 research_plan/question_ref，绑定研究问题的任务继续由本轮冻结契约约束。获取复用现有 gateway/provider，SPA 在 provider 内升级浏览器；回执、事实投影与完成门禁仍各自使用现有主干。

指定必需 URL 独立约束完成；无指定 URL 但结构化要求读取时，真实页面获取失败也能 blocked 收尾。可选搜索/读取不会自动增加全局来源义务。原生 run 超时、管道读取错误、排空超时已有 process 作用域和未知退出码的结构化回执，拒绝执行不会伪造已启动事实。

**这些修改解决了确定性的权限、契约、覆盖与回执问题，没有证明自由文字回答全面可靠。** 本轮实机同时出现了成功获取、摘要误写、重复搜索与空页猜测，均保留原始记录。

## 验证范围

各批相关检查与提交见[进度记录](../chat-web-convergence-progress-20261108.zh-CN.md)。没有运行完整 pytest、覆盖率或安装包测试，没有修改打包配置。

- 用户指定的 9 个回执/来源测试文件，最终 124 项通过，原先失败的真实 headless 指定 URL 收尾测试仍保留 blocked 与单次获取断言。
- 异常回执影响的验证命令、进程采集、完成边界，补充 135 项测试、18 项子测试通过。这组与前组有重叠，不累加为独立总量。
- 第五批入口/普通 Chat 契约/来源完成/原始记录器 84 项通过；记录器类型迁移后再跑对应 5 项通过。
- Ruff、全生产树 mypy（657 files）、Pyrefly、ty、precision gate 通过。Pyrefly 保留已有 35 suppressed、133 warnings，未增加豁免。记录器另以 mypy `--follow-imports silent` 检查通过，修正了其原有和新增的 JSON 列表类型不变性问题。
- 旧入口测试的两组断言按新明确产品契约更新：默认授权不由文本关键词决定，不自动要求打开来源；否定/示例仍不生成必需 URL。生产代码没有迁就旧词表测试。

## 实机设置与证据边界

模型为 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature=0，最多 8 轮，单次请求上限 600 秒。每次调用生产 headless/kernel/provider，没有额外 agent loop、语义分类器、自动改写答案或通用语义裁判。

前三个对照复用历史旧三批 `7ec6608a` 的既有记录，未重新检出旧代码。核验问题、模型、温度、轮数、超时和页面 SHA-256 均相同，见[输入核验](../../.e2e-artifacts/chat-web-convergence-20261108/comparison-audit.json)。固定页面夹具只控制网络响应，SPA 由真实浏览器执行 JavaScript；不能把它们当成真实网站访问。本轮新增默认许可等多项改动，单例文字差异不能归因于某一个提交。

| 案例 | 旧三批既有记录 | 本轮结果 | 答案审阅 |
|---|---|---|---|
| 同一 SPA 用途 | blocked，无正文，猜测 API 用途 | `batch3-spa`：一次 open_url、done，有渲染正文 | 主要 ToCreate/密钥/模型/套餐/配额信息有支持；缺显式来源标注 |
| 同一美食正文 | done，三种小吃正确，缺来源链接 | `batch4-food`：搜索、打开、done | 三种小吃及做法来自夹具，附实际链接，无新增店铺/价格/榜单；此样本追溯性改善 |
| 同一空白页面 | blocked，仍猜测网站用途 | `batch6-blank-final`：blocked，无正文 | 仍断言 API 端点、猜测 JSON 与后端职责，质量失败；模型请求确实包含未知边界指导 |

这三个小样本支持 SPA 可读性和部分来源追溯改善；不支持“新版本所有答案更好”。空页这一项没有改善成可靠答案。受控批次的 manifest 记录运行当时 HEAD，待提交修改归属由进度记录中的对应提交说明。

## 同会话：福州美食 → 东百中心

[完整记录](../../.e2e-artifacts/chat-web-convergence-20261108/batch5-conversation/summary.json)由同一 state_home、同一 session_id 的两个生产请求生成。第二轮首个真实模型请求包含前轮原始问题与实际回答，`prior_task_and_answer_in_next_request=true`。记录器只检查，没有注入历史。

第一轮原句“你帮我上网看看福州有什么好吃的”：一次搜索成功，无 research_plan/question_ref 错误，2 轮 done，但没有打开正文。搜索摘要其实包含较多店铺和做法，不能将这些内容全部判成虚构；最终把摘要中的“猪血化”写成“猪血花”，未说明仅据摘要整理，也没有给出实际来源链接，使用了内部 `[r1, r2, r8]`。这是事实准确性与追溯缺陷。

第二轮原句“东百中心 ？你帮我搜索一下，这里附近有什么好玩的”：Web 工具可用，未出现 snapshot_tool_mismatch。模型三次使用完全相同的查询“东百中心 附近有什么好玩的”，Bing RSS 返回“东”字百科等无关结果，最终现有重复结果机制以 no_progress 停止，没有交付景点回答。实际请求保留了福州上下文，故不能再归因于记录器启动了新会话；模型没有用城市缩小查询、没有换查询或询问。该项行为与任务完成验收失败。

`acquisition_ok=false` 在这些记录中只表示未达到该用例的搜索、正文与交付条件，不证明摘要中的每个事实都错误。现有无进展停止如实保留，没有为使样本通过而放宽停止或来源要求。

## 真实网站访问

[本轮真实网站记录](../../.e2e-artifacts/chat-web-convergence-20261108/batch5-live-site/summary.json)：原始网站问法，一次 open_url 成功、done，实际交付了 389 字页面正文。主要用途是 ToCreate 的多模型 API 聚合，统一密钥、Claude/GPT/Gemini、上游调度、用量与配额可追溯至页面。

答案将其称为“官方 API 平台”缺少独立身份依据；“稳定可靠”是网站自述，模型没有标明自述或说明未做服务测试。这次获取与用途解释成功，仍不能算每项结论都已独立核验。

## 保留的问题与可复现命令

后续质量问题分别属于搜索提供方相关性、模型查询调整与证据范围遵守；当前记录没有证明 Codey 丢失会话或重复执行了未请求的工具。避免为此回退成关键词授权、强制每次搜索打开所有页面，或添加另一个运行时。可替换搜索 provider 做对照，继续以同一生产调用链记录，而不是将本轮失败计为通过。

```powershell
python tools/shell_web_original_prompt_gate.py --case conversation --run-dir .e2e-artifacts/chat-web-convergence-recheck/conversation --timeout 600
python tools/shell_web_original_prompt_gate.py --case site --run-dir .e2e-artifacts/chat-web-convergence-recheck/site --timeout 600
python tools/spa_acquisition_comparison_gate.py --repo D:/codey --case site_blank --run-dir .e2e-artifacts/chat-web-convergence-recheck/blank --timeout 600
```

新目录仅存运行产物，不包含检出源码。所有失败样本留在 `.e2e-artifacts/chat-web-convergence-20261108`，没有从报告中移除。
