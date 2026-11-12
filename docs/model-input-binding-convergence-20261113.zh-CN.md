# 上传与网页复制输入绑定收敛

日期：2026-11-13。

## 修改范围

本次只收敛已有上传和网页复制简化输入。固定程序仍经原有 CodeMode、kernel、浏览器执行器和回执提交；没有增加浏览器入口、叶工具直调、执行运行时或持久化副本。

- `TurnSnapshot.frozen_specs` 保存执行合同，新增 `input_binding` 保存简化类型及冻结的模型 `ToolSpec`。上传不再把简化字段混进执行 `codemode` 的参数；复制观察仍由原有 `browser_copy` 字段保存。
- 文本合同、原生工具声明和模型输入验证使用同一份 `model_specs`。构建请求时冻结一次；详情入口和 Research 提交收窄后统一刷新合同，隐藏工具的绑定同步清除。
- `normalize_turn` 统一完成可见性约束、模型合同验证、纯参数转换，再交原有执行合同验证和 ToolPlan。原生 `call_id` 保留；整批有无效输入时没有执行调用。未声明的上传 `code` / `repair_of` 在执行前拒绝。
- `CopyTurn` 内部以不可变 JSON 字符串持有观察；读取和持久化导出独立对象，避免外部修改表格或 JSON 数据。原有列表处理和观察回执引用继续使用。
- 简化入口失败提示展示原请求的模型合同，不要求模型修补看不到的 JavaScript。子回执引用、执行事实及原有上传/下载自动回执恢复保留；父调用失败不表示子动作已撤销。固定模板语法错误明确归属运行时模板。
- 请求快照统一写入版本 6。恢复只接受版本 6，版本 1–5 明确拒绝，需要新请求；没有旧合同重建或旧转换兼容分支。实现标识和外部执行器版本检查继续生效。
- 清理本轮文件原有的隐式 Any：直接访问已知快照属性、显式标注反射元数据和 JSON 容器、使用有类型的正则匹配及测试回调。外部执行器身份提取复用一个函数。缓存仍是原来的 `lru_cache(maxsize=1)`，保留 `cache_clear`。

模型合同是模型输入的事实来源；执行合同是原执行器参数的事实来源。两者用途不同，转换不执行副作用，也不产生第二份子动作事实。

## 回归覆盖与静态检查

类型清理后的相关回归：24 个文件，194 passed（307.95 秒）；另运行 `tests/test_browser_production_execution.py`，13 passed（11.88 秒）。共 207 项通过，没有运行全量 pytest。最后的失败提示修正又运行绑定、上传/复制 Chromium 生产路径、CodeMode 子失败反馈及修复/授权重放的 6 个文件，73 passed（168.48 秒）。

24 个文件覆盖新增绑定测试、上传/复制/详情/Research 提交的 Chromium 生产路径、冻结工具视图、快照绑定与恢复、kernel 快照执行守卫、CodeMode 合同/集成/错误提示/修复重放/授权，以及 durable provider 请求和快照。

新增测试覆盖：上传拒绝隐藏字段；文本/原生协议转换一致及调用身份保留；JSON 排序落盘恢复；旧版拒绝；复制观察隔离；恢复不能改变授权；隐藏工具清除绑定；无效批次无调用；固定模板语法错误使用模型合同。

真实 Chromium 生产路径增加：未声明上传 `code` 无副作用；上传子动作成功但父调用缺少返回时从原回执恢复；复制写入成功但父调用缺少返回时保留子回执、不提示整步重放。现有 stale 文件、失效引用、复制目标限制和 Research 提交测试继续执行。

Ruff 与仓库严格配置的 mypy 检查本轮全部 9 个 Python 文件通过。额外 `--disallow-any-expr` 审计本轮文件诊断为 0；该额外选项同时检查的依赖树仍有 14,505 条诊断，分布于本轮范围外的 450 个文件，因此不声称全仓通过此额外检查。完整原始输出保存在 `.e2e-artifacts/model-input-binding-any-audit-final.txt`。没有新增检查豁免。

## 完整实机矩阵

通过 `python -u -m tools.browser_full_surface_live_gate --run-dir <目录>` 运行全部 11 个场景。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，端点 `http://127.0.0.1:5001/v1`，seed `123456789`，单请求 timeout 180 秒。

首轮：`.e2e-artifacts/model-input-binding-full-surface-20261113-first/`，8/11 通过。首轮结束后进行了上述类型清理，第二轮完整矩阵保留于 `.e2e-artifacts/model-input-binding-full-surface-20261113-final/`，9/11 通过。随后补充失败提示断言，发现原始运行时错误仍夹带 JavaScript 修补指令：原文转存同一回执的 `audit.template_error`，模型只看到模板错误归属、子回执和当前模型合同。最终代码矩阵保留于 `.e2e-artifacts/model-input-binding-full-surface-20261113-verified/`，9/11 通过。三轮各自运行前后的生产源码摘要一致；最终摘要为 `5eea6945658a0d218c5e4e7da2b55eea176beb08440abadbf0b080cd79720540`。各轮独立保存原始请求、响应、回执、拒绝记录和 summary，不覆盖首次结果。

| 场景 | 模式 | 首轮 | 第二轮 | 最终代码 |
| --- | --- | --- | --- | --- |
| basic | Chat | 通过 | 通过 | 通过 |
| structured | Chat | 未通过 | 未通过 | 未通过 |
| legacy_structured | Chat | 通过 | 通过 | 通过 |
| reread | Chat，快照恢复及重复读取 | 通过 | 通过 | 通过 |
| stale_ref | Chat | 未通过 | 通过 | 通过 |
| stale_edit | Hybrid | 通过 | 通过 | 未通过 |
| upload | Chat | 未通过 | 通过 | 通过 |
| download | Chat | 通过 | 通过 | 通过 |
| research | strict Research | 通过 | 未通过 | 通过 |
| source_failure | Chat，预期来源失败 | 通过，blocked | 通过，blocked | 通过，blocked |
| fixed_failure | Chat，预期固定来源失败 | 通过，blocked | 通过，blocked | 通过，blocked |

首次失败的原始证据与归因：

1. `structured/provider.jsonl`、`proposals.jsonl`、`summary.json`：模型的 summary 使用字面 `\\n`，并在解释中重复应由精确字段承担的 URL/token，未满足两段自然语言格式。页面操作和字段采集成功，属于模型输出错误；生产层未改写解释原文。
2. `stale_ref/provider.jsonl`、`receipts.json`：模型找到 Locality 后立即 `break`，按钮引用未赋值即以 `null` 调用点击，并重复该程序。没有实际触发 stale-ref 拒绝，不能算覆盖通过。父回执报告既有 replay control-flow 检查失败；请求本身的无效引用是模型错误，没有为其接受空引用或改变重放检查。
3. `upload/provider.jsonl`、`receipts.json`、`summary.json`：上传选择回执及页面读回成功，但模型两次 `read` 均未传 `wait_for_text`，没有 `browser_wait` 成功回执，完成条件正确拒绝；属于模型遗漏步骤。没有补造等待回执或把上传选中等同于任务完成。

第二轮未通过项：`structured` 先遗漏 eval 获取真实 token，随后补读但仍将来源 ID `s1` 当作知识对象的 `knowledge_refs`，被 `knowledge_reference_invalid` 拒绝；`research` 先直接访问未解包的 `newSnapshot.elements`，之后虽完成页面读取，却重复读取而没有调用当前合同的 `submit_research_answer`，以 no_progress 结束。两项均根据原请求、响应和回执归为模型输出错误。

第二轮 stale-ref 实际产生 1 个 `not_delivered/stale_ref` 拒绝后恢复；reread 完成 9 次快照恢复及 2 次正文读取。通过的场景也可能包含模型修正前的语法、返回结构或引用错误，各场景 `first_tool_rejection`、原始 provider 流及 receipts 均保留，未因最终通过删除。

最终代码的两个未通过项也检查了原请求与回执：`structured` 在补读 token 后仍使用未读取的知识对象引用，完成检查以 `knowledge_reference_invalid` 拒绝；`stale_edit` 模型把按钮引用传给 `browser_fill`，原始 Playwright 回执明确指出目标不可填写，之后反复 snapshot 耗尽轮次。没有执行文件修改，也没有触发本轮预设的文件版本冲突，因此不能声称本轮 Hybrid 冲突场景通过；前两轮该场景均通过且验证过真实冲突和重新读取恢复，相关 Chromium 生产回归也通过。两项归为模型输出错误，没有放宽合同或修补模型回复。

最终 reread 完成 10 次快照恢复及 2 次正文读取；stale-ref 实际发生 2 次拒绝后恢复。strict Research 最终调用 `submit_research_answer` 成功。上传有真实选择、等待及读回回执，下载有受管产物回执。来源失败场景的 HTTP 403 是预设测试条件，按预期 blocked；本次没有用环境原因解释上述模型失败。

## 保留的开发失败与限制

- 重构前定向验证为 3 failed / 6 passed，复现未声明输入、旧版接受、可变观察等问题。
- 开发初版把恢复合同重新渲染后逐字比较，导致合法快照因 JSON 字段排序被误拒绝，首轮相关回归为 32 failed / 14 passed。已删除这一错误检查：保留原请求文本，验证冻结结构、权限和复制观察绑定；新增真实 JSON 排序往返测试。随后浏览器相关回归 33 passed，新增定向测试 4 passed。
- 最后增加的两条错误提示断言分别首次失败，暴露简化失败消息仍含原始 JavaScript 修补指令。失败工件保留于 `R:\codey-pytest\run-mylq967l` 和 `R:\codey-pytest\run-q3urenbp`；将原文保留在审计字段后重新验证，不删除首次失败记录。
- 通用任务与回执上下文继续复用现有提示体系；其中通用回执补读建议尚未全部按当前模型可见工具收窄，复制阶段也没有新增 `read_tool_result` 入口。本次收敛的是简化调用的冻结、验证、转换和失败修复合同，未重写整层通用提示。
- 个别 pytest 运行结束有 RAM 盘临时目录清理的 Windows `WinError 3` 警告，发生在测试结束后，未改动清理设施。
- 本次保留实现标识变化即不能执行原待执行请求的限制；旧版本请求也不能续跑。没有迁移承诺。
- 这是现有简化入口的合同与恢复收敛，不是 12B 能力提升的对照实验；实机模型仍可能写错一般 CodeMode 程序、漏传可选参数或不遵守回答格式。没有修改合同迎合这些错误。
- 未运行全量 pytest，未创建 worktree；提交范围仅本次生产代码、相关测试和本说明。
