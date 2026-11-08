# 五批事实收敛：最终实机证据与边界

日期：2026-11-08。模型为本机 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，OpenAI completions 兼容协议。未调用线上 Solar。测试使用修改后的工作树；产物 manifest 中的 Git HEAD 为第四批提交，不能误认为第五批没有参与运行。

## 带引导的工具诊断

命令：

```powershell
python tools/shell_web_diagnostic_gate.py --models gemma --only e2e web --shells pwsh --expect-unavailable --run-dir .e2e-artifacts/fact-consolidation-local-20261108 --timeout 180
```

4/4 检查通过：模型连接、真实 HTTP PowerShell 审批及退出码核对、禁止联网时不提供页面工具、指定首页不可读时以裸 JSON 如实受阻收尾。该诊断提示明确限制单页读取并规定输出字段，因此只能证明相应工具路径，不证明原始问法自主能力。

完整证据：[诊断汇总](../../.e2e-artifacts/fact-consolidation-local-20261108/summary.json)。

## 原始问法独立试验

新增脚本只调用现有 run_headless/provider，不实现任务循环。提示没有补充工具、收尾或证据范围指令，状态与模型请求保存在独立目录。`acquisition_ok` 仅表示调用与交付条件，`answer_review_required=true`，不生成语义正确性通过结论。

```powershell
python tools/shell_web_original_prompt_gate.py --case site --run-dir .e2e-artifacts/fact-original-site-20261108 --timeout 180
python tools/shell_web_original_prompt_gate.py --case food --run-dir .e2e-artifacts/fact-original-food-20261108 --timeout 180
```

| 原始问题 | 实际观察 | 人工审阅结果 |
|---|---|---|
| `https://api.lihe.chat/  你帮我看看这个网站是干嘛的` | 第一次调用携带未注册 question_ref，被正确拒绝；模型修正后实际获取返回 page_blank。随后执行一次站点搜索，未成功打开任何正文，最终运行时 blocked。 | **失败**：模型仍把站点主要用途断言为 API 接口地址，并推测 JSON 等行为，实际页面证据不足。blocked 阻止完整完成，却不能校正答案文字。 |
| `你帮我上网看看福州有什么好吃的` | 两次搜索保留福州美食方向。第一次打开携带未注册 question_ref 被拒绝，后续模型自行规划并成功打开一篇旅游攻略，正文传给模型，最终 done。 | **失败**：答案泛泛推荐佛跳墙、小吃、海鲜和甜点，缺少具体来源；所交付的页面窗口不含佛跳墙及其食材说明。不能把工具调用成功当成基于正文的推荐成功。 |

证据：[网站试验](../../.e2e-artifacts/fact-original-site-20261108/summary.json)、[美食试验](../../.e2e-artifacts/fact-original-food-20261108/summary.json)。目录中的 `provider.jsonl` 记录实际模型请求/响应，`events.jsonl` 记录运行事件，`state` 保留本次独立会话数据。这些为本地产物，不承诺随 Git 分发；本报告保留关键负面结论。

## 判断

回执、覆盖计算和完成检查能确定“是否执行、是否读到、必需目标是否完成”；它们无法直接确定任意自然语言解释是否正确。模型在工具提示已给出事实边界的情况下仍可能补入常识或猜测，这次两条原始问法都留下了实证。

这轮没有使用关键词匹配或模糊语义解析器改写答案，也没有额外拼接文字破坏用户的 JSON。下一步的模型/提示评测应单独衡量：失败后是否明确保留未知、是否读取相关正文段落、推荐是否有实际支持、是否提供可追溯来源。单次样本不能给出可靠率，也不能用于宣称全面超过其他代理。
