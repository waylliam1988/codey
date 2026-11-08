# 新旧版本回答质量实机对照

日期：2026-11-08。旧三批基线 `7ec6608a`，新版生产代码检查点 `d94a45cc`。
本轮只修改评测脚本与记录，不修改生产代码，不创建 worktree，不运行完整 pytest、覆盖率或安装包构建。
使用已有旧版目录 `.e2e-artifacts/spa-compare-old-20261108`；当前工作树中的用户 `codey/runtime.json` 修改保留。

## 方法

- 模型：本机 KoboldCpp `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
- 各版本统一 temperature=0、最多 8 轮、单次请求 600 秒；一次只运行一个案例。
- 新版包含旧三批之后的事实收敛和 SPA 修改，不能把所有文字差异归因于单独某个提交。
- 原始问法不追加工具使用、证据范围或输出格式指令。
- 调用各版本的生产 headless/kernel/provider；固定 HTTP、搜索和浏览器网络响应。
- SPA 使用真实后台浏览器执行受控 JavaScript，不模拟渲染结果。
- 固定来源是评测夹具，不代表本轮真实联网。另行记录真实网站试验。
- 每例保存原始问题、版本、参数、页面 SHA-256、事件、模型请求/回复和交付正文。
- 工具状态与答案准确性分开评判；入口错误、超时及夹具错误不计为语义质量结论。
- 小样本用于发现差异，不估算可靠率，也不作为全面优于另一版本的证明。

获取预检已通过：同一 SPA，旧版返回 `page_blank`；新版 HTTP → browser 后取得 ToCreate 正文。

## 评判规则

审阅回答是否解决问题、关键事实是否有实际交付正文支持、是否明确未知和来源边界、是否便于追溯。
普通常识可以作为明确区分的补充，不能冒充网页观察。网站营销宣称不等于独立验证的服务质量。
美食原始问题没有要求固定推荐数量或 JSON，不以这些格式作为通过条件。

## 结果

已完成 10 次受控来源模型试验（4 对初测、1 对 SPA 复跑）和 1 次真实网站验证。
全部模型运行正常结束，没有本轮入口错误、超时或夹具故障。
受控试验共 6 次 done、4 次 blocked；这是运行状态统计，不是答案正确率。
完整样本保存在 `.e2e-artifacts/spa-quality-20261108`。

| 案例 | 旧版 | 新版 | 答案审阅 |
|---|---|---|---|
| SPA 网站用途 | blocked；无交付正文 | done；交付 1 份正文 | 旧版编造页面观察；新版主要用途正确，但“所有主流模型”超出来源范围 |
| 静态美食正文 | done；交付正文 | done；交付正文 | 两版主要推荐与做法均有支持；共同缺少最终来源链接 |
| 相同静态网站正文 | done；交付 1 份正文 | done；交付 1 份正文 | 主要用途相当；旧版补入客户端例子，新版仍泛化模型覆盖范围 |
| 空白网站 | blocked；无交付正文 | blocked；无交付正文 | 两版均未守住未知边界，新版也断言 Lihe.chat 平台/API 用途 |
| SPA 反向顺序复跑 | blocked；无交付正文 | done；交付 1 份正文 | 复现获取与主要用途解释差异；新版此次没有“所有主流模型”泛化 |

输入核验确认 5 对样本的问题、模型、温度、轮数、超时与页面 SHA-256 均一致。
美食和静态网站两组实际交付给模型的完整正文逐字相同；SPA 两组则明确为旧版 0 份、新版 1 份。
美食两版搜索词也相同：“福州特色美食 必吃 餐厅 小吃”。
核验只证明输入与交付条件，不自动认证自然语言正确性。

旧版 SPA 摘录：“通过访问……发现该页面目前是一个相对简洁的 API 接口地址”，并声称可能显示简单 JSON。
实际只收到 `page_blank`，未交付正文。这是将域名推测冒充网页观察；运行时 blocked 没有纠正自由文字。

新版 SPA 解释 ToCreate、统一密钥调用 Claude/GPT/Gemini、套餐额度与配额管理，主要信息有交付正文支持。
末尾“所有主流模型”应限定为已接入或正文列举的模型，不能评为完全无误。

美食两版均推荐鱼丸、肉燕、锅边糊，主要做法与适用场景来自同一夹具正文。
新版多一句“口感丰富”，属于评价性扩展，不足以证明优劣。两版均未编造店名、地址、价格或榜单，
但最终答案没有附实际来源链接，读者追溯不便。

静态网站两版主要用途解释相当。旧版额外举了 Chatbox、NextChat，来源没有列出具体客户端；
新版没有补入这些名称，但“大部分主流模型”仍超出列举三个模型的证据范围。

空页两版都未交付正文，也都 blocked。旧版把空页推成“纯粹的 API 端点”；
新版写“根据目前的探索结果……是一个基于 Lihe.chat 平台的 API 接口”，并继续解释平台功能。
搜索夹具只提供一个无实质信息的结果，不能支撑这些判断。新版用了 7 轮、旧版 4 轮；
新版额外搜索没有带来正文或更可靠收尾。两版该项均失败，不能宣称此次修改解决了失败后猜测。

## 真实网站验证

新版使用原始问法实际访问 `https://api.lihe.chat/`，2 轮后 done，真实正文成功进入模型请求。
答案解释 ToCreate、统一密钥调用 Claude/GPT/Gemini、套餐和额度、上游调度及负载均衡，主要用途有页面支持。
但“官方 API 页面”缺少独立身份依据，Chatbox/NextChat 不在本次交付正文中；
网站“稳定可靠”的宣传也没有经过独立服务测试。因此这是获取与主要用途解释成功，不能算逐项完全准确。

## 结论与下一步

本轮样本支持：SPA 获取能力及由此带来的用途回答明显改善；在相同可用正文下没有观察到整体质量下降。
它不支持“新版所有回答更可靠”：空页猜测、证据范围泛化、来源链接缺失仍然存在。
优先评测方向应是失败后明确保留未知、限制证据外补充、在最终答案保留可追溯来源。
本轮没有为这些问题继续修改生产代码。

评判为逐项证据审阅，未声称双盲。另保存匿名 A/B 复核包与独立版本映射，供后续人工复核；
正文与答案均来自实际模型交付记录，没有用模板代替模型回答。

## 产物与复现

- [输入与交付核验](../../.e2e-artifacts/spa-quality-20261108/comparison-audit.json)
- [匿名复核包](../../.e2e-artifacts/spa-quality-20261108/review-packets.json)
- [独立版本映射](../../.e2e-artifacts/spa-quality-20261108/review-key.json)
- [旧版 SPA 初测](../../.e2e-artifacts/spa-quality-20261108/old-site-spa/summary.json)
- [新版 SPA 初测](../../.e2e-artifacts/spa-quality-20261108/new-site-spa/summary.json)
- [旧版美食](../../.e2e-artifacts/spa-quality-20261108/old-food/summary.json)
- [新版美食](../../.e2e-artifacts/spa-quality-20261108/new-food/summary.json)
- [旧版相同正文](../../.e2e-artifacts/spa-quality-20261108/old-site-static/summary.json)
- [新版相同正文](../../.e2e-artifacts/spa-quality-20261108/new-site-static/summary.json)
- [旧版空页](../../.e2e-artifacts/spa-quality-20261108/old-site-blank/summary.json)
- [新版空页](../../.e2e-artifacts/spa-quality-20261108/new-site-blank/summary.json)
- [旧版 SPA 复跑](../../.e2e-artifacts/spa-quality-20261108/old-site-spa-repeat/summary.json)
- [新版 SPA 复跑](../../.e2e-artifacts/spa-quality-20261108/new-site-spa-repeat/summary.json)
- [真实网站](../../.e2e-artifacts/spa-quality-20261108/live-new-site/summary.json)

```powershell
python tools/spa_acquisition_comparison_gate.py --repo .e2e-artifacts/spa-compare-old-20261108 --case site_spa --run-dir .e2e-artifacts/spa-quality-recheck/old-site-spa --timeout 600
python tools/spa_acquisition_comparison_gate.py --repo D:/codey --case site_spa --run-dir .e2e-artifacts/spa-quality-recheck/new-site-spa --timeout 600
```

其他受控案例为 `food`、`site_static`、`site_blank`。复跑重新使用 `site_spa`，反转版本运行顺序。
真实网站使用 `python tools/shell_web_original_prompt_gate.py --case site --run-dir .e2e-artifacts/spa-quality-recheck/live-new-site --timeout 600`。
路径参数中的“新目录”仅存放本次运行记录，不创建或复制源码树。

此前存在的无效夹具样本、180 秒生成超时样本不混入本轮评分。
