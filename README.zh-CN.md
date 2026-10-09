# Codey

**让你已经能用的网页 AI，安全地在本地帮你写代码、查资料、跑验证。**

[![版本](https://img.shields.io/badge/version-0.5.11-blue)](CHANGELOG.zh-CN.md)
[![许可证：GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![本地优先](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#安全边界)

[English](README.md)

版本：`0.5.11`

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、MiMo、StepFun、Qwen 和
GLM，也可以连接本地 OpenAI-compatible 模型或支持的 OpenCode Zen 免费模型，
然后把它们接到你电脑上的受控工作区。

它的目的有一点“平权”：AI 编程不应该只属于买得起高价 API 或昂贵订阅的人。Codey
让新手和独立开发者可以先用自己已经能访问的网页 AI，在本地看到改动、运行测试、查看
diff、必要时恢复，并在需要时做带证据的研究。

## 它是什么

- 一个本地/桌面 AI 工作台，用来聊天、写代码、审查和研究。
- 一座把网页版 AI 接到本地项目文件夹的小桥。
- 一个受控工具闭环：读取、编辑、测试、diff、Review、Restore。
- 共同任务闭环上的严格 Research 要求：引用实际打开过的来源，不把搜索摘要当证据。
- 一个有界本地记忆层：可以检查、导出、删除、重置或禁用。

Codey 不是云端代码托管 agent，不是插件市场，也不是让网页 AI 暗中访问整台电脑的工具。

## 快速开始

安装依赖：

```powershell
pip install -e .
```

启动 Codey：

```powershell
python -m codey
```

Codey 会打开本地 UI：`http://127.0.0.1:<port>/`。模型网站只在发送时按需打开，
启动或选择模型不会自动打开网站。第一次打开某个网页 provider 时，
在专用浏览器窗口里手动登录一次。之后选择项目文件夹并直接描述任务；如果只想普通聊天，
留在 `New Chat`，不要选择项目。

Codey 打开的本地 UI 会自动完成操作者认证。原生窗口无法打开时，使用终端打印的
完整启动链接。链接只能使用一次、五分钟后过期；已使用或过期时，重启 Codey 获取
新链接。这不需要额外的 AI 账号。

Settings → Models 决定对话框显示和可用的模型。Websites 默认选择五个已注册网站，
API 来源默认关闭且未选择模型。每个来源共用总开关和模型复选框：关闭保留子集，
零选择自动关闭；勾选模型或 Select all 后启用，再 Save changes。刷新不自动选择
新增模型。全部关闭仍保留草稿和历史，Send 禁用。

草稿原文、光标和选区随每个聊天保存在本地，重启后恢复。Clear messages 保留
未发送输入；删除聊天同时删除草稿。保存失败时保留输入，在文件夹与 Research
同行右侧显示 `Not saved · Retry`，不挪动输入框；重试原位显示 `Saving…`，正常
后台保存保持安静。主动上滚
即停止跟随输出，靠近底部也不拉回；实际到底部或 Back to latest 才恢复。
选中回答中的文字后，可用 Quote in reply 将可编辑 Markdown 引用追加到草稿，
不会自动发送。

Save changes 只保存模型选择，Save connection 只保存连接字段，各自有修改时
才可保存。刷新模型保留未提交选择、搜索和焦点。Changes 刷新时仍可阅读当前
diff；失败明确标记旧结果，成功更新前禁止 Restore。

使用本地模型时，打开 Settings → Local → Connection，填写 OpenAI-compatible
base URL、model ID 和可选 API key。Save connection 后 Refresh models、勾选模型，
再 Save changes。名称来自实际元数据或可选显示名，不固定为某个模型。
Connection → Advanced → `API protocol` 明确选择 Chat Completions
（`/chat/completions`）或 Responses（`/responses`）；`Tool calling` 另外控制
原生工具调用或文本工具请求。

使用 Zen 免费模型时，在 Settings 展开该来源、Refresh models 并明确勾选；
只有来源开启且已选择的模型进入对话框菜单。目录取公开目录与实际 endpoint 列表
的交集，只收录明确零费用且协议受支持的模型；断网保留有界缓存。
本合作连接不要求注册或个人密钥，但每次请求的资格与工具限制仍由上游检查；
免费列表不代表所有任务模式都能访问。已验证 Muse 编码与 Space Bunny 审查，
具体范围见[测试报告](TEST_REPORT.md)。

详见[模型管理与可选连接拆除](docs/model-management.md)。

请求上下文与累计 API 用量分别统计。Run details 标明上次准备的请求，估算带 `~`，
缺失用量明确显示不完整。确认支持 Jinja 的 KoboldCpp 对完整请求分词；其他 Local
连接与 Zen 请求前计数明确为估算。实际 API 用量只来自服务端报告，不能把分词计数
当作消耗。详见[请求预算与 token 统计](docs/token-accounting.zh-CN.md)。

API 会话在后台维护经过计数的工作上下文，并保留已接纳的原始事件。执行回执
独立于模型摘要，读取已有测试结果不会重新执行命令；维护不增加聊天进度提示。
可逆工具输出视图与简洁工作状态减少重复输入，保留独特观察与当前任务内的回执读取。
详见[最新优化对比与验证边界](docs/context-compaction-optimization-2026-10-09.zh-CN.md)。

## 命令行

```powershell
# 单次聊天，不写文件
python -m codey chat "用一句话解释 Python 的 GIL"

# 指定 Qwen
python -m codey chat --provider qwen "用一句话解释 Python 的 GIL"

# 直接运行 agent
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "修复失败的测试"

# 输出 JSONL 事件流，方便脚本、CI 或 benchmark 消费
python -m codey agent --json --provider qwen --project E:\my-project "修复失败的测试"

# 指定免费 API 模型，协议来自当前目录
python -m codey agent --provider zen --model muse-spark-1.3-contributor-free --project E:\my-project "修复失败的测试"
```

`agent` 与桌面共用任务服务和授权规则，默认是 `project`；`--auto` 自动选路，
`--intent` 可指定 `chat/research/hybrid/review/planning_readonly`。聊天和 Research
不必关联项目；`--readonly` 选择只读规划；`--allow-web/--allow-write` 提供明确授权，
仍受任务的禁止项约束。`--session-id` 选择会话，`--continue` 继续该会话；
审查复用使用同一会话及 `--review-source-run-id` 指定的历史 run ID。
非交互 shell 审批仍拒绝。单次 `chat` 命令是 provider 工具；
`agent --intent chat` 才走记录运行事实的任务流程。
API 任务可用 `--model`；`--effort` 要求同时指定模型，并符合公布的能力。
桌面、CLI 与 headless 在接纳任务时正式保存模型、协议和生成参数。
之后修改 Settings 不改变旧任务；原连接不可用时，恢复会明确阻塞。

Chat Completions 和 Responses 共用取消与结果交付规则。Zen 是可移除的连接；
移除后 Local API 和已保存的聊天、运行历史仍可使用。

CLI、网页事件和 headless JSONL 共用运行身份与工具状态。恢复保留原任务要求；
已结算结果可继续交付，未结算的危险写操作不会被盲目重试。

## 文档

只读审查使用实际选定的 Reviewer，按真实输入范围校验问题并检查工作区是否变化。
部分范围或不可用结果会明确显示；审查通过不能替代测试。显式
`review_source_run_id` 只在同会话/项目、输入与已知 API 模型配置匹配、快照仍有效时
复用已完成结果；不提供来源就进行新审查。Run Details 展示有界状态，经过认证的
`GET /api/run_review` 从正式存储恢复问题。详见[当前职责地图](docs/project_structure.zh-CN.md)。

桌面与 CLI/headless project 共用自动审查阶段。默认优先使用可用网页 Reviewer，
API Writer 在目录中有可用独立模型时先选择另一个模型 Review；独立只读 Review
使用用户选定模型本身，没有独立模型时仍按原策略决定是否自审。
未知交付不会换模型重发。其他情况下可用 Writer 模型开启新自审会话；
具体问题最多修复一次，普通非 Git 文件夹
也可使用。`--review-policy require_web` 要求网页 Reviewer，嵌入测试门可显式固定
Reviewer 连接器。审查通过不能代替新鲜验证。
详见[桌面/CLI 一致性验收](docs/desktop-cli-task-parity-2026-10-04.zh-CN.md)。

- [详细能力说明](docs/codey_capabilities.zh-CN.md)
- [路线图](ROADMAP.zh-CN.md)
- [版本更新记录](CHANGELOG.zh-CN.md)
- [项目结构与职责地图](docs/project_structure.zh-CN.md)
- [0.5.11 发布验收](docs/release_0.5.11.zh-CN.md)（[早期版本提交记录](docs/release_0.5.10.zh-CN.md)）
- [Ghost 未来方向](docs/ghost_future_direction.zh-CN.md)

## 安全边界

模型只能在你选择的项目文件夹里工作。本地动作会经过 Codey 的工具契约、权限配置、
action policy、completion proof 和 Research evidence 检查。审计视图使用有界摘要和引用；
本地受管输出与恢复收据可能保留读取的源码、网页正文和工具结果。对话存储与启用的 Ghost
经历也会在本地保留用户消息/回答；这与仅记录 manifest/digest、不记录 raw prompt 的
提示追踪不同。

网页 provider 会改版。Codey 把不同网站的 adapter 隔离起来，所以网页坏了主要修对应
adapter，不需要改 agent 核心。

Zen 约定的上游请求头与工具名称映射只位于独立连接包，不进入 Local 请求或提示词。
生成请求只发送一次，断流或结果未知时不自动重发；完成证明与最终结果交付分别记录。

Zen 临时请求适配会补充标为不可用的网络声明，不授予文件或命令权限。
独立 API 审查固定使用本次所选模型，不会因为打开了网页模型而切换过去。
边界和实机结果见[请求适配说明](docs/zen_request_profile_2026-10-08.md)。

## 开发

```powershell
pip install -e .[dev]
python -m pytest -q -o faulthandler_timeout=120
```

离线 kernel parity 门：`python tools/kernel_parity.py --report parity.json`。
固定旧版 oracle、覆盖边界和差异裁决见[确定性审计报告](docs/kernel_parity.zh-CN.md)。

CI 还检查 Ruff、全树 mypy、JavaScript 语法和支持平台的回归。开发环境需要与 CI
一致时，先安装 `requirements-ci.txt`，再运行 `pip install -e . --no-deps`。
发布检查及实机覆盖范围见[发布门槛](docs/release_gate.zh-CN.md)。

## 许可证

GPL-2.0-only
