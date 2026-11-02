# OpenScience 为什么在相同 12B 上研究得更好：源码与真实请求交叉审计

日期：2026-11-02。Codey 基线 `de3edcb1`；参考项目 `reference-projects/openscience` 基线 `a08e0ad0ad3d6448123039ee4659163eb2e35d99`，版本 2.0.153。本轮没有修改生产代码，没有发起模型推理，没有调整或重启 KoboldCpp。

本文补充 [Research 收敛审计](research-convergence-audit-20261102.zh-CN.md)，重点回答：既然同一部署的 12B 在 OpenScience 中能回答，为什么 Codey 仍失败，哪些差异值得修改。此前早期 OpenScience 审计中“尚无同材料 12B 对照”的限定，已被后续实机数据更新，不能继续当作当前结论。

## 1. 结论与证据边界

**现有数据证明选定任务有工程空间，不能把 Codey 的差距直接归结为 12B 的能力上限。** 但它没有证明 OpenScience 普遍研究可靠，更没有把某一条提示的因果收益单独测出来。

这次最重要的区分是：

1. OpenScience 在六个成功短题中，实际就是“读一次完整材料，再写一次自然正文”。没有额外审稿模型、子代理或技能替它解题。
2. Codey 已经删掉旧逐题手续，但模型真正面对的请求接口仍不同：材料进入临时 user 工作视图，原 user 轮可能被裁掉后重建，tool 回执变成指向视图的说明，最终必须调用提交工具。旧流程删减不等于已经复现 OpenScience 的阅读问答接口。
3. 找到一个直接的内容退化样本：Codey 第一次已经用中文答出关键条件，因未调用工具被拒绝；修复请求没带这份正文，重新生成时改成英文、丢失条件并新增错误限制。这不是“模型从一开始就不会”。
4. 长文有明确的访问差异：OpenScience 一次看到第 44 节；Codey 旧窗口与目录让它重复读开头并把已有答案当作缺口。这类失败首先修材料访问。
5. 完整短材料仍会发生关系遗漏，因此材料访问、协议修复和内容判断必须分别处理，不能承诺一个投影修改解决全部问题。

复核结果在 [机器审计 JSON](reports/openscience-codey-source-audit-20261102.json)。生成脚本为 [.e2e-artifacts/openscience-codey-source-audit-20261102/audit.py](../.e2e-artifacts/openscience-codey-source-audit-20261102/audit.py)。脚本不导入运行时、不联网，解析 **22 份历史 wire 文件**，保存源码、材料和 wire 的 SHA256，并检查采样、正文行完整性与实际工具调用。它不是新的模型测评。

## 2. 先排除 OpenScience 的“隐藏能力”

### 2.1 使用的就是同一个本地模型

实际转发 payload 的模型均为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。审计的请求中，除各例 seed 外，temperature 0.3、top_p 0.9、max_tokens 4096、enable_thinking=false 均一致。OpenScience 的 small_model 也配置为同一部署。

采样统一发生在原测评代理的 [sampling](../tests/manual/research_framework_comparison.py:92)，没有翻译消息、补研究动作或改写答案。原生工具、提示、读材料接口仍各用各的。相同 seed 不意味着不同提示下采样轨迹相同。

### 2.2 六个成功短题没有实际调用复杂研究机制

检查第三批 WAL/asyncio 各三个 seed：

| 实际行为 | 六例逐一核查 |
| --- | --- |
| 主研究推理 | 每例 2 次：先 read，再最终正文 |
| 标题生成 | 每例另 1 次；独立标题上下文 |
| 实际研究工具调用 | 每例仅 `read` 一次 |
| 子代理、skill、todo、额外检索或审稿 | 没有对应调用或额外推理请求 |
| 原 system 和原 user | 最终作答请求中逐字保留 |
| 最终 tool 原文 | 与 evidence.md 的全部原文行一致 |

因此，不能把这六例优势归功于多代理协作、技能自动拆题、审稿模型或复杂 plan→execute→review 链。标题请求没有把答案反馈给研究轮次；标题调用使用 tools={}，返回值更新标题，见 [标题生成](../reference-projects/openscience/backend/cli/src/session/prompt.ts:4173)。

`research` 代理本身是普通 primary/native 代理，没有专用 prompt，见 [agent.ts](../reference-projects/openscience/backend/cli/src/agent/agent.ts:203)。Gemma/Qwen 没有专用的小模型分支，走默认 family，见 [system.ts](../reference-projects/openscience/backend/cli/src/session/system.ts:44)。

### 2.3 源码里有审稿器，不等于测评用了审稿器

[review.ts](../reference-projects/openscience/backend/cli/src/harness/review.ts:189) 需要明确的文件交付物等条件。[prose](../reference-projects/openscience/backend/cli/src/harness/review.ts:65) 还检查至少 400 个空白分词、英语单词占比和英语句末数量。短中文聊天回答不应据此被想象成已经审过。真实 wire 中也没有审稿推理。

同样，不能因参考仓库存在研究合同、StudyDriver、压缩和记忆模块，就把它们列为这些样本的成功原因。对这六个短题，真实执行路径没有提供这样的证据。

## 3. 最关键的差异在模型接口，而不是内核数量

### 3.1 OpenScience 保留了一段自然的阅读过程

以 asyncio/74 为例，最终主请求结构是：

```text
system：稳定的代理/科学工作指导、环境和技能索引
user：原始五问，保持原文
assistant：刚才的 read(evidence.md) 原生调用
tool：完整原文，逐行编号，有 End of file
→ assistant：中文最终答案
```

源码对应：

- [prompt.ts:1986](../reference-projects/openscience/backend/cli/src/session/prompt.ts:1986) 将会话经 `toModelMessages` 转入当前推理。
- [message-v2.ts:929](../reference-projects/openscience/backend/cli/src/session/message-v2.ts:929) 保留 user 文本；[1062](../reference-projects/openscience/backend/cli/src/session/message-v2.ts:1062) 从原 tool 状态取 output。
- [llm.ts:283](../reference-projects/openscience/backend/cli/src/session/llm.ts:283) 拼接 system 和会话消息。
- [prompt.ts:1212](../reference-projects/openscience/backend/cli/src/session/prompt.ts:1212) 在普通最终文本完成且没有额外反馈时结束；并不要求再调用一个终结提交工具。

这不意味着 OpenScience 无条件永久保留所有原文。它也有工具输出压缩和上下文预算。但成功短题的实际请求没有经过这些裁剪。

### 3.2 Codey 当前整篇路径仍让模型同时处理工作视图和提交协议

第八批 asyncio/74 读取后的请求是：

```text
assistant：open_url 原生调用
tool：读取成功；完整原文在当前证据视图 [e1]，另附回执信息
user：临时证据原文 + 来源/定位元数据 + 原始五问 + 写作/引用/提交要求
user：冻结时钟
tool_choice=required
→ assistant：submit_research_answer({markdown, state})
```

这个测评配置里没有 system 消息。**这描述被测路径，不表示 Codey provider 不支持 system_prompt。** 原任务并没有丢失，而是被重新放入 synthetic user 工作视图；不能把已修复的“完全丢任务”bug 重新算作当前问题。

源码对应：

- [kernel_prompt.py:96](../codey/operations/kernel_prompt.py:96)：将任务、上下文和执行要求组成一个 prompt；整篇分支追加提交要求。
- [kernel_context.py:336](../codey/operations/kernel_context.py:336)：投影工作视图，整篇 native 设置 required 和一组工具历史。
- [api_request_view.py:9](../codey/providers/api_request_view.py:9)：按完整协议组裁剪历史，system/developer 可保留，初始 user 不享有同样保护。
- [task_guidance.py:77](../codey/operations/task_guidance.py:77)：已在视图中完整显示的成功读取，tool 文本缩成指针回执。
- [answer_context.py:26](../codey/research/answer_context.py:26)：把证据、定位、历史、原任务和提交规则合成视图。
- [api_provider.py:768](../codey/providers/api_provider.py:768)：以 user 消息插入视图，并在末尾附时钟。

这些函数分别解决了真实问题：避免重复原文、保证冷启动可重建、限制历史、校验引用。不应把它们简单评为坏设计。但组合后，模型需要从一个重建的工作包同时辨认原任务、数据、运行记录和最终动作，和 OpenScience 的阅读对话并不是同一种任务接口。

**请求可重建与自然角色分层并不冲突。** 可以仍从同一回执和日志生成 system/user/tool 消息，不必把所有内容合成一个 user 字符串，更不必另建一份可写的研究状态。

### 3.3 OpenScience 提示更长，不能把减负等同于继续删字

asyncio/74 的 OpenScience system 为 25,998 字符，最终输入后端报告 10,301 token。第八批 Codey 首次提交请求为 2,644 token；第三批 Codey 首次写正文为 4,137 token。

OpenScience 不仅规则很多，实际可见工具也有九个；并不是只有 read/answer 两个 schema。Codey 第八批可见七个工具。因此“把工具数/总字数进一步降到最低”没有得到这组对照的支持。

更有价值的差别是：OS 的长指导稳定地承担角色和工作原则；原 user 承担问题；tool 承担读到的材料。Codey 当前工作视图则混合这些职责。是否由于角色位置、具体科学提示、材料编号或其他差异而改善，仍需隔离，不能声称单独添加 system 就必然追平。

本轮已按源码将 `default.txt` 的 `{{SCIENCE}}` 替换成 science.txt、附 response.txt，并确认结果确实出现在历史 system 中。这里不是只读到一个可能从未用过的提示文件。

[science.txt](../reference-projects/openscience/backend/cli/src/agent/prompt/science.txt) 强调核对证据、每个题目子句、对象和适用范围，也包含大量对这些短题无关的科研/文件规范。可借鉴它的工作原则，不能据此要求复制整份约 26K 字符系统提示；Codey 最新指导已经包含很多相似原则，再追加同义句不是新的工程方案。

## 4. 一个直接证据：协议修复把已经答出的内容损坏了

历史文件：`.e2e-artifacts/research-whole-20261101/batch3-native/wire/asyncio_synthesis-75-codey.jsonl`。

**第 2 次请求**，模型输出 1,359 字符中文普通正文，其中明确写道：

> 此取消行为无论 `return_exceptions` 的值如何都会传播 [e5]。

它也区分取消 gather 和取消一个 Task/Future、说明其他子任务不因此取消，并回答 TaskGroup 不能由材料证明总是更快。本文没有对这份未交付正文重新给整份通过分，只确认这些直接可见的内容。

因为没有调用 submit，正文被当作协议错误。**第 3 次请求**只有三个 user 消息：工作视图、通用协议修复、时钟；没有上一份 assistant 正文。虽然原题和材料仍在，但模型无法按“保留已有参数原样重发”的要求看到那份原答案，实质上必须重新写。

正式提交后：

- 语言改成英文；
- 丢失父取消不受 return_exceptions 影响的明确条件；
- 新增 `unless return_exceptions is False and the gather itself is cancelled`，错误限制传播条件。

这些最终问题也已登记在原第三批人工评分中。审计 JSON 保存两份正文哈希、消息角色和对应原句检查。

这说明：

1. 至少这一例，不能把最终错误解释为模型最初没有相关知识或读不懂材料。
2. 协议修复不是没有内容成本的包装步骤；它会诱发重新生成和语义漂移。
3. OpenScience 在普通正文完成后直接交付，少了一次这样的重新生成机会。

**不能由此直接宣布“让 Codey 接受所有普通文本就解决了”。** 其他第一份正文也可能有事实错误；直接 Markdown 诊断也确实出现过内容问题。当前发布事务还要求引用、读取和状态验证，不能因文本看起来像答案就绕过。

第八批 required 将短题缩到两次请求后，内容仍未通过；第十批后端仍会忽略 required。因此“不必要的重写造成一部分退化”是具体证据，“工具提交造成全部差距”则超出了证据。

## 5. 长文优势首先来自拿到完整材料

固定长文原始内容为 18,858 字符。OS 的 evidence.md 加题头/来源后为 18,997 字符、181 行。其 read 默认 2,000 行、50 KiB，见 [read.ts:32](../reference-projects/openscience/backend/cli/src/tool/read.ts:32)。一读便包含所有 181 行，并显示 EOF，最终主请求 13,084 输入 token。

第 44 节明确写出：

- remote job 为 RESTORED **且** operator 校验恢复产物 checksum 才完成；
- 第 4 版回执七天过期，取代第 3 版 24 小时；
- 过期只删除回执，不删除恢复产物，也不能证明恢复成功或失败。

Codey 第八批长文实际七次请求：

```text
open_url(无 offset)
open_url(无 offset)
web_search(第44节)
open_url(无 offset)
web_search(第44节)
web_search(第44节)
submit(answered，但把第44节内容写成缺口)
```

当时目录只露出前 16 个标题，关键章节位置不可见。模型没有自动发明正确 offset，又没有在已有来源内用 source_search 定位，而走向无效重复。这个样本测的是“找到并读到正文，再总结”；OS 的文件读取默认就完成了前半段，Codey 则留给小模型多次导航决策。

最新生产源码已补首尾目录和检索提示，但尚未实机验收；不能拿旧失败代表新目录必然无效，也不能把修复未验收写成已经解决。前一审计指出的非连续读取进度与 18,000 字符整窗预算仍值得处理。

可迁移的原则是：**在真实 token 预算允许时，直接给完整的小中型来源；超预算时给明确的截断范围、缺口和可执行续读位置。** 不是把 OS 的 50 KiB 常数抄过去。这个长文的原文甚至比 Codey 18,000 字符视图预算略大，单独扩大 open_url 默认读取而不处理当前视图预算，仍可能读到却显示不全。

## 6. 同材料对照仍有呈现差异，也有 OpenScience 的失败

脚本验证四组材料：asyncio、WAL、长文、跨来源导出。OS 文件内容都能从 Codey 对应 sources.json 按原材料题头/URL 逐字重建。没有发现换材料、注入标准答案或使用不同来源内容。

但“同材料”不等于“相同阅读成本”：

- 短题各一份完整选段，访问差异较小；第八批 gather 已在一个 e1 中完整显示，仍有条件遗漏。
- 第三批 gather 的取消脚注曾分在 e5；把相关规则合并成一个窗口是合理尝试，但后续结果没有证明这足以解决内容问题。
- 长文由 OS 默认完整读出，Codey 则需分页/定位。
- 跨来源题，OS 一个 evidence.md 已拼好两份材料，Codey 则需读两个 URL；这不是研究能力的纯隔离对照。

跨来源补测 OS 实际五次请求，先多次 read 未找到目标、再 glob、read、回答；也遗漏了项目所有者的作用范围和冻结评分中的 24 小时 TTL，进程退出码 3。它说明 OS 也会绕路、漏关系，不能把它想象成处处简洁的 read→answer。TTL 未被题目直接询问的评分局限继续保留，不在这次审计事后豁免。

已有第三批：Codey 必要事实 80/84，整篇严格内容 1/6；OS 84/84，6/6。前者不是 95% 准确率，但也不是几乎不能理解材料。九例扩展则为 Codey 5/9、OS 6/9，差距远没有两个短主题显得那么绝对。

同模型 OS 的成功是有意义的可达性证据；它没有给出通用研究正确性的保证。材料固定、题目反复用于开发、样本少，仍需区别于真实开放检索研究。

## 7. Codey 具体应当怎样收敛

### 7.1 保留事实与发布内核，调整模型请求投影

优先做一个有边界的请求组织改动，不再扩展研究状态机：

| 当前职责 | 建议收敛方式 | 主要代码位置 |
| --- | --- | --- |
| 原任务嵌入工作包 | 每个请求从原任务记录派生独立、完整的 user 任务；后续用户补充也按原日志保留 | kernel_prompt / kernel_context / 请求投影 |
| 稳定原则与动态工作状态混合 | 研究原则放稳定 system/developer，动态数据保留数据角色；禁止把抓取原文提升成 system | kernel_preparation / provider 请求准备 |
| tool 指针 + user 再展示原文 | 把获准窗口的 eN 与原文放在工具数据呈现中；超出最近调用的已读材料采用受支持的资料视图，不能伪造未发生的 tool_call_id | task_result_context / answer_context / codec |
| 收据、定位、历史占据作答提示 | 正常阅读只展示必要来源、范围、完整/截断状态；失败时展示实际错误和可执行恢复信息 | answer_context / context_view |
| 只保留最后协议组 | root user、当前必要完整调用组与预算内证据共同投影；修复时保留对应失败输出 | api_request_view / api_provider |

这里的“放在工具数据呈现中”不要求把全部旧原文重新装进最后一次工具结果。旧证据必须保持真实来源和回执归属；provider 不支持的角色要由原 codec 规范降级。做到一次显示、来源可核查即可，不应为了模仿 OS 伪造历史调用。

先保留现有 submit、状态声明、冻结范围、引用准入和发布事务，单独判断输入投影是否改善。这样改变的是模型看到什么，不是另建一套研究运行时，也不是取消来源安全。

### 7.2 修复协议失败时，不应把“重新写答案”当成“重发”

这是比继续增加写作规则更有明确证据的修改目标：

- 原失败响应来自现有 provider 响应记录；在修复视图中引用/保留它，不另写一份答案作为事实来源。
- 修复原因针对本次失败：已经有正文但缺少提交动作，与工具参数本身无效分开说明。
- 只做格式修复时，应明确保护已有正文，不能悄悄要求一次新的完整研究写作。结构修复成功不代表内容变好。
- 一样的完整 payload 已重发且没有信息变化时，应停止无效修复，而不是把同 seed 重发当作新研究。

若后续单独证实“允许自然终结正文”有稳定收益，才把它设计为**原 kernel 支持的一种完成输入**：同一响应记录→同一冻结读取范围/引用校验→同一提交 effect→同一 answer 发布事务。

这需要显式处理状态：自然正文没有 answered/partial/unresolved 声明时，不能偷偷默认 answered；要定义“未声明/未评定”的兼容语义，或者采用能够明确声明状态的原生完成合同。不能用“没报错就是 answered”替代研究状态，更不能让普通聊天正文绕过原提交校验。本文不建议立刻把这一合同变化和输入投影一起上线。

### 7.3 材料访问由工程承担更多确定性工作

在现有来源工具上处理即可，不增加独立检索代理：

1. 完整小中型来源能放入真实输入预算，就整篇显示；材料预算必须同时考虑工具输出和当前答案视图。
2. 大来源明确显示真实阅读区间与未读区间；next_offset 不能暗示中间已经连续读完。
3. 目录不能静默只展示前若干节；应明示省略，提供从原文确定生成的章节查询及 offset。
4. 同来源连续重复读取开头，应在原结果/进展反馈中给出本次已读范围和续读动作。不要改成框架按题意自动“找到标准答案”。
5. 正文、标题、URL 和 eN 从同一归档与读取回执派生，目录和索引只读可重建，不独立维护第二套材料事实。

来源权限、冻结版本和真实读取证明照旧；不应以减负名义把“可检索到”自动当成“已读可引用”。

### 7.4 不要再增加哪些东西

不建议恢复强制 finding/采用/逐题状态；不建议加入同模型审稿闸门；不建议复制 OS 整个运行时；不建议再加入另一种可写的 answer/checklist 真相；也不建议看到一个漏句就向全局提示追加一条针对样题的规则。

OS 成功短题没有依赖这些机制。当前真正缺的，是可重建、可核查而又便于模型阅读的请求接口，以及不损坏已有答案的完成路径。

## 8. 怎样避免继续无止境地修改

这次源码审计本身不证明上述候选一定有效。下一轮只应回答一个有限问题：**保持同一模型、材料、引用和发布内核，把 Codey 的请求组织接近 OS，是否减少遗漏与错误扩写？**

可使用现有测评代理和脚本做一次冻结对照，不另建 runner：

- 先固定当前 Codey，以及 OS 原生成功接口作为锚点；保留实际 payload，不仅比较最终报告。
- 先比较“当前投影”和“角色分层、原任务稳定保留、原文只出现一次”的投影，提交仍用同一 submit。不要同时换提示、输出格式、模型和材料。
- 只有输入投影收益仍不足且存在交付/重写差异时，才单独比较工具提交与自然正文，不能把这两种改变混成一个结论。
- 已反复使用的题只验证机制；另加事先冻结、未参与提示修改的新材料/题目，预先明确评分必要项。按相同预算成对运行，统一人工关系评分。
- 同时记录首次正文、正式交付、修复次数、材料实际可见范围；若首次正文正确而正式正文退化，不能只把它登记为模型内容失败。
- 预先设一轮预算和停止条件。收益不稳定就停止这条修改路线，不继续根据单例追加规则；有稳定收益再做一个生产合并。

此前直接 Markdown 诊断保留了 Codey 的材料组织和任务提示，因此只能说明“去掉 JSON 输出并不能自动修好内容”。它不能排除 system/user/tool 布局、稳定任务保留和证据呈现的影响。上一轮建议若被理解为“排除了格式，所以剩下只能是模型能力”，需要据此修正。

本轮只完成源码与历史请求审计，**没有完成新的实机因果验证**。最新 de3edcb1 仍不能借历史 OS 成绩或本报告宣称验收通过。
