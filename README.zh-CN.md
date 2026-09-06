# Codey

**让你已经能用的网页 AI，安全地在本地帮你写代码、查资料、跑验证。**

[![版本](https://img.shields.io/badge/version-0.5.8-blue)](CHANGELOG.zh-CN.md)
[![许可证：GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![本地优先](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#安全边界)

[English](README.md)

版本：`0.5.8`

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、MiMo、StepFun、Qwen 和
GLM，也可以连接本地 OpenAI-compatible 模型，然后把它们接到你电脑上的受控工作区。

它的目的有一点“平权”：AI 编程不应该只属于买得起高价 API 或昂贵订阅的人。Codey
让新手和独立开发者可以先用自己已经能访问的网页 AI，在本地看到改动、运行测试、查看
diff、必要时恢复，并在需要时做带证据的研究。

## 它是什么

- 一个本地/桌面 AI 工作台，用来聊天、写代码、审查和研究。
- 一座把网页版 AI 接到本地项目文件夹的小桥。
- 一个受控工具闭环：读取、编辑、测试、diff、Review、Restore。
- 一个 Research 闭环：只能引用实际打开过的来源，不把搜索结果当证据。
- 一个有界本地记忆层：可以检查、导出、删除、重置或禁用。

Codey 不是云端代码托管 agent，不是插件市场，也不是让网页 AI 暗中访问整台电脑的工具。

## 快速开始

安装依赖：

```powershell
pip install -r requirements.txt
```

启动 Codey：

```powershell
python -m codey
```

Codey 会打开本地 UI：`http://127.0.0.1:<port>/`。第一次打开某个网页 provider 时，
在专用浏览器窗口里手动登录一次。之后选择项目文件夹并直接描述任务；如果只想普通聊天，
留在 `New Chat`，不要选择项目。

如果要用本地模型，选择 `Local`，填写 OpenAI-compatible base URL、model id 和可选
API key。

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
```

## 文档

- [详细能力说明](docs/codey_capabilities.zh-CN.md)
- [路线图](ROADMAP.zh-CN.md)
- [版本更新记录](CHANGELOG.zh-CN.md)
- [Ghost 未来方向](docs/ghost_future_direction.zh-CN.md)

## 安全边界

模型只能在你选择的项目文件夹里工作。本地动作会经过 Codey 的工具契约、权限配置、
action policy、completion proof 和 Research evidence 检查。Codey 会保存有界本地事实，
用于审计和恢复，但避免保存 raw prompt、完整聊天记录、源码全文、网页正文、cookie 或密钥。

网页 provider 会改版。Codey 把不同网站的 adapter 隔离起来，所以网页坏了主要修对应
adapter，不需要改 agent 核心。

## 开发

```powershell
pip install -r requirements.txt
python -m pytest
```

## 许可证

GPL-2.0-only
