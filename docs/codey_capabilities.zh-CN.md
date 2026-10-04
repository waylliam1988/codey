# Codey 能力说明

这份文档承接 README 里移出的功能细节。它是产品能力概览，不是 release gate。

## 模型接入

- 网页 provider：DeepSeek、MiMo、StepFun、Qwen、GLM。
- 本地 provider：任意 OpenAI-compatible endpoint，可选 API key。
- 网页 provider 不需要 API key；你在专用 Edge 或 Chrome profile 里登录已有账号。
- 不同网站的浏览器自动化代码隔离在各自 adapter 里，网页改版时尽量只修对应 adapter。

## 工作模式

- `New Chat`：普通聊天，不开放项目文件。
- `Choose folder`：把当前对话接到一个本地项目文件夹。
- `Research`：为当前请求启用严格证据、账本与报告要求。
- 自动模式下，主模型的首次正常调用直接作答，或请求原任务策略允许的动作。
  工具调用与尚未完成的回答继续经过同一内核；模型给出的模式标签不能扩大权限。
  没有独立的路由模型调用；写项目必须持有 writer lock。
- 手动选择、项目范围和权限设置始终优先于自动模式。

编程、规划、Research 与获授权的混合任务共用工具循环。联网需要任务授权；
普通联网编程不会因此被要求写 Research 笔记。职责见[源码地图](project_structure.zh-CN.md)。

## 编程闭环

在选定项目目录内，Codey 可以让模型读取文件、编辑文件、运行允许的命令、查看 diff、
审查改动，并在需要时恢复 snapshot。

本地闭环保持可见：

```text
读取 -> 编辑 -> 运行/检查 -> diff -> review -> done/blocked
```

有 Git 会增强体验，但没有 Git 也能用非 Git diff 和 restore 开始工作。

桌面与 CLI/headless project 共用服务组合和审查策略：优先可用的网页 Reviewer，
否则在策略允许时开新会话自审。具体问题最多进入一次 Writer repair；审查通过
不能代替新鲜验证。CLI 提供全部任务 intent、明确联网/写入授权、继续会话及
显式审查复用。嵌入测试门可固定 Reviewer 连接器；展示与非交互 shell 审批保持
入口各自的适配。

文本工具支持有界只读批次 `read_files / parallel`：整批校验后按顺序执行。
结果不变的重复循环会有界停止，新信息与成功编辑继续推进。
取消会阻止后续动作，包括同批尚未开始的工具。

## Research 闭环

Research 可以搜索网页、打开 HTML/PDF 来源、保存有界笔记，并生成带引用的 synthesis。
最终 claim 必须绑定到已打开来源里保存的 evidence；搜索结果、本地记忆和 Ghost continuity
都不是 evidence。

返回和保存的 synthesis 使用同一份 evidence/引用编译结果，未支持结论降为限制，
并保留最多 4 个追问。

医学和论文类问题会优先打开 PubMed/arXiv 文章结果。宽泛首页会在有更具体来源时被跳过。
如果 proof review 发现明确证据缺口，Codey 可以跑一轮有界 evidence-only follow-up，
再由本地确定性合并新 evidence。

## 验证与完成

代码改动后，模型说 `done` 不等于任务真的完成。Codey 会根据新鲜检查、变更文件、
观察到的失败和 repair context 记录本地 completion proof。

- 新鲜通过的相关检查可以完成任务。
- 用户明确要求跳过验证时予以尊重，但不会宣称检查通过。
- 缺少验证、验证失败或环境损坏会诚实 blocked。
- 观察到产品失败时，可以允许一次有界 facts-only repair round。
- edit/test integrity 可疑时，任务收据会标出需要检查，而不是显示 clean。

## 本地记忆

Ghost 是 Codey 的有界本地连续性层。它记录已完成回合的经历（用户原话、最终回答），
并在下一次正常模型调用前按严格字符预算（chars，不是 tokens）检索少量相关过往回合。
只有成功结束的回合可被检索，按 run id 幂等。Ghost 自身不增加任何模型调用：没有每轮
路由调用，没有每轮抽取调用，也不要求主模型填写隐藏字段。

Ghost 状态必须可控（预览、导出、删除、重置、禁用同时作用于经历观察与旧存储）：

```text
预览
导出
删除
重置
禁用
```

它不是 evidence，不是 permission，不是自动化系统，也不是第二个 agent。

## Runtime 与恢复

本地 HTTP/SSE 要求浏览器通过 Codey 启动链接建立操作者会话。这与任务授权独立，
不会授予模型联网、写项目或 shell 权限。桌面与 CLI/headless 通过相同规则推导任务授权。

Codey 会记录有界 runtime fact，让中断后的工作更容易解释和恢复。Provider send、
tool call、repair round、delivery receipt 和 completion proof 都通过 durable
intent/settlement 风格记录。

恢复策略保持保守：

- safe read/search effect 可以重放；
- unsafe 或结果不确定的本地 effect 不会静默重复；
- 缺少 settlement 会显示为 interrupted 或 unknown-outcome step；
- Run Details 可以解释发生了什么，但不暴露 raw prompt 或 raw output。

## 审计入口

Codey 提供一些安静的审计入口：

- task receipt；
- Run Details；
- Local context drawer；
- prompt envelope manifest，按 digest 和 source refs 审计；
- Research evidence/source/note 视图；
- 有界 run trace 和 ledger。

审计摘要与提示 manifest 是有界投影。本地受管输出和恢复收据可能保留读取的源码、
网页正文和工具结果；对话存储及启用的 Ghost 经历也会保留用户消息/回答。提示追踪
不记录 raw prompt。这些存储承担不同职责，摘要视图不能被理解为本地状态绝无原始内容。

## 当前边界

Codey 当前不开放公共插件系统，不让 Ghost 或 World Model 裁定事实，不把 provider 原生
搜索当成 Codey evidence，也不允许 adapter repair 修改 runtime core。

后续 runtime 重构方向见
[Codey Pi v2-inspired 重构方向](codey_pi_v2_refactor_direction.zh-CN.md)。
