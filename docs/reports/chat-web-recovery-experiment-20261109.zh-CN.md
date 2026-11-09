# Chat 搜索恢复与正文阅读实验

日期：2026-11-09。基线 `1e655744`。先实验，收益确认后才允许修改生产；实验候选仅位于 tools，通过 worker 进程中的临时 patch 接入现有生产入口。用户问题不加工具或验收提示，不新增 agent 执行循环，不修改用户配置。

本批完成 **46 次 KoboldCpp 实机任务**（28 次受控来源、18 次真实网络），另有 8 次真实浏览器引擎探测。只将表格单元格边界修复合入生产；查询恢复、重复反馈、续读和引用检查保留实验区。原始普通 Chat 的完整回答质量仍未通过，失败均保留。

## 预先约定的判断标准

- 单独比较查询恢复指引、跨查询重复反馈、正文续读指引、引用身份检查，再比较有证据支持的组合。
- 受控来源用于稳定复现，真实网络用于验证可用性；二者分别记录，不能互相替代。
- 受控用例要求实际交付含目标事实的正文、答案覆盖这些事实并提供来源；目录标签与搜索摘要不算正文证据。这个机械条件仍不能证明每一句话正确，必须人工审阅。
- 候选进入生产至少须在重复的配对运行中改善目标行为，并通过正常聊天、仅查链接的对照；不能只靠一次成功或更多工具调用。实验前记录计划，交错基线/候选顺序，保留全部失败。
- 记录轮数、无效搜索数、耗时、真实模型 usage。成功恢复允许增加必要的搜索/阅读，但保留生产 8 轮与无进展上限；已正确完成的对照不得出现持续的工具循环或显著消耗退化。
- 引用实验只检查内部标记和 URL 身份，不把已发现的官网链接伪装成正文已读，也不新增全局读取/引用义务。未读来源的论断与自由文字语义另行审阅。
- 后备引擎必须真实浏览器验证；验证页、空结果、偏题结果均保留。配置存在不算可靠，不按关键词重合自动判定相关性。

## 初始范围

福州美食、东百中心、成都太古里三个偏题恢复场景；美食长文、账号安全长文两个续读场景；短文引用；仅查官网链接；普通问候。受控搜索服务声明：保留核心实体、至多附加两个字符的查询能返回目标页面，其他长查询返回相同字典结果。该规则仅用于构造可重复的失败，不进入生产。

长文将主题正文置于首个可见窗口之后；主题标题提供正常导航，不能当成已经阅读。受控页面与官网链接均为合成 example.com 夹具，不用于证明现实事实。

## 运行记录

模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，地址 `http://127.0.0.1:5001/v1`，temperature=0，最多 8 轮，请求超时 180 秒。使用现有 headless、kernel、工具执行、来源登记和 provider；不注入模型回复、工具调用或成功历史。受控模式只替换搜索与来源响应，live 模式使用实际网络及默认真实浏览器。每个 trial 单独进程、独立状态目录，第二轮反转候选顺序。

原始记录位于忽略目录 `.e2e-artifacts/chat-web-recovery-20261109/`。每批先保存 `plan.json`，每次保留 manifest、实际 provider 请求/回复、事件、摘要；受控模式另存搜索响应。worker 退出 0 只表示运行完成，答案通过率必须查看 summary，并人工核对实际交付正文。

### 查询恢复

`pilot-recovery` 共 6 次：福州美食的基线、恢复指引、重复反馈均完成正文与链接回答，没有改善空间。东百中心的基线与恢复指引均自动无进展停止；重复反馈候选曾读到正文并答出三个地点，但漏来源，完整条件仍失败。

`confirm-recovery` 共 12 次，两个地点 × 基线/重复反馈/四项组合 × 两轮：

| 受控场景 | 基线完整通过 | 单独重复反馈 | 四项组合 |
|---|---:|---:|---:|
| 东百中心 | 0/2 | 0/2 | 2/2 |
| 成都太古里 | 0/2 | 0/2 | 0/2 |

东百中心组合确实搜索、打开了相关正文，回答包含三坊七巷、达明美食街、乌山及夹具来源。两次约 37,845 prompt tokens、6 轮；失败基线约 29,190 tokens、5 轮，增加约 30%。候选仍曾搜索“东百中心 广州”，随后正文确认福州；不能把本次成功解释为已经具备可靠的城市判断。成都场景收到反馈后仍堆叠限定词，没有泛化收益。

所有跨查询反馈均核对真实出站请求，提醒确实送达；没有重置无进展计数、放宽上限或注入答案。一次重复反馈试验生成了不合法 `ask_user`：4 个选项对应 5 条描述，将 `allow_free_text_true` 写进描述数组，连续三次无效后以 `protocol` 停止。生产正确拒绝无效参数，不能据此修改解析器迁就模型。

### 正文续读

`pilot-reading` 共 4 次：美食、安全长文的基线与候选都实际读到了末尾主题，均为两次打开、3 轮。安全基线留下裸 `[s1]`，候选给出了 Markdown 链接；这只是一次引用格式差异，不能算续读增益。

初次试验后增加真实网页 `travel_reading`，沿用原冲突页面及“这个网页介绍了哪些福州美食”的问题。它属于后续验证，不冒充初始预注册样例。`live-reading` 共 4 次（基线/续读指引各两次），都读到了美食章节，全部遗漏来源链接。基线均 3 轮，候选为 3、6 轮；prompt tokens 中位数分别为 34,089、50,671。续读指引尚无稳定收益，且出现额外消耗。

逐项审阅发现，基线与候选各有一次把“南煎肝”写作“南煎肝豬”。这不能仅归咎于模型：原 HTML 的三个单元格是 `南煎肝`、`豬肝嫩滑，醬汁香濃`、`聚春園`，当前抽取结果为 `南煎肝豬肝嫩滑，醬汁香濃聚春園`，已经丢失列边界。

### 引用身份及对照

`pilot-citations` 共 6 次：短美食正文的两种版本均最终正确附链接，但候选由 3 增至 4 轮。只查官网时，基线返回记录中不存在的 `https://www.taikoo.com/`，候选拒绝该地址后返回已发现的夹具 URL，且没有强制打开正文，2 轮增至 4 轮。普通问候两种版本均 1 轮、无工具。

这证明身份检查能纠正一次编造链接，但只有一次受控观测；还不能证明检查范围、URL 身份等价关系及成本均适合普通 Chat。检查也没有要求所有答案附链接，故不能用来解决“完全漏链接”。

`live-original-controls` 共 8 次，两个原始问题、只查官网、普通问候各比较基线/组合一次：

| 问题 | 基线 | 组合候选 |
|---|---|---|
| 福州有什么好吃的 | 已读正文，最终裸 `[s1]`、漏链接；3 轮/20,929 tokens | 已读正文、附实际链接；3 轮/21,262 tokens |
| 东百中心附近有什么好玩的 | 仅搜索摘要，未读正文；2 轮/12,349 tokens | 已读正文，但完成提议反复被拒绝，自动无进展停止；6 轮/64,247 tokens |
| 成都太古里官网链接 | 2 轮、未强制正文 | 2 轮、同一已发现 URL、未强制正文 |
| 简单自我介绍 | 1 轮、无工具 | 1 轮、无工具 |

东百失败的直接原因在实验引用校验：合法文本 `*参考来源：s1 (https://www.sohu.com/a/205542231_435084)*` 被现有 `_urls_in_text` 提取成带 `)*` 的 URL，候选又以字符串集合检查来源身份，误判未知地址。最终 `no_progress` 指向 repeated rejected completion proposals，区别于重复搜索停止。这是实验候选复用边界不合适，不能当成用户按 Stop 或网络读取失败。单元用例保留该反例；本批不推广这个引用校验，也不对共享 URL 解析临时追加删字符规则。

人工审阅确认福州答案主题匹配，但没有独立核实每家店营业现状。东百答案主要推荐商场内餐饮，仍未充分调查周边游玩。结构通过不能替代主题覆盖、事实准确性和时效性审核。

### 后备搜索入口

`engine-probe` 是 8 次真实浏览器查询，不调用模型。Bing 对福州美食、东百中心、成都太古里、杭州美食均返回非空结果；人工审阅发现内容与实体/主题有关。东百长查询主要是商场美食文章，并不证明已经回答“附近有什么好玩”。本次没有重现只返回“东”字典，也不能保证未来不会偏题。

DuckDuckGo 四次均正常返回空列表，没有分类异常；本次记录不能断言具体是验证码或 DOM 提取失效。没有验证出可靠后备，暂不推广引擎切换接口或自动重试。

## 后续独立假设：表格列边界

此项在上述真实续读试验发现问题后单独提出，运行前记录：只在实验 worker 中让既有 HTML extractor 将 `td`、`th` 视为文本边界，保持其他指引和完成检查为基线。先用真实行与最小 HTML 验证列文本分离、隐藏内容与内联文本行为，再使用原网页进行基线/候选两轮交错实机。

推广条件是生产抽取确定性保留列内容与边界，真实模型不再把相邻说明拼入菜名，并且没有额外阅读循环或相关回归。不能借此声称解决漏链接、搜索恢复或所有表格语义。候选若无稳定收益，同样保留在实验区。

表格回归先在未改生产时运行，5 条失败、1 条内联文本对照通过；仅临时 patch 加入两种单元格标签后 6 条全部通过。涵盖真实菜名/说明/店家、数字列不能拼成新数字、标题单元格、隐藏单元格和内联标签不拆词。

此前 40 次模型试验的候选文件 SHA-256 为 `a5dbeef7c1094fe4abea3c72f6590f6b49f13f37fb670b0a0cdddb379f099d6a`；完成后才新增独立 `table_cells` 变体，原四项候选的行为未改，其 SHA-256 为 `e69ff1d17c3f29da948aa96377e90249a506aa1284720466072fa8fcc990a038`。基线/候选、问题、来源响应与实际发送指引均可从各次 manifest 和 provider 记录核对。期间另一工作提交 `543d4c5c` 更新前端附件显示，未更改本实验 Python 调用链；不把该提交或其打包清单纳入本批修改。

`live-table-boundaries` 的 4 次实机均实际续读美食章节。两次候选均按原表格区分“南煎肝 / 豬肝嫩滑……”和“鍋邊 / 米漿攤成……”。本组基线的“南煎肝”也正确，但两次都把“鍋邊”与后列“米漿”拼作“鍋邊米漿”，与原 HTML 菜名单元格不符。模型看到的工具正文同步验证：候选交付的列文本已有换行分隔，其他单元格文字仍保留。

两种版本均为两次读取、3 轮。prompt tokens 中位数基线 33,597.5、候选 33,710.5（约 +0.34%）；耗时中位数 45.04、47.29 秒，小样本网络耗时不能当作稳定性能差异。四次仍全部漏链接，完整结构条件为 0/4。因此只推广确定性的抽取修复，不将它宣传为完整回答质量门禁通过。

生产改动仅为既有 `_BLOCK_TAGS` 增加 `td`、`th`，没有新解析器、调度器、词表或特殊菜名规则。修复后未开启实验补丁的 `production-table-check` 又跑了一次原网页问题：模型只读首段，误把“闽江夜游”混入美食，2 轮结束、未附链接，完整验收失败。这说明续读决策的波动仍存在，不能从一次或两次候选成功推断已经解决。

另增加 `table_fields` 作为生产路径定向检查，明确请求网页中“必試地道美食”表格的列信息，不提供菜名答案。`production-table-fields` 在无实验补丁下两次读取、3 轮，实际交付含分隔单元格的正文；最终表格的七种美食、特色和推荐店家均与原 HTML 各列对应，“南煎肝”和“鍋邊”正确。但仍漏来源链接，因此完整结构条件依然失败。该问题与原问题不同，只确认经过真实生产读取的单元格边界，不计作原问题成功。

两次生产验证的 manifest HEAD 是 `543d4c5c`，实际运行包含尚未提交的单元格修复；修复文件 SHA-256 为 `fd645b26b7a012d5798d6eb2c79eeae535d4f39d6ea0e3d296628f574a87d93f`。汇总见忽略目录中的 `review.json`；它区分任务完成、正文交付、列身份正确和完整答案质量。

## 推广决定与范围

| 项目 | 决定 | 依据 |
|---|---|---|
| 通用查询恢复指引 | 保留实验区 | 单独无增益，组合跨地点不稳定 |
| 跨查询重复反馈 | 保留实验区 | 提醒已送达，但单独恢复不稳定 |
| 正文续读指引 | 保留实验区 | 基线也能续读，真实两轮无稳定收益且有额外消耗 |
| 普通 Chat 引用身份检查 | 拒绝本版推广 | 有单次纠错，但真实 Markdown 合法链接被误拒并导致停止 |
| 后备引擎接口 | 暂不增加 | 未找到实际可用的后备入口 |
| HTML 单元格边界 | 合入生产 | 确定性内容粘连缺陷、6 条回归、两轮实机按原表格区分列内容 |

这批没有解决普通 Chat 漏链接、模型续读选择、自由文字语义及实时搜索相关性。保留无进展和无效参数限制。剩余方向应先修正实验引用检查的 Markdown 身份边界，并验证读到相关段落后的行为；不以强制短查询、自动删“附近”或放宽停止机制获得表面成功。

## 测试与静态检查

相关测试最终 **122 passed**：实验事实反馈/隔离/测量，原有重复反馈，原始问题与美食门禁分析器，新增表格边界，以及共用抽取器的隐藏内容、页面标题、浏览器失败页面、Chat provider、SPA 获取和 source gateway。扩大到这些路径，是因为 HTTP 与浏览器来源共用此抽取器；没有运行完整 pytest、覆盖率或安装包构建。

旧 `test_repeated_tool_feedback` 原本以普通项目任务调用已不在冻结契约中的 `research_plan`。核对生产契约后，将夹具任务改为允许研究规划的 `evidence_followup`，保留真实规划观察；未修改生产工具授权或放宽完成检查。新增的五条表格失败则确认是生产抽取丢边界，修复生产后六条均通过。两类失败分别处理。

所有本批 Python 文件 Ruff 通过；新增工具与测试单独严格 mypy 通过（5 files）。生产 `codey/research/extract.py` 的 mypy、Pyrefly、ty 均通过（Python 3.11）；kernel 精度门禁通过。没有增加 `Any`、类型忽略或检查豁免，不声称历史全仓库已无动态类型。

## 复现

以下目录必须为未使用的新目录。比较旧生产行为需回到表格修复前版本；当前生产已经保留单元格边界，`baseline` 不会撤销生产修复。`table_cells` 仅表示实验临时 patch，四项组合不包含该变体。

```powershell
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/recovery-pilot --cases food_recovery nearby_recovery --variants baseline recovery repeat --repeats 1 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/reading-pilot --cases food_reading security_reading --variants baseline reading --repeats 1 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/citations-pilot --cases food_citations link_only normal_chat --variants baseline citations --repeats 1 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/recovery-confirm --cases nearby_recovery chengdu_recovery --variants baseline repeat combined --repeats 2 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/reading-live --mode live --cases travel_reading --variants baseline reading --repeats 2 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/original-controls-live --mode live --cases food_recovery nearby_recovery link_only normal_chat --variants baseline combined --repeats 1 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/table-live --mode live --cases travel_reading --variants baseline table_cells --repeats 2 --timeout 180
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/production-table-fields --mode live --cases table_fields --variants baseline --repeats 1 --timeout 180
python tools/chat_search_engine_probe.py --run-dir .e2e-artifacts/engine-probe
```

```powershell
python -m pytest tests/test_chat_web_recovery_experiment.py tests/test_repeated_tool_feedback.py tests/test_shell_web_original_prompt_gate.py tests/test_shell_web_food_gate.py tests/test_research_table_boundaries.py tests/test_research_deleted_source_text.py tests/test_research_page_title_excludes_svg_accessibility_labels.py tests/test_browser_failed_pages_never_become_evidence.py tests/test_chat_spa_acquisition.py tests/test_chat_web_provider.py tests/test_source_gateway.py -q
python -m mypy --explicit-package-bases --follow-imports silent tools/chat_web_experiment_candidates.py tools/chat_web_recovery_experiment.py tools/chat_search_engine_probe.py tests/test_chat_web_recovery_experiment.py tests/test_research_table_boundaries.py --python-version 3.11
python -m mypy codey/research/extract.py --python-version 3.11
pyrefly check codey/research/extract.py --python-version 3.11
ty check codey/research/extract.py --python-version 3.11
python tools/kernel_type_precision_gate.py
```
