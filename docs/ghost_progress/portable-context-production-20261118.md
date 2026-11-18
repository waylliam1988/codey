# 本地偏好、项目经历与目标的通用生产入口

日期2026-11-18；本记录与生产实现同批提交到master，状态`PRODUCTION_MERGED`。
生产提交：`git log --grep="generalize confirmed local context across providers"`。
[实机记录](portable-context-result-20261118.md)和[冻结协议](portable-context-protocol-20261118.md)独立保存。

## 默认行为

| 内容 | 正常消费者 | 范围 |
|---|---|---|
| 人工确认偏好 | Chat、项目writer、planning、Research共享kernel入口 | user跨本机聊天/项目；project仅本项目；session仅本会话；更具体范围优先 |
| 跨会话项目经历 | 项目writer及有明确项目authority的共享入口 | 仅同项目已完成经历；Research不导入原始聊天经历 |
| 人工确认项目目标 | writer、planning、Chat的项目上下文、Research | 普通项目目标原话；同项目跨会话；独立目标名；原存储目标保留旧数据兼容 |

不再检查固定模型名、loopback地址、stream、temperature或某一组output/context/reserve/keep参数。
用户正常切换API/模型，已保存的本地状态和消费资格继续保留；故障、scope不符、关闭或无有效源时不投递。
Chat Completions/Responses的真实HTTP/SSE fixture测试通过；Gemma的流式/非流式实机冒烟见独立记录。
这描述上下文可用性，没有声称每个模型都获得同等行为收益。

支持的偏好仍是显式字段：中文、简短/详细、先给答案、列表/表格/Markdown、直接/技术/温暖语气。
本轮增加warm选项，面板默认记到本机user范围，用户可以改为项目或会话。
不自动从每条消息抽取永久偏好。当前明确指令继续优先。

面板“Remember preference”表单也提供Project goal。目标必须摘自已保存用户原话，先进入pending，
用户再Accept。可以为目标命名；纠正同名已审核目标时，绑定实际更早交流及完整回答digest。
已确认目标在面板可见并可Forget；项目面板列出本项目跨会话经历供选择，不显示其他项目经历。

输出token上限继续由连接配置决定，128/8192只是历史实验参数。投影仍受有名的字符预算约束：
偏好900、项目经历1800、目标600及目标历史3200。多个目标按新旧顺序选择完整来源/目标对，
放不下某个目标不压掉其他可容纳目标；不截断来源冒充完整绑定。原话截取与可检索窗口限制仍适用；纠正绑定需要未截断的早先回答，当前Ghost保存回答的上限仍是4000字符。

## 实际接线与边界

- `ghost_context.py`直接渲染所有支持的已确认偏好，并在物理发送前重读每个非空偏好投影。
- `project_adapter.py`在正常writer入口重新读取偏好、经历、目标，渲染同一快照；恢复的旧来源不能借用新资格。
- `personal_context.py`从明确的durable sink/本地state authority补充缺失来源，经独立ContextSource与现有admission。
  已有整体源validator的adapter保留其所有权，不建立绕过故障回退的第二条读路径。
- Research只添加个人偏好/确认目标；它们不产生evidence_refs、事实、工具权限或完成proof。
- session检索先按当前项目过滤，再排序，防止同一聊天切换项目时混入别处经历；低层历史审计视图仍保留原始记录。
- 保留retrieve/admit关闭、来源窗口、源变更、撤回、删除、重建、重启和发送前校验。
  没有经历时不发送只有标题的虚假历史来源；重复确认的历史经历也保留在预算内。
- 产品不加载本地HF，不增加Ghost生成、第二遍最终回答或额外模型评分。

原中文、项目经历和存储目标的完整效果实验继续作为对应功能的历史证据。
本轮普通目标、暖语气和配置泛化有正常入口/生命周期验收；实机的单个合成经历只作冒烟，
严格短标签指标的失败及流式用量缺失没有被放宽或删除。没有World Model生产晋级。

## 关闭与回滚

用户可按user/project/session关闭`admit_hints`或`retrieve_projections`；单项偏好或目标可以撤回，
经历可按scope删除。关闭投递不删除历史，删除/重置的既有持久生命周期保持有效。
没有新增只能开发者使用的隐藏启用开关。
代码回滚使用本生产提交的revert，并一起回滚发布静态资源；本地数据格式可继续保留，
旧版本忽略新的generic goal消费者和warm字段，不把它们自动转成旧存储优先级。

## 验证

最终相关回归：237 passed，19 subtests passed（289.53秒），没有运行全量pytest。
命令为`python -m pytest`加以下文件，再加`-q --maxfail=3`：

```text
tests/test_ghost_portable_context.py
tests/test_project_goal_production.py
tests/test_project_experience_production.py
tests/test_ghost_language_production.py
tests/test_ghost_experience_role_budget.py
tests/test_ghost_post_turn.py
tests/test_ghost_post_turn_affinity.py
tests/test_ghost_post_turn_work_queue.py
tests/test_ghost_post_turn_shared_workspace_and_explicit_workflows.py
tests/test_ghost_context_admission.py
tests/test_permission_profiles.py
tests/test_local_context_render.py
tests/test_research_iteration_topic_continuity.py
tests/test_frontend_build.py
tests/test_frontend_source_fingerprint.py
```

全仓`python -m ruff check .`、完整生产树`python -m mypy codey --python-version 3.11`
（706文件）、`pyrefly check codey --python-version 3.11`、
`ty check codey --python-version 3.11`及`python -m tools.kernel_type_precision_gate`全部通过。
Pyrefly仍报告35项既有suppression及160条warning；不将退出码0描述成没有历史抑制。
本轮22个Python源文件、测试、工具与实验脚本另经mypy、Pyrefly、ty专项检查通过
（专项Pyrefly另有1条warning）。额外开启全部五项`--disallow-any-*`检查时，
本轮文件0条诊断，但命令因330个未修改依赖文件的11818条诊断退出1，不能称该命令整体通过。
没有为本轮改动新增豁免、cast或ignore。

前端`npm run check`通过64个测试文件、400项测试及类型/格式检查；`npm run build`、
`python -m tools.publish_frontend`及`python -m tools.publish_frontend --check`通过。
另用无头Chromium核对默认user范围、普通项目目标选项、目标名及早先交流选择控件。
原始检查输出留`.e2e-artifacts/ghost-portability-*`，不提交用户回复和state。

## 剩余工作

自然语言语气质量、多种模型的行为收益、自动理解/反馈写回、近期处境/临时偏好、失败任务经历、
完整有收益World Model仍需各自验证。本轮没有改变历史负结果或恢复用户已中断的条件接受率实验。
