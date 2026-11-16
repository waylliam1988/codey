# 合同与回执驱动的浏览器、文件续跑

基线 `fe04b4d1`，直接修改原工作区，没有创建或复制 worktree。只调整本轮机械续跑、引用串接及完成判定，沿用原 ToolSpec、TaskPolicy、CodeMode、host、kernel、版本检查与 durable receipts。

## 修改内容

1. Hybrid 交接根据临时 `WorkStatus` 选择当前方向。它复用现有工具观察、项目检查、来源检查、指定输出读取检查和文件字段验证；文件字段的当前内容验证由交接和完成门共同调用。浏览器读取完成但文件或必要验证未完成时，不再邀请提交完成答案。自然语言答案、引用及输出格式仍在原提交边界检查，没有另存完成状态。
2. 完整声明、来源完备且获准写入的字段复制，通过现有 `ModelInputBinding` 提供仅含目标 `path` 的 CodeMode 输入。应用生成固定 read_file → edit 程序，使用原始外层 `base.result_ref`；字段值仍由现有 `file_field_binding` 从原来源回执提取。读取与编辑分别结算正式回执。普通全文修改继续使用原编辑合同。
3. Chat、Hybrid、strict Research 共用当前文档及点击阶段的等待、读取步骤推导。`purpose=action` 不会取消已声明的等待、读取义务。来源证据齐全后，Research 仍使用原提交入口及实际证据标签。多页面复合点击不再把不同页面的等待全部挂到同一次点击上；共享推导只选择当前文档的声明。
4. 明确的文件版本拒绝后，固定入口重新完整读取再编辑；连续三次冲突后停止主动提供该入口。模型仍选择是否调用及目标路径，程序负责代码、值与引用。写入结果不确定时先要求核实结算，不能盲目重写。恢复工具及原授权检查继续有效。

## 同轮确定性问题与修复

- 初版共享续跑在首次导航后就提示等待，抢占了观察、编译错误恢复及错误 URL 恢复。`production-03.log` 的四项失败与首轮实机原始请求均证实这一点；修正为点击、明确页面序列或获准 Research 工作流开始后才进入对应续跑。四项原失败用例在 `preclick-04.log` 独立通过。
- 固定文件阶段最初仍携带浏览器导航绑定，导致序列化恢复与当前模型接口冲突；进入文件阶段时清除原浏览器绑定，恢复仍校验冻结政策及允许的目标路径。
- Research 旧工作流入口把同一种工具在不同明确阶段的使用当作重复声明。本轮按声明阶段识别等待、读取，汇集各阶段正式证据；提交检查仍要求所有声明成立，并引用最后一阶段有效正文。`research-stages-01.log` 保留首次失败，`research-stages-02.log` 记录修复后的通过。
- Research 序列完成检查与续跑对页面证据失效的判断不一致。统一复用原失效操作集合，操作使证据失效后先重新等待，再读正文。首次六项失败在 `invalidation-01.log`，后续定向回归通过。
- 续跑最初把生成调用补入的默认超时也当作冻结要求。改为按原声明识别已结算等待，只有合同明确声明的超时才参与该项匹配。`wait-predicate-01.log` 保留两项失败及显式超时正例；最终回归同时验证默认值与明确要求的边界。
- 一个既有说明输出夹具仍向 `paragraphs` 合同提交 `summary`，改为提交原合同字段。错误类型编辑测试改为先保留真实拒绝，再使用新的路径入口恢复，没有放宽生产合同或删除负例。

`tools/mechanical_surface.py` 只作明确类型与 JSON 解析入口整理，用于检查本轮测试的依赖；没有启用该实验的替代执行或模型回复改写。

## 泛化及类型边界

新增定向回归覆盖变换后的 URL、路径、字段、回执、JSON number 小数/高精度/科学计数法、integer 和带前导零的 string；还覆盖真实 CodeMode host/kernel 的读写与版本冲突、冻结快照恢复、非法目标、旧式错误 JS 拒绝、无写权限、不完整/旧文档来源、普通代码文件、来源/指定输出前置条件、不同页面和阶段、部分等待声明、自由探索、不确定点击/写入及冲突预算。

完整类型门槛的目标版本为 Python 3.11，测试执行解释器为 Windows Python 3.12.8。最终全树 Ruff 通过；mypy 检查 `codey` 及三个修改测试和辅助脚本，共 699 个源文件无问题；相同范围的 Pyrefly 为 0 errors，ty 通过。Pyrefly 的 35 suppressed、157 warnings 是既有配置状态，本轮未增加豁免。

全部 16 个修改 Python 文件另以 mypy 的 `--disallow-any-expr --disallow-any-explicit --disallow-untyped-defs --follow-imports=silent` 检查通过。严格检查最初发现的隐式类型均修正：去重字典明确保存 None、测试请求使用 JsonObject、辅助脚本复用正式 JSON 解析器。没有加入 Any、cast、type: ignore 或关闭检查。`kernel_type_precision_gate` 也通过。

原始日志、实机请求、回复与回执保留于 `.e2e-artifacts/declared-work-20261116/`。全树检查日志分别为 `ruff-final.log`、`mypy-final.log`、`pyrefly-final.log`、`ty-final.log`；严格修改文件检查为 `types-strict-02.log`，初次失败保留于 `types-strict-01.log`。

## 生产回归与额外实验检查

没有运行全量 pytest。扩展批次明确选择 23 个相关文件，包括交接、Research 提交、CodeMode JSON/恢复/协议、文件版本、完成门、来源与指定输出前置条件、上传和下载。准确文件列表及证据 SHA256 保存于同名 JSON 审计记录。

| 批次 | 结果 | 说明 |
| --- | --- | --- |
| reproduction-01 | 2 failed | 修改前复现提前 done 提示及缺少结构化复制入口 |
| production-01 | 3 failed / 181 passed | 导航绑定遗留、旧说明夹具字段、恢复反馈缺少已送达事实；均检查并修正 |
| production-03 | 4 failed / 413 passed，8 subtests passed | 初次导航后过早等待，随后四项独立复跑通过 |
| production-04 | 448 passed，8 subtests passed | 阶段补充前扩展回归 |
| boundaries-05 | 40 passed | 含阶段、失效恢复、声明超时边界的完整新增回归 |
| production-05 | 461 passed，8 subtests passed | 完整功能修改后的扩展生产批次；之后仅清理隐式类型 |
| production-final-02 | 167 passed | 类型清理后，新增续跑、浏览器恢复与 Research 提交三个生产回归文件的完整复跑 |

`production-04`、`production-05`、`production-final-02` 退出码均为 0，但退出清理 RAM 临时目录时有文件已不存在的 WinError 3 警告；没有删除这些原始日志。早期数值测试的临时目录及回执索引夹具错误、无效测试路径命令，也分别保留原日志，没有当作生产失败或成功采样。

最后额外加入 `test_mechanical_surface.py` 后，四文件批次为 **184 passed / 5 failed**（`production-final-focused.log`）。五项失败属于现有实验探针：三项上传夹具仍向固定上传输入提交自由 JS，两项旧运行包装函数没有接收当前执行器的 `delivery` 参数。原始请求和回执已从 RAM 目录保存到 `mechanical-focused-first/`。

在独立检查进程中，仅将 `tools.mechanical_surface` 加载为 `HEAD` 原文，再执行其同一完整测试文件，得到 **17 passed / 同样 5 failed**（`mechanical-baseline-01.log`）；原始回执另存 `mechanical-baseline-01/`。基线内核已经传递 delivery，基线上传入口也已经要求结构化输入。这些结果不由本轮类型整理引入；没有更改该旧实验路线、夹具或生产合同来制造通过。该辅助脚本相关检查没有全绿，单独列为限制。

## 完整 full-surface 实机

使用原 `tools.browser_full_surface_live_gate --scenario all`，本地 KoboldCpp 模型为 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，真实 Chromium 与受控页面/附件。没有改写模型原始回复或矩阵通过条件。

| 完整运行 | 结果 | 版本范围 |
| --- | --- | --- |
| full-surface-01 | 13/17，退出码 1 | 调试中运行，源码前后不同；完整保留，不作为最终版本验证 |
| full-surface-02 | 14/17，退出码 1 | 阶段与类型收尾期间运行，源码前后不同；独立保留 |
| full-surface-03 | **15/17，退出码 1** | 最终稳定源码，前后摘要相同 |

最终生产源码 SHA256：`40d8bcfcf46820d521debc3b0292d7a600f38fe06ec73408f30f630874e56619`。摘要包括新增未跟踪生产文件，暂存不会遗漏它们。三个批次不合并为通过率，也不构成同版本、同任务的统计 A/B 结论。

| 最终场景 | 结果与实际覆盖 |
| --- | --- |
| basic、codemode、structured、legacy_structured | 均通过；Chat、CodeMode、当前说明合同与旧结构化事实输出 |
| reread | 通过；恢复 6 个冻结快照，读取来源 2 次 |
| hybrid、hybrid_codemode | 均通过；各读取来源 1 次，文件与回答事实正确，正式 read_file/edit 各 1 次 |
| upload、download | 均通过；保留正式附件选择/正文与下载回执 |
| research、research_sequence、research_codemode | 均通过；strict Research 使用实际证据引用提交，plain Research 另实际恢复一次 stale-ref 拒绝 |
| source_failure、fixed_failure | 均按原合同 blocked，属于预期通过 |
| stale_ref_injected | 通过；实际注入 1 次 stale-ref 拒绝，之后恢复并完成读取 |
| stale_ref | 未通过；模型用最新控件引用完成业务事实读取，跳过用户明确要求的旧 ref 负例，failed_stale_refs=0 |
| stale_edit | 未通过，no_progress；三次重复读取导航 receipt，没有 read_file/edit，版本冲突注入未触达 |

两项最终失败均逐条核对 `task.txt`、`request.json`、`provider.jsonl`、`receipts.json`。stale_edit 的浏览器正文已完整交付，下一请求明确要求全文件 content 编辑及外层读取引用；模型仍反复读取导航 receipt 的 value。这项任务明确要求 content，因此没有被悄悄改成 declared_fields。实际文件版本冲突与新读恢复仍由原 `test_browser_full_surface_contract.py` 的受控 Chromium 生产回归及新增固定程序的真实 host/kernel 回归验证；不能宣称模型实机已经覆盖该分支。

首轮失败另含已修复的提前等待，以及一次 180.015 秒后 `GenerationUnknownError: generation cancelled while opening request` 的 provider 失败。Hybrid CodeMode 首轮反复把外层 snapshot 当数据而被拒绝；第二轮实际文件正确，但模型最终文字编造为 125.5/A7B2，仍判失败。最终通过不会覆盖这些失败记录，也没有发现必须放宽生产合同才能解释的结果。

## 减负的证据与剩余限制

最终两种 Hybrid 的原始回复均只选择 `codemode({path:"result.json"})`；应用串接的读取与编辑回执分别是：

- Hybrid：read_file `task_turn_effect:dbad6a9d9adbd336` → edit `task_turn_effect:115bf0c5e63b8b5d`。
- Hybrid CodeMode：read_file `task_turn_effect:dfd57e50cb98294b` → edit `task_turn_effect:f3bb82b19cff6394`。

两个 edit 的 base_result_ref 均等于各自原始 read_file 回执，字段值来自原来源，模型无需编写 JS 或传递引用。该收益是固定入口承担机械工作的直接证据；不会保证模型到达该入口、服从阶段提示或正确写出最终自然语言。普通全文重写、未完整声明、来源不完整及不确定写入仍保留各自原边界。

Research 的自动入口继续限于原支持的明确页面序列或单一命名来源工作流；多阶段命名流程使用原 stage 声明，多个独立页面由共享推导按各自文档过滤，不新增多页面提交合同。自由探索仍由模型选择路线。

本次只验证本机模型和受控页面，不声称外部站点、多模型或跨平台效果。没有复制 worktree，没有引入第二套运行时、双写或新的持久完成标记。提交范围为本轮 16 个 Python 文件及报告/审计记录；工作区原有的 `dist-sdist-check/codey-0.5.11.tar.gz` 删除不纳入提交。
