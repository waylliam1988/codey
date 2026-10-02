# KoboldCpp 12B 实机门槛测试（2026-10-02）

## 范围与实验条件

测试使用 `tools/local_model_release_gate.py`，agent 走正式 `run_headless` 和共同内核，
仅注入记录请求的生产 `LocalOpenAIProvider`。没有用假模型替代实机结果，也没有改用户保存的配置。
每个案例使用仓库外的临时项目和 state；完成后把项目、状态、事件、请求/响应和独立验证结果存档。
原始数据含完整提示与临时路径，留在忽略的 `.e2e-artifacts/`，不提交运行数据。

| 项目 | 本次观察 |
| --- | --- |
| 服务端 | KoboldCpp 1.117.1，`http://127.0.0.1:5001/v1` |
| `/models` 实际 ID | `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` |
| 客户端上下文预算 | window 32768 / reserve 8192 / keep recent 12000 |
| 服务端报告 | max context 262144 / default max length 1024 |
| 采样 | temperature 0；未发送 seed |
| 请求输出预算 | 未发送 `max_tokens`；不能把服务端默认值当作这次请求的硬上限 |
| 模板、实际量化 | 模板未报告；量化只见于模型名，未独立核验 |
| 运行环境 | Windows，Python 3.12.8 |
| 单案例截止时间 | 600 秒，包含模型、工具和独立验证；终止 owned process tree |

Git 基线为 `4404c033`。实验针对工作区代码，因此元数据同时保存脚本与修改模块的 SHA-256。
各批次过程中保持生产代码和脚本固定。批次之间才修改，不把不同版本拼成一个固定条件成功率。
前后提示和要求也有修正，不能解释成“只有内核修复造成的因果提升”。

## 判定器先锁定反例

- 聊天只接受去首尾空白后的精确 `KOBOLD_OK`；`NOT_KOBOLD_OK` 和附加说明都拒绝。
- 项目公式使用独立的多输入矩阵，拒绝 `return 5` / `return 80` 这类可骗过可见单例的实现。
- 原有测试保存哈希，删除或修改即失败；创建任务的生成测试必须拒绝隔离副本中的错误加法。
  这个突变检查只证明对一种错误实现的敏感性，不等于全部输入覆盖。
- 正常完成、独立产物正确、只读安全、Ghost 控制面分别计数；规划和讨论不自动评判答案质量。
- 混合任务必须有成功的 `web_search → open → read_file → edit → run → done`，
  验证必须有结构化 exit 0，任务事件属于同一个 run/session。搜索本身不能代替打开来源。
- 超时、异常也留在分母；每次尝试独立目录，失败不覆盖。真实子进程测试确认到期终止并保留部分项目。
- provider 日志记录逻辑发送及实际 usage/finish_reason，不冒充底层 HTTP 重试次数。

## 固定基线：原生协议三轮

目录：`.e2e-artifacts/local-model-release-20261002-113259-05f9eb09da`。

```powershell
python tools/local_model_release_gate.py --cases chat,read,create,edit,references,hybrid --repeat 3 --timeout 600 --json
```

| 案例 | 正常通过 | 独立产物正确 | 观察 |
| --- | --- | --- | --- |
| chat | 3/3 | 不适用 | 精确标记通过 |
| read | 0/3 | 不计项目任务 | 首次模型发送前发生状态转移错误 |
| create | 0/3 | 0/3 | 请求 shell mkdir 被拒；终态接线错误掩盖正常取消 |
| edit | 1/3 | 3/3 | 两次结束阶段连续截断 |
| references | 1/3 | 3/3 | 两次未正常结束 |
| hybrid | 0/3 | 0/3 | 无知识库时未构造网页执行资源，出现 unknown tool |

18 次中 5 次通过；12 次修改/混合任务中 2 次正常完成、6 次独立产物正确。
案例累计墙钟时间为 1836.221 秒，包含工具与判定器，不能当作纯模型生成时间。

## 实机发现并锁定的程序问题

| 问题 | 正式修复位置 | 回归证据 |
| --- | --- | --- |
| 显式只读入口尚在 accepted 就发送模型请求 | `planning_flow.run_planning_readonly_mode` 先进入 writer_running | 真 headless、真日志、脚本 provider |
| 审批取消仍尝试 writer/repair settled | 两个驱动对 approval/stopped 留给外层终态处理 | 真 headless shell 拒绝；repair 的聚焦测试 |
| 网页执行资源错误依赖知识库存在 | `build_research_tools` 构造无知识库的网页资源；知识写入明确不可用 | 真内核 search/open 收据；来源 I/O 在测试侧模拟 |
| 必须修改的要求只有失败检查，没有通过检查 | `project_completion_checks._engine_checks` 独立产生修改检查，再组合验证 | 7 项：先 3 失败/4 通过，后全绿；含真 headless 修改与验证 |
| 文本协议只保留第一个工具示例 | ToolSpec 保留全部不可变示例，快照据此渲染 | 两种快照均显示三种规范 edit 示例，并经过正式解析 |

第一组只读/取消测试曾修正测试作者对 headless 拒绝结果的错误预期：它应为 `stopped`，
不是 `approval`；旧程序的实际错误是非法状态转移。示例测试也先修正作者错误的调用签名，
再确认因“已有文件示例丢失”变红。这些测试编写错误不计为生产 bug。
原生参数、路径、网络/重定向 guard、shell 审批和完成证据要求均保持。

## 中间批次保留，不并入最终结果

| 批次 | 通过 | 修改任务通过 / 产物正确 | 原始目录 |
| --- | --- | --- | --- |
| 修入口/取消/网页资源后，native all × 1 | 5/10 | 0/5、3/5 | `local-model-release-20261002-121301-06b1299667` |
| 修修改检查后，JSON all × 1 | 7/10 | 2/5、2/5 | `local-model-release-20261002-123024-6bf1c319fe` |

中间 native 的 edit/references 存在正确产物，但明确的修改要求漏报，让 done 被拒；
随后又发生截断。JSON 的 edit/references 暴露提示里缺失替换字段示例，模型猜 `old/new`，
校验正确拒绝。没有加这些旧别名的兼容层。

## 规范示例修复后的 JSON 三轮

目录：`.e2e-artifacts/local-model-release-20261002-123431-9aac80287f`。

```powershell
python tools/local_model_release_gate.py --case all --repeat 3 --protocol json --timeout 600 --json
```

| 案例 | 正常通过 | 墙钟秒数（三轮） |
| --- | --- | --- |
| chat | 3/3 | 1.571 / 1.244 / 1.222 |
| read | 3/3 | 5.841 / 5.881 / 6.566 |
| create | 3/3 | 11.570 / 11.720 / 11.485 |
| edit | 3/3 | 10.962 / 10.394 / 11.089 |
| references | 3/3 | 21.101 / 19.197 / 19.157 |
| hybrid | 0/3 | 108.811 / 108.262 / 109.167 |
| discussion | 3/3 | 19.375 / 7.342 / 16.635 |
| planning | 3/3 | 9.378 / 17.319 / 10.531 |
| auto | 3/3 | 4.964 / 4.888 / 5.179 |
| ghost | 3/3 | 0.858 / 0.862 / 0.860 |

总计 27/30；修改/混合任务 12/15 正常完成且独立产物正确。
其中四种纯项目任务合计 12/12，搜索混合任务 0/3。全 gate 仍返回失败，不能据此发布。
累计 573.431 秒，包括存档和独立验证，不是纯模型延迟。
重复三次、温度为零也不能估计任意真实项目的成功概率。
discussion/planning 的通过表示只读与终态契约满足，不表示其建议已被专家认可。

## 搜索失败的独立诊断

三轮混合任务均只收到 `no results`，随后输出被截断。另用正式搜索 factory 查询
`pricing discount context` 和 `Python unittest official documentation`，分别耗时
28.391 / 28.375 秒，包装层返回空列表，`last_connector_errors` 记录浏览器 `TimeoutError`。
诊断在原生纯项目复测期间进行，没有与另一个搜索任务同时执行。
原始诊断：`.e2e-artifacts/local-gate-search-diagnostic-20261002.json`。

这个证据确定了“异常被包装成空成功”，仍未确定浏览器超时的底层原因。
后续回归分别锁住真实无结果、失败无结果、已有部分 connector 结果，以及取消/截止时间。
局部 connector 有真实结果时仍可返回这些结果；两条搜索途径都不可用时不能伪装成成功。

失败传播修复后，沙盒中的单次 JSON hybrid 再测仍失败，但真实工具事件已为
`ok=false / ERROR: search failed: CDP port 9262 did not open within 20s`。
目录：`.e2e-artifacts/local-model-release-20261002-125443-1cd514e982`，109.248 秒。
这个修复让失败报告诚实，没有让搜索环境自动恢复。

沙盒外的正式 factory 诊断在 10.453 秒返回真实 Python 官方文档结果，无搜索错误。
诊断目录：`.e2e-artifacts/local-gate-search-unsandboxed-20261002.json`。
随后在沙盒外保持同一模型、提示、预算和权限跑三次 JSON hybrid：

| 轮次 | 通过 | 产物正确 | 墙钟秒数 |
| --- | --- | --- | --- |
| 1 | 是 | 是 | 26.327 |
| 2 | 是 | 是 | 25.469 |
| 3 | 是 | 是 | 22.574 |

目录：`.e2e-artifacts/local-model-release-20261002-125859-936fc461f3`。
每次都有六步成功的真实工具顺序和单一 run/session。搜索和正文未替换成 fixture。
沙盒外复验支持“此前浏览器启动受测试权限影响”，但不是对底层浏览器失败机制的形式化证明。
环境改变后的三次成功单独报告，不覆盖沙盒三次失败，也不拼成一个 30/30 批次。

## 规范示例修复后的原生项目复测

目录：`.e2e-artifacts/local-model-release-20261002-124435-5bf53f832b`。
本批次在搜索失败传播修复之前进行；该后续修复不改变这些无网页权限的项目路径。

| 案例 | 正常完成 | 独立产物正确 | 墙钟秒数 |
| --- | --- | --- | --- |
| create | 是 | 是 | 76.838 |
| edit | 否 | 是 | 203.745 |
| references | 否 | 是 | 233.324 |
| auto | 是 | 是 | 21.013 |

2/4 正常完成、4/4 产物正确。edit/references 的实际响应都是 `length`，有一次续轮后又截断，
所以报告 `provider_failure`。没有把修改成功替代任务正常完成。
这两例未产生成功的 done；不是修复之后再次漏报修改检查。
原生批次只有一次，不能拿它与 JSON 三轮作为严格的统计 A/B。

## 当前使用建议

这套 Gemma 12B + KoboldCpp 配置先采用文本 JSON 项目路径：四种小项目任务三轮 12/12，
真实浏览器可启动的混合任务另测三轮 3/3。原生协议仍需单独解决结束阶段的生成/模板摩擦，
不能声称其发布门槛已通过。脚本通过 `--protocol json/native` 显式选择，不改用户配置，不静默切换。
本次全部七批合计 76 次案例尝试（含不调用模型的 Ghost），不同版本/环境不合并成总体成功率。

本轮生产代码净增 33 行；新增的大部分代码是独立判定、进程监督和回归测试。
没有复制 Agent 循环或增加旧编辑别名兼容。后续优先扩大真实项目/模型样本，
而不是继续拆文件或因为一个小 fixture 通过就宣称内核无 bug。

## 最终回归

生产与 pytest 修改全部结束之后，在沙盒外启用 `RUN_BROWSER_E2E=1`，独立 TEMP/TMP 下运行：

```powershell
python -m pytest -q -o faulthandler_timeout=120 -rs
```

第一次全量零失败：**6618 passed、6 skipped、1488 subtests passed，424.86 秒**。
六项跳过为 Windows POSIX/O_NOFOLLOW 能力；此前二十二项权限/符号链接跳过本次执行通过。
日志：`.e2e-artifacts/local-gate-py312-full-20261002.log`。
新增八文件四十三项在 Python 3.12.8 与官方 3.13.15 embed 均通过；本轮没有再跑 3.13 全量。
Ruff、compileall 和 diff 检查通过。全量完成后才填写 TEST_REPORT 与中英文 Changelog。

## 已知证据边界

沙盒搜索超时之后的混合任务存在截断或重复调用；底层超时机制仍未证明。
没有把空结果替换成固定网页让门槛变绿。严格 Research 报告、网页模型、其他项目/语言、恶意代码隔离、
跨模型成功率和人工 UX 评分均不由这组测试证明。这些是短英文提示和小型 Python fixture，
不是对中文语音需求、复杂产品开发或全部真实项目的完成率评估。
有限状态枚举和回归测试支持相应不变量，不能证明整个程序没有 bug。
