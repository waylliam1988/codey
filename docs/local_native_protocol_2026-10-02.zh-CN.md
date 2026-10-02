# KoboldCpp 原生协议失败复现与修复（2026-10-02）

## 结论与范围

此前正常完成 **2/4**、产物正确 **4/4**。两次失败发生在修改和验证已经成功之后：
模型连续输出重复思考标记，未交付内核要求的 `done` 调用。完成门拒绝了这两次任务。

本轮从原始请求复现，分阶段修复并扩展重复次数。最终 `create/edit/references/auto`
各三次，**正常完成 12/12，独立判定产物正确 12/12**。补测六个其他入口也通过。
这证明下面列出的已观察失败已解决；不能推导出所有模型、项目和未来请求都成功。
原始报告与中间失败保留，没有把多版代码合并成一个成功率。

## 1. 实验条件

| 项目 | 实际条件 |
| --- | --- |
| Git 基线 | `6ff8cbd2ce3dd1c6f6c32a8a8d0a2804e2f240cb`，实验代码为工作区修改；逐批记录模块哈希 |
| 服务端 | KoboldCpp 1.117.1，`http://127.0.0.1:5001/v1`，版本接口报告 `jinja=false` |
| 模型 ID | `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` |
| 客户端 | Python 3.12.8，temperature 0，未发送 seed |
| 上下文预算 | window 32768 / reserve 8192 / keep recent 12000 |
| 任务截止时间 | 每次 600 秒，包含模型、工具和独立验证 |
| 活动轮次输出预算 | 服务端默认，未设置 `max_tokens`；原失败实际 usage 为 1536，配置接口的 1024 不代表实际硬上限 |
| 终止收据输出预算 | 显式撤回工具，`max_tokens=1`；已经确定的用户回答不来自这个确认回复 |

没有修改用户保存的配置、服务端设置、完成标准或原 fixture 测试。
原始请求/响应、项目、隔离 state 和失败日志留在忽略目录 `.e2e-artifacts/`，不提交。
最终实机批次记录的全部生产模块哈希在全量回归前复核一致。
后续审批/UI 修复没有改变这些原生项目案例所走的模块。

## 2. 根因与实际对照

### 2.1 活动工具轮次允许了普通回答分支

Codey 的内核把 `done` 作为完成提议，因此活动轮次必须返回一个调用。
旧 provider 使用 `tool_choice=auto`。在当前服务端配置下，测试通过后可以选择普通回答分支，
然后这台模型出现 `<|channel>thought` 与 `<channel|>` 标记循环，耗尽生成预算。
一次 length continuation 后仍没有合法 `done`，最终返回 `provider_failure`。

官方同版本实现的 `determine_tool_json_to_use` / `transform_genparams` 显示，非 Jinja
路径先选择工具，再生成调用参数；`auto` 和 `required` 的选择规则不同。
这与观察到的分支差异一致。[KoboldCpp 1.117.1 官方源码](https://github.com/LostRuins/koboldcpp/blob/v1.117.1/koboldcpp.py)。

重放原始 `edit` 的测试通过后请求，**线上请求只改变 `tool_choice`**：

| 请求 | 观察结果 | 请求耗时 |
| --- | --- | ---: |
| 原始 `auto` | 再现 383 对思考标记，1536 completion tokens，`length`，零调用 | 83.476 秒 |
| `required` | 真正的 native `done` | 7.457 秒 |
| 原始 `references` 历史改 `required` | 真正的 native `done` | 10.146 秒 |

这是对协议分支的实机因果对照；没有证明模型内部为什么重复这些 token。
修复在 `LocalOpenAIProvider._request_payload`：有可用工具的活动轮次使用 `required`，
普通聊天保持原方式。记录器调用同一函数，防止诊断日志与真实请求不一致。

### 2.2 当前服务端忽略单调用字段，混合批次可能错配参数

扩大到十二次后，发现同一批次的 `run` 携带 `edit` 的 `path/content` 参数。
内核正确拒绝整个非法计划，模型却有时误以为编辑已执行，继续跑旧文件的测试。
服务端该路径的参数 grammar 绑定所选工具，而调用数组中的函数名未被同样绑定。

重放一次失败前的 `create` 请求：

- 只设置 `parallel_tool_calls=false`：20.348 秒，仍返回四个调用并有参数错配。
- 同样字段，加上“每轮只调用一个工具、等待结果、检查通过后单独 done”：6.389 秒，一个合法 edit。

因此字段本身不能被当成服务端已经强制执行的保证。生产请求声明 `parallel_tool_calls=false`，
共同 native 提示也写明逐步调用；共同解析器仍校验所有返回调用，支持其既有批次协议。
没有放宽参数校验、猜测参数别名或静默切换 JSON。

### 2.3 歧义编辑提示没有解释替换对象的独立性

两个调用方有相同 return 行。模型先提交裸 return 的替换，被唯一匹配要求拒绝；
之后把函数头作为一个独立的无变化 replacement，误认为它能限定另一个 return replacement。
编辑器保持原子拒绝，模型重复尝试，直到 `no_progress`。

共享工具定义和歧义错误现在明确要求：上下文与目标文本放进**同一个** `old_string`，
在 `new_string` 中保留上下文；不同 replacement 不会互相限定范围。
增加完整的多行规范示例，两种协议共用。
只替换原失败历史中的这一条错误指导，实机 13.459 秒返回了完整、唯一锚定的函数块。
LF/CRLF 红测同时锁住拒绝时文件字节不变，以及正确上下文只修改指定函数。

## 3. 收据和回归中发现的缺口

- 完成、取消、轮数耗尽和无进展终止时撤回工具，避免 `required` 在任务结束后继续催生调用。
  已接受 `done` 的回答和证明保留；最后请求仅确认已交付的收据，限制为一个 token。
  此处 `finish_reason=length` 是预期的确认预算，**活动轮次**的截断规则未改变。
  HTTP 失败、非法响应和无法关闭的调用仍会显式失败。
- 最后一轮拒绝 done 或修复协议时，provider 可能返回新调用。旧预算终止函数只交付 pending
  results，漏掉 pending reply。新增真实 kernel 测试先复现悬挂 id，再让 `_finish_after_budget`
  关闭它；关闭阶段不执行该调用。保留原来有界的四轮排空限制。
- 首轮全量发现真实 Edge 的审批续跑竞态：页面显示 Denied 时续跑未结束，下一次请求撞上 busy。
  HTTP 审批成功后调用已有状态同步入口；E2E 按真实终态等待续跑，不再把 shell 决策当成任务结束。
  同时修复拒绝命令的提示误称“已批准并执行”，明确拒绝且未执行，禁止未经新授权重试。
  批准路径保留原文本。

这些改动保留一个工具循环、一个完成门、严格授权和唯一匹配编辑，没有新增生产兼容层。

## 4. 各批次结果与独立审计

| 代码阶段 | 批次目录尾部 | 正常完成 | 产物正确 | 案例耗时合计 |
| --- | --- | ---: | ---: | ---: |
| required + 终止撤回工具 | `132604-8c6938bbfb` | 10/12 | 10/12 | 897.428 秒 |
| 加单调用指导、确认预算与最后一轮收据 | `134349-4aefe7df22` | 11/12 | 11/12 | 477.752 秒 |
| 加完整上下文编辑指导 | `135612-140be5af6b` | **12/12** | **12/12** | 444.740 秒 |
| 其他六个入口 | `140516-6d420577f3` | 6/6 入口判定 | 1/1 修改任务 | 120.756 秒 |

完整目录前缀为 `.e2e-artifacts/local-model-release-20261002-`。
第一阶段第二次 create 和 references 失败；第二阶段第二次 references 仍有歧义编辑失败。
失败材料全部保留。这些包含多项代码变化的批次不能当作严格的速度 A/B。

最终十二次：

| 案例 | 三次完成 | 三次耗时（秒） |
| --- | --- | --- |
| create | 3/3 | 36.265 / 35.860 / 35.979 |
| edit | 3/3 | 35.312 / 35.401 / 35.316 |
| references | 3/3 | 62.346 / 62.788 / 61.772 |
| auto | 3/3 | 14.093 / 15.626 / 13.982 |

独立输入矩阵、原测试哈希、实际测试执行、生成测试对错误实现的敏感性检查保持。
审计十二次项目请求：57 个 call id 各一次收据，零活动轮次截断，零多调用响应。
最后确认实际使用一个 token，请求耗时 0.875–2.050 秒。

补测 chat/read/hybrid/discussion/planning/ghost 各一次：

- hybrid 真正同 run 执行 `web_search→open_url→read_file→edit→run→done`，71.048 秒。
- read 有成功读取且文件不变；discussion/planning 只证明只读安全和协议结束，未评估回答质量。
- chat 精确返回标记；Ghost 为隔离的写/读/检索/删除 roundtrip，不调用模型，不计入模型任务成功率。
- 这些 provider 轨迹共十个调用 id，各一次收据，零活动轮次截断。

本轮四批共有四十二次尝试，其中一次 Ghost；另有六次诊断请求。
不把它们合并为总体模型成功率。真实运行未经改动的多项目、大项目和其他模型仍待评估。

## 5. TDD 和最终回归

新增三文件、二十二项行为回归：

1. `test_native_tool_choice_matches_task_lifecycle.py`：十三项，真实 HTTP 边界与共同 kernel，
   覆盖请求选择、记录同源、单调用提示、终止撤回、取消/预算/最后一轮全部 id 的交付。
   首批七项为六红一绿；后续新行为逐项确认红测再修。保留已经正确的普通聊天行为。
2. `test_ambiguous_edit_guides_context_in_one_replacement.py`：六项先红后绿，
   覆盖字节原子性与两种快照中的可执行规范例子。
3. `test_shell_continuation_reports_denial_and_reconciles_ui.py`：三项先红后绿，
   真实拒绝提示及执行真实 `approveCommand` 的 JavaScript 测试。

首轮全量 **3 failed、6634 passed、6 skipped、1488 subtests passed，417.20 秒**：
两个已审阅的编辑说明 golden fixture 过期，一个上述真实 Edge 竞态。
fixture 只更新相关说明与示例，断言保留；UI 缺口补红测后修复，不以跳过掩盖。

最后冻结代码，全量启用真实 Edge，仓库外独立 TEMP/TMP：

```powershell
$env:RUN_BROWSER_E2E = '1'
python -m pytest -q -o faulthandler_timeout=120 -rs
```

**6640 passed、6 skipped、1488 subtests passed，464.01 秒，零失败。**
六项跳过均为 Windows POSIX/O_NOFOLLOW 限制。最后结果结束后才更新本文与报告。
首轮/最终日志分别为 `native-root-cause/full-pytest-20261002.log` 和
`native-root-cause/full-pytest-final-20261002.log`，位于 `.e2e-artifacts/`。

相关四十四文件回归曾有 1173 passed、4 skipped、458 subtests；审批/快照相关回归
359 passed、1 skipped、61 subtests，真实 Edge 单独 2 passed。这些重叠数字不相加。
最终 Python 3.13.15 通过七文件八十三项针对性回归（含全部二十二项新测），4.27 秒；
本轮未重跑该版本全量。Ruff、compileall、diff 检查通过。

## 6. 复跑与停止条件

```powershell
python -m pytest tests/test_native_tool_choice_matches_task_lifecycle.py tests/test_ambiguous_edit_guides_context_in_one_replacement.py tests/test_shell_continuation_reports_denial_and_reconciles_ui.py -q
python tools/local_model_release_gate.py --cases create,edit,references,auto --repeat 3 --timeout 600 --protocol native --json
python tools/local_model_release_gate.py --cases chat,read,hybrid,discussion,planning,ghost --repeat 1 --timeout 600 --protocol native --json
```

这些门槛通过后停止本轮修复，不增加猜测性 fallback 或无限重试。
有限测试可锁住指定反例和不变量；不等同于普遍无 bug 的数学证明，也不证明原生路径比 JSON 更快。
未发布、未打 tag、未 bump 版本；按要求推送后不等待托管 CI。
