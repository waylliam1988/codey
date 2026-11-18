# PubMed 雷帕霉素：自然请求行动测试

日期：2026-11-18。用户原话：**帮我去PubMed上看看雷帕霉素的论文**。

本轮两种工具模式均没有完成“搜索→打开相关内容→依据观察继续”。不能描述成“模型打开 PubMed 后被登录要求挡住”：被测模型没有发起任何打开请求。同时，真实搜索返回明显跑题，是本轮重要混杂因素，不能把失败全部归因于模型能力。

## 条件

- 模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，本地现有端点。
- native / text 各一个独立任务，温度 0，非流式，输出预算 8192，最大 8 轮；请求超时 120 秒，单次物理尝试。
- 复用既有 `model_web_action_probe` 的正常生产 headless Chat 入口；真实搜索与抓取，无模拟搜索结果，无新增阅读提示或完成门。
- 显式提供 `web.read`；`sources_open_required=False`，无必需 URL。独立状态，无旧助手回答或 Ghost 历史。
- 使用隔离的 headless 采集浏览器，不接管用户浏览器。记录包装只观察真实 search/fetch 返回值，不改查询或数据。
- 实际首个请求中已存在 `web_search` / `open_url` 及网页阅读规则：标题、摘要是发现线索，报告超出摘要的细节前读取相关页面正文。

## 结果

| 工具模式 | 搜索 | 打开尝试 | 已投递打开正文 | 模型请求/响应 | 终态 |
|---|---:|---:|---:|---:|---|
| native | 7 | 0 | 0 | 7/7 | no_progress |
| text | 4 | 0 | 0 | 4/4 | no_progress |

耗时分别约 36.89 秒和 24.66 秒。共 11 次真实生成；后台生成计数由 1526 增至 1537。每次只有一个物理请求尝试，未发生超时、隐藏重试或工具调用解析失败。两格均没有产出论文推荐回答；终态为重复工具结果后无进展停止，不能记为完成。

已逐项检查 native 的 56 条和 text 的 32 条原始搜索命中（含重复），没有得到具体相关论文记录链接。

native 的查询依次为：

1. `雷帕霉素 PubMed 论文`
2. `rapamycin PubMed papers`
3. `rapamycin PubMed`
4. `PubMed rapamycin`
5. 至 7. `site:pubmed.ncbi.nlm.nih.gov rapamycin`

中文查询返回“雷”字百科、雷电模拟器、扫雷等；rapamycin 查询和 site 限定查询返回雷帕霉素百科、试剂商及知乎介绍；PubMed 开头的查询返回 PubMed 首页、高级检索、About 等页面。text 首次查询 `PubMed 雷帕霉素 论文`，随后连续三次 `PubMed rapamycin`，结果同样偏向 PubMed 站点介绍。

生产工具呈现中可见 `https://pubmed.ncbi.nlm.nih.gov/advanced/`；模型没有选择打开，而是继续搜索。原始命中含首页，但当前来源选择会过滤无查询参数的站点根首页，因此不能把原始命中的首页等同于模型可选搜索结果。高级检索页未被该规则过滤。

模型表现出将雷帕霉素换成英文、调整关键词和尝试 site 限定的动作，但没有根据反复跑题的观察切换到打开网站。搜索回执为何跑题尚未定位；没有证据可以断言为查询截断、编码错误或搜索引擎本身的单一责任。

## 独立访问核对：不算模型成绩

以下通过同一生产 `WebSourceProvider` 另行访问，结果**未投递给被测模型**：

| URL | 结果 |
|---|---|
| `https://pubmed.ncbi.nlm.nih.gov/?term=rapamycin` | 成功，标题为 `rapamycin - Search Results - PubMed`，约 12024 字符 |
| `https://pubmed.ncbi.nlm.nih.gov/19587680/` | HTTP 403，access_restricted |
| `https://pubmed.ncbi.nlm.nih.gov/40188830/` | HTTP 403，access_restricted |
| `https://pubmed.ncbi.nlm.nih.gov/41524558/` | HTTP 403，access_restricted |

成功的检索页包含论文题录、PMID 和结果摘要片段，例如 PEARL trial results（PMID 40188830），以及 ageing human immune system 研究（PMID 41524558）。这些仅为独立访问取得的检索条目，不代表被测模型阅读过，也不代表取得论文完整摘要或全文。

所以，当前环境能读取至少一个 PubMed 站内检索页，但论文记录页仍有访问限制。403 不能证明需要注册或登录，也不能据此确定封锁原因。当前 `open_url` 规则允许带 `?term=rapamycin` 的站点根路径；这个检索 URL 没有投递给模型，因此也不能声称模型已知这条可行路径。

## 证据与边界

- 实验脚本：[model_pubmed_action_probe.py](../../tools/model_pubmed_action_probe.py)。
- 指标：[model-pubmed-action-metrics-20261118.json](model-pubmed-action-metrics-20261118.json)。
- 原始证据根目录：`D:/codey-local-evidence/model-pubmed-action-20261118-first/`。
- `protocol.json` 在生成前冻结；每格保存 `manifest.json`、`provider.jsonl`、`events.jsonl`、`live-acquisitions.jsonl`、`summary.json` 与生产源码 hash。
- 独立访问保存在 `independent-access-control.json`、`independent-record-access.jsonl`；后验后台状态保存在 `postflight.json`。
- 两格各自结束时，`codey/**/*.py` 与冻结快照一致。后续写报告时检测到 `codey/agents/context.py`、`codey/workspace/ghost_selection.py` 有外部并发变更；它们发生在两格结束后的快照期间，未计入模型测试条件。本文及测试工作未修改这些生产文件。
- 本轮只新增测试脚本与记录，不修改生产行为、不提交。脚本 Ruff 检查通过；未运行全量 pytest。
- 每种模式仅一个自然请求样本，无法估计稳定成功率，也无法比较两种模式的普遍优劣。

下一步若要隔离模型动作能力，应另列条件，直接提供可读的 PubMed 检索页，或者使用相关且可读的受控搜索结果，观察能否从题录继续打开记录；不能把改变条件的结果混入本轮自然请求成绩。真实搜索跑题需要独立复现定位，不能用更强提示掩盖它。
