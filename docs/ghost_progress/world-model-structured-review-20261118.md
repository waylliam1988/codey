# World Model：结构化复盘传输对照

- 日期：2026-11-18
- 状态：`ABANDONED`（接口有效，完整对照无预测复盘增益）
- 基线：`f5386b35`；关联 0.6.12。
- 前序：[文本复盘格式负结果](world-model-verification-review-20261118.md)，原 40 个样本保留，不重判、不替换。
- 冻结提交：`7487bfbf`；实验与负结果提交：`3419feaa`（完整 SHA 见脱敏报告）。
- 生产状态：未晋级，无新增生产模块或默认关闭功能；无需生产回滚。

## 目标、主要变量与假设

前序严格 JSON scorer 的 on/off 均为 0/20，但全部是围栏格式失败，无法判断复盘内容收益。
本轮固定同一结构化传输在两臂共同使用，再隔离测量 reviewed prediction projection 有无
额外收益。不是剥围栏、修模型输出或放宽 scorer，也不把 wire 格式改善归因于 World Model。

复用前序真实 Python check→现有 completion owner proof→已提交 bounded fixture→预测→
verification review→有 TTL projection→独立 ContextSource/PromptEnvelope/admission→
只读模型复盘消费者。不新造事件系统，不新增模型权限、evidence_refs 或完成事实。
前序五个 fixture、四种问法、动作期望、所有安全边界及原 action scorer 保持不变。
本候选仍只是对已知类别的持久性预测，不将固定 confidence 0.6 声称为校准概率。

## 冻结配置

- 模型：koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M。
- endpoint：http://127.0.0.1:5001/v1；openai-completions；temperature=0；native_tools=false。
- context/reserve/keep_recent：262144/32768/32000；output_tokens=256；timeout=120s；单次尝试。
- 新配置：两臂都在实际 API payload 设置 response_format.type=json_schema，strict=true；
  schema 为单一 action 字段，enum 为 inspect_environment、report_verified、inspect_source、
  verify；required=[action]；additionalProperties=false。不增加 prompt 答案或启用工具。
- 五类任务：重复环境失败、环境修复后成功、产品失败、缺后续 verification、旧成功但
  workspace 已变；对应期望 inspect_environment、report_verified、inspect_source、verify、verify。
- 每类四问法，20 题/arm，共 40 新请求；同题交替 on/off 顺序，每请求独立空历史。
- 唯一 on/off 变量为有界 World Model projection。两类 unjudged 的投影为空，作为零扰动
  对照；既有事实、runtime 指导与 JSON schema 两臂相同。
- 原 scorer 原样 import，严格单字段 JSON，不剥围栏、不修回复、不做第二模型判断。
- 保留原始 prompt/reply/usage/error、wire schema 配置与源码 SHA256 于新本地隔离目录。
  仓库只保留复现入口、必要 fixture 代码、脱敏指标、完整命令及结论。

## 指标、晋级与停止条件

完成两个 arm 全部样本后，主要指标仍为严格 action 正确数；on 比 off 至少多 3/20，
五类逐类不退步，且无 unjudged→已验证、跨 scope/过期泄漏、跳过验证或来源失活后
投递，900 字符内无截断，才允许补本消费者生产边界并接普通入口、受测范围默认启用。
保留用户关闭、故障隔离、回滚和未测模型基线回退；未通过则不留无人调用的生产开关。

格式有效数、provider reported input/output tokens、延迟与实际投递字符单独报告；
格式适配改善只属于传输，不是 World Model 收益。只有两臂各至少 18/20 格式有效时，
才讨论语义收益；未达格式前提则不能把格式错误当语义阴性。

模型 ID 不符、服务错误、空/截断响应、无效 fixture 时立即停止并保存 INCOMPLETE。
首条响应若仍不满足原 JSON scorer 的结构要求，停止为 INCOMPLETE_INTERFACE：新传输
尚未证明可用，不继续堆积格式失败样本，也不把服务/接口错误计为模型内容阴性。
后续单条结构错误按原 scorer 计失败，不重试、补样本或临时改阈值。

若接口有效且完整矩阵没有净收益，终止“重复当前验证事实的持久性预测复盘”候选，
不再仅换提示词重跑；后续 World Model 须选择提供新增预测信息的真实消费者。

## 检查与当前边界

复用前序 24 项投影/准入/非法输入回归；补结构化 wire 的正常/未知 action/多余字段、
first-response 停止、不覆写旧证据、工具不执行及真实 HTTP payload 捕获。
相关回归后运行全仓 Ruff、覆盖完整 codey 的 mypy/Pyrefly/ty/precision gate，以及本轮
修改 Python 文件定向三类型及额外 Any 审计；不运行全量 pytest。

全 G0 仍缺稳定排序/digest、记录型 control payload、完整硬退出/跨 run 与混版本等边界。
本轮隔离消费者验收不替代其生产生命周期，也不宣布 0.6.12 整体完成。

## 实现、命令与实际结果

新增手动入口 `world_model_structured_review.py`，复用原 fixture、projection 和严格 scorer；
通过现有 ApiProvider.configure_request 设置 schema，并在 HTTP attempt observer 保存实际
请求体、计数及 SHA256。两臂传输配置相同，原不带 schema 的入口仍保留格式负结果，
不剥围栏。新增首响应格式停止、证据不覆写及无效 fixture/服务错误的失败记录。

```powershell
python -m tests.manual.ghost_memory_routing.world_model_structured_review --output .e2e-artifacts/world-model-structured-review-20261118/live --live
python -m pytest tests/test_world_model_structured_review.py tests/test_world_model_verification_review.py -q --tb=short
```

复现必须使用新隔离目录；已有 config/metrics/failure 均拒绝覆盖。实机前后 `/v1/models`
均为冻结 Gemma ID，源码先冻结再运行，40 次请求完整返回，无服务错误、空回复、截断、
工具执行、重试或补样本。实际 wire 的 schema 全部与冻结配置一致。

| 指标 | off | on |
|---|---:|---:|
| 完整且结构有效的 action | 20/20 | 20/20 |
| 原 action scorer 正确数 | 8/20 | 8/20 |
| reported input tokens 总数 | 5840 | 6792 |
| reported output tokens 总数 | 180 | 180 |
| 平均请求耗时（秒） | 0.5203 | 0.5304 |

产品失败、环境修复后成功两类，各臂各 4/4；重复环境失败、缺 verification、workspace
改变三类，各臂各 0/4，实际选择均为 inspect_source。预测复盘没有改变任一题的动作，
不将两臂共同失败归因于 World Model 泄漏，也不声称满足正确验证/恢复的安全收益。
输入额外 952 tokens（+16.3%）、4716 字符，没有达到净增至少 3/20 的冻结晋级门。
格式改善只归属共同结构化传输，不归属预测内容；前序 40 个围栏回复未重判。

12 个已复盘 on 请求经过独立 source/admission，8 个 unjudged on 请求无 source，
与对应 off 的原型 prompt digest 相同。实际 ApiProvider 另外加入每请求运行时钟观察，
时间在请求间变化；本轮没有冻结该正常机制，因此不声称完整 wire 逐字相同或严格因果
识别，也不据本机小样本声称显著延迟差异。该限制不提供晋级理由。

按预先冻结的无收益停止条件，终止“重复当前验证事实的持久性预测复盘”候选，不再只换
提示词重跑。没有生产消费者、默认启用范围或新 runtime 模块；停止手动入口即可隔离。
World Model 整体仍未完成。后续先继续 G0 的实际缺口；新候选必须提供新增预测信息，
选择真实运行恢复/验证消费者，独立冻结任务与成本，不能重复包装现有 completion 事实。

## 最终检查与剩余审查

新增 14 项实际 loopback HTTP 验收；加原型为 38 项。相关 12 文件 **206 passed /
31 subtests**，零失败、错误和跳过。全仓 Ruff、完整 702 文件生产树 mypy/Pyrefly/ty、
kernel precision gate 及三个修改 Python 文件定向三类型均通过。额外 Any 审计本轮文件
零诊断；命令 exit 1 为未修改依赖 4630 项/193 文件，未改检查范围或新增豁免。
全树 Pyrefly 既有 suppressed/warnings 仍存在，不声称全仓没有 Any。live 与最终检查的
三个源码 SHA256 相同，生产树冻结不变。不运行全量 pytest，无前端或静态资源改动。

初期 Ruff 的宽泛 raises 断言、mypy 的 JSON 容器不变性和测试 tuple 切片隐式 Any 已在
live 前修正。汇总时 PowerShell 空数组成员展开误计 null 为一项，已改为过滤 null 并对
同一 40 请求复查，未新请求、改样本或 scorer；这些均不计生产/模型阴性。

逐 cell 脱敏指标、检查命令及源码哈希见
[报告](../../tests/manual/ghost_memory_routing/reports/world-model-structured-review-20261118.json)。
完整 prompt/reply、wire、proof fixture、usage、XML、日志与失败检查仅留本地
`.e2e-artifacts/world-model-structured-review-20261118`。本轮仍不证明真实用户历史上的
收益、持久生命周期、可校准 confidence、跨模型能力、完整 G0 或 World Model 生产闭环。
