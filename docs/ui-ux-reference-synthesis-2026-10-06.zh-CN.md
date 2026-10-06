# Codey UI/UX 两方案复核与输入区建议

日期：2026-10-06。本文件补充朋友 1 的参考分析，结合朋友 2 的建议及当前本地源码；它记录设计决策，参考产品比较来自源码复核。后续落地与 Codey 界面验收见 [实施记录](ui-ux-implementation-2026-10-06.zh-CN.md)。

## 1. 判断：朋友 1 为主，朋友 2 作为候选功能库

朋友 1 更适合成为实施基线：区分同源项目、已有能力、实现缺口和后端前提，围绕草稿、提交、阅读、审批、焦点及检查范围提出验收。朋友 2 的优点是列举具体交互，容易转成候选任务；问题是把视觉兼容和实现成本混在一起，把一些已有能力算成新增能力。

不建议取两份方案的并集。先修当前路径，再吸收有明确使用场景的增强。

### 需要纠正的判断

| 朋友 2 的表述 | 复核结果及影响 |
|---|---|
| 五者都有三栏、常驻 composer、会话模型选择 | 三个 DSH 项目属于同源家族；opencodex 主 GUI 面向配置与运行管理，不能把聊天交互算成五者共有。跨产品应比较交互原则。 |
| 切 chat 草稿丢失 | 现状是共享 textarea 没有会话草稿存取：切换后原文字可能仍在，但归属变了，存在错投风险。刷新丢失是另一问题。 |
| 运行时全禁用 | Send、模型选择、目录/Research 等按全局运行状态受限，textarea 本身没有因此被禁用；可以写下一条，但缺少草稿归属。 |
| Codey 审批已接管 composer 槽位 | 现有审批卡在聊天记录内，未接管输入区。应先增加待审批定位入口，并保持唯一审批状态。 |
| @、/、Queue/Steer、Always allow 可以直接吸收 | 路径补全文本可以轻做；引用读取、命令执行、队列、运行中干预、持久授权分别需要实际协议或权限支持。灰色外观不等于行为已兼容。 |
| idle 显示 DeepSeek ready 即可 | available 主要来自网页 tab 检测，不能据此保证已登录、未限流或能够执行。新文案必须有可靠事实支持。 |
| 增加 Worked 统计 | Codey 已有按需 Run Details 的 Actions。先改善当前执行反馈；没有需要时不要常驻复制同一份统计。 |
| 需要复制/Markdown/工具折叠等新功能 | 消息复制、代码复制、Markdown、inline rename、成功只读工具折叠已经存在，DESIGN.md 的部分 Future work 落后于实现。 |

## 2. 真正值得吸收的共性

五个 checkout 更合理地分成 DSH 家族、Percho、opencodex 三类证据。

- **状态属于对象。** DSH/Percho 的输入属于会话，opencodex 的编辑属于配置对象。Codey 应保护草稿、光标、阅读位置，以及迟到请求响应的对象归属。
- **主路径简单，细节按需展开。** DSH/Percho 区分过程和答案，opencodex 将高级配置分层。Codey 的终答应默认展开，工具过程可以折叠，Details 保持按需加载。
- **反馈指向下一步。** 提交失败保住文字；配置失败留在原表单；待审批给出入口；检查改动说明项目和范围。
- **同一设置有唯一管理位置。** 保留会话级模型入口及 Research 位置；不增加同功能的顶栏、侧栏、输入区三份控制。
- **键盘和鼠标行为可预测。** 菜单打开、移动、选择、关闭和焦点返回一致；隐藏区域不接收焦点；新输出不破坏阅读和选择。
- **稳定正文与统一控件。** 继续使用 760px 正文、现有 tokens、sans/mono 分工和克制灰阶。参考产品并不共同采用严格单色或去气泡。

这些原则符合 PRODUCT_PRINCIPLES 的低门槛、可见可控、可恢复，也符合 DESIGN.md 的安静本地工具气质。玻璃、彩色胶囊、背景、宠物、市场和复杂工作台不属于必要吸收项。

## 3. 输入区：交互需要升级，视觉适合小改

Codey 的自动增高 textarea、Enter/Shift+Enter、IME 防误发送、会话级模型选择以及 Send/Stop 同位切换已经合理。DSH/Percho 的优势主要是完整的输入状态与菜单交互，不只是控件更丰富。

### 第一优先：保护输入及发送意图

1. 草稿按会话保存，切换恢复文本与选区。先保证内存隔离；跨重启保存再定义存储与清理范围。
2. 引入提交中的状态与重复提交保护。成功受理才消费提交快照；失败可恢复，期间新增文字不能被覆盖。
3. Choose folder 只修改项目上下文。当前 composer 在已有文字时传 sendDraft=true，可能选完目录就启动任务；建议改为用户再按 Send/Enter。
4. A 运行时查看 B，显示 `Running in <chat> · Open`，让 Stop 目标明确。B 可以保留草稿，但不绕过全局单任务能力。
5. 待审批在输入区附近显示 `Approval required · Review command`，定位到原卡。不要让发送用的全局 Enter 自动变成批准。

### 第二优先：模型菜单和发送控件

- 保留左模型、右 Send/Stop 的布局和单一模型入口。
- 模型菜单补打开后的初始焦点、方向键、Enter 选择、Escape 关闭及焦点返回；同步 aria-expanded/隐藏状态。
- 菜单较长时提供搜索和滚动高度限制，少量选项不强制新增搜索栏。分组只在真实目录有层级时使用。
- 不增加底层尚未支持的 reasoning effort 控件，不把网页连接入口伪装成可自由挑选的完整模型目录。
- 当前发送按钮 30×28px、图标 14px，视觉和点击目标较小。可试 32–36px 方形目标、16px 图标，保持灰阶、6–8px 圆角、无彩色实底。
- Send 仅在可发送时提高灰阶对比，禁用时仍较暗，hover 提示清楚；Stop 使用同一位置、同一尺寸及明确名称。

### 第三优先：把输入与操作归为一个整体

当前边框只包 textarea，模型与操作栏在框外，Choose folder/Research 又在上方；语义相关的三部分视觉略分散。可试把 textarea 与底部操作栏纳入同一个灰色 composer 外框，Choose folder/Research 保留上方安静一行。

```text
Choose folder · Research
┌────────────────────────────────────────────┐
│ Send a message to Codey…                   │
│                                            │
│ ● DeepSeek ⌄                Enter     [↑]  │
└────────────────────────────────────────────┘
```

重点是共同外框、对齐与足够点击面积。保留 760px、10px 圆角、现有 --bg-2/--border；不用复制 Percho 的 20px 外框、圆形实底按钮、附件/权限/思考档位簇。发送图标可统一为向上箭头，图标更换不是第一优先。

这是候选方案，应先核对短窗口、缩放、长输入与模型弹层遮挡，再决定。落地时同步 DESIGN.md §5.6 的组件示意。

## 4. 鼠标选择复制：应该修，桌面壳已有明确原因

当前 [server.py](../codey/app/server.py) 第 545 行：

```python
webview.create_window("Codey", launch_url, width=1380, height=900)
```

本机安装的 pywebview create_window 签名默认 `text_select=False`，其 js/customize.js 据此注入 body 的 `user-select: none`。Codey 的正文 CSS 本身没有全面禁止选择。因此，如果用户使用的是此桌面启动路径，这个默认值直接解释了无法鼠标选中文字。

建议在桌面窗口显式传 `text_select=True`。正文、代码、路径、命令、diff 和错误详情应可选择；操作按钮、展开标签等控件可以局部保留不可选择。原有整条消息及代码块 Copy 按钮继续保留，它们与局部选择互补。

这项修正不需要改变 Codey 的外观。不要只增加 Copy 按钮，也不要先通过全局 !important CSS 对抗桌面壳。

此外，renderChat 会清空聊天 DOM，部分完整重绘路径仍可能取消选区。已有浏览器测试验证工具替换时保留 assistant DOM/选区，但它通过 DOM Range 创建选区，且没有经过 pywebview，不能覆盖真实桌面鼠标选择。

实施后的验收应包含真实桌面拖选中文/英文段落、双击选词、跨段选择、代码/diff 选择及 Ctrl+C；工具更新时保留原选区和阅读位置。此分析确认了配置与注入代码，没有进行真实桌面修复后验证。

## 5. 建议顺序与暂缓项

| 顺序 | 内容 | 目的 |
|---|---|---|
| 1 | 桌面文字选择；终答默认展开 | 基本读、选、复制可用；修复违反现行设计的行为 |
| 2 | 会话草稿；发送恢复/防重；选目录与发送分开 | 保护用户输入和执行意图 |
| 3 | 模型菜单键盘；按钮面积/对比；共同 composer 外框试验 | 操作可达，输入区更整齐 |
| 4 | 工作归属；审批定位；阅读位置与 Back to latest | 长任务仍可查阅与处理 |
| 5 | 会话搜索；检查面对象归属；文件定位 | 历史增长后查找和检查仍轻松 |

@ 路径补全可作为下一阶段小功能；/ 命令应复用真实已有动作。Queue/Steer、Always allow、Turn rail、常驻 quota/context、hover 预览、多 tab 工作台先暂缓。它们或需要新增后端能力，或会增加用户需要理解的状态，收益尚未超过基础缺口。

## 6. 复核来源

- [朋友 1 分析](ui-ux-reference-analysis-2026-10-06.zh-CN.md)、[DESIGN.md](../DESIGN.md)、[产品原则](../PRODUCT_PRINCIPLES.zh-CN.md)。
- Codey：[composer](../codey/web/assets/composer.js)、[provider UI](../codey/web/assets/provider_ui.js)、[render](../codey/web/assets/render.js)、[index](../codey/web/index.html)、[styles](../codey/web/assets/app.css)、[desktop launch](../codey/app/server.py)、[DOM/选区测试](../tests/test_ui_inplace_render.py)。
- DSH：[conversation](../reference-projects/deepseek-harness/packages/client/ui-conversation/README.md)、[model selection](../reference-projects/deepseek-harness/packages/client/ui-model-selection/README.md)；桌面分支：[pinned upstream](../reference-projects/dsh-desktop-anywhere-labs/upstream.json)、[fsw frame styles](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-layout/src/client/AppFrame.module.css)。
- Percho：[Composer](../reference-projects/percho/packages/desktop/src/renderer/src/components/composer/Composer.tsx)、[ModelPicker](../reference-projects/percho/packages/desktop/src/renderer/src/components/composer/ModelPicker.tsx)、[drafts](../reference-projects/percho/packages/desktop/src/renderer/src/stores/drafts.ts)。
- opencodex：[components](../reference-projects/opencodex/gui/design-system/components.md)、[App](../reference-projects/opencodex/gui/src/App.tsx)。
- 本机安装依赖源码：`C:/Users/Administrator/AppData/Local/Programs/Python/Python312/Lib/site-packages/webview/__init__.py` 的签名/参数文档，以及 `webview/js/customize.js` 的禁止选择注入。依赖检查用于确认本机行为，不代表所有版本都相同。
