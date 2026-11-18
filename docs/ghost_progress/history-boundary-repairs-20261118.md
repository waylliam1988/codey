# 历史编码与区段所有权补修（2026-11-18）

本批修复历史投递复核中的两处确定性边界，不改变此前模型实验的收益、负结果或学习状态。
不新增模型调用、数据库、运行时或双写。沿用原存储、原 source snapshot validator、admission 和实际发送入口。

## 问题与生产行为

1. 完整消息经过压缩入口 JSON 序列化后，换行等字符被转义，旧的普通消息扫描误报
   `represented_history_missing`。现在仅在 `purpose=compaction` 时解析完整 `<source>` 消息列表；
   原文以及现有 `summary_source` 产生的重复行／模板编码可以证明完整表示。
   编码比较复用原编码器和父 provider 中保留的原始文本，不另造解码合同或展开任意重复次数。
   canonical 用户输入仍要求完整 user content；助手、摘要、丢失条件、错误内容结构和不完整 JSON 不算完整输入。
   普通 conversation 请求不获得这项编码识别能力，压缩后丢失原始表示仍须拒绝并重新组装。
2. 去重原先全局 `replace(source.text, ...)`，会改写项目指令／研究材料中的同文引用。
   现在只编辑组装器登记的 `GhostContextSpan(key,start,end)`，复核原范围、原字节、唯一来源和非重叠区间，
   再按原坐标重组。项目 renderer 和附加目标登记位置，共享入口追加来源也登记位置。
   正常 task entry 的已知格式是「按顺序排列的 Ghost 区段 → workspace → follow-up」；
   durable 身份／scope 核对后，仅完整匹配这组前缀才登记其位置，不搜索任意出现位置。
   项目路径有独立 validator，不走这条前缀登记路径。
   其他未登记布局保留原文和原来源，不猜位置、不宣称已去重，最终预算和来源 admission 仍有效。

选择后只对实际省略的历史 refs 建立发送依赖；其他来源仍投递同一 ref 时不制造多余依赖。
来源被撤回、删除、关闭或改变时，即使去重后零条，现有 snapshot validator 仍先于生成执行。

修复直接用于现有默认入口，没有新增开关、模型／API／输出预算白名单或额外生成。
用户仍可关闭 retrieve_projections/admit_hints；不修改历史记录格式或已确认状态。

## 回归与失败证据

原始日志留本地 `D:/codey-local-evidence/ghost-boundary-repairs-20261118/`，不提交完整请求／回执。

- `first-reproduction.log`：首次 **3 failed、6 passed**，分别复现完整 JSON 历史、完整 user 输入被误拒绝，
  以及同文引用被全局改写。没有覆盖首次失败。
- `initial-fix.log`：初步修复 **24 passed**。
- `expanded-initial.log` / `expanded-rerun.log`：实际压缩入口测试夹具先拼出了不合法历史，之后 Responses
  合成回执又缺少 `status=completed`。检查捕获的真实请求和回执后修正夹具，未放宽生产验证。
- `expanded-final.log`：**44 passed**。
- `final-boundary-cases.log`：补上模板编码、混合 content 拒绝、相同文本不同来源等后 **49 passed**。
  实际 `summarize_context → auxiliary provider → attempt gate → generation` 覆盖两种 API 协议；
  generation 使用确定性合成回执，未调用实机模型。撤回后不得发生第二次 generation。
- `production-regressions.log`：较宽检查 **32 failed、393 passed、2 subtests passed**，失败没有抹去。
  其中 26 项为旧发送测试夹具：把手写请求偏好当成本地验证来源，或者仍假定禁用时只读一次控制。
  现改为独立的合成来源 admission 矩阵，以及真实普通入口的控制检查；保持原来的拒绝原因、零 HTTP、
  来源 digest／scope／epoch 和正常发送断言。测试使用现有 durable setup 与生产 kernel，不重建运行时。
  初版修复对无布局入口保留原文，导致 4 项 Chat 去重回归；已通过正常入口固定布局登记恢复。
  `entry-regressions-rerun.log`：相关两文件 **57 passed**。
- 宽检查另有两项未修改的研究测试依赖旧接口，列在下一节，不能把宽检查写成全通过。
- `production-regressions-first.log` 是一次命令路径拼错，未收集任何测试，不计入生产失败或通过。

最终主回归 **376 passed、2 subtests passed**，见 `production-regressions-final.log`。
主回归收集后追加的五个边界参数／案例另在 **49 passed** 的边界组中验证；49 与主回归有重复，不相加。
没有运行全量 pytest，没有新增实机效果实验。

复现命令：

```powershell
python -m pytest tests/test_ghost_boundary_repairs.py tests/test_ghost_source_selection.py tests/test_ghost_context_admission.py tests/test_ghost_provider_send_boundary.py tests/test_ghost_legacy_projection_admission.py tests/test_ghost_portable_context.py tests/test_project_experience_production.py tests/test_project_goal_production.py tests/test_ghost_history_delete_guards.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_experience_role_budget.py tests/test_ghost_observation_consumption_view.py tests/test_compaction_source_encoding_and_summary_contract.py tests/test_compaction_source_reduction_and_packing.py tests/test_context_compaction_transactions_and_lifecycle.py tests/test_zen_compaction_auxiliary_request_boundary.py tests/test_provider_durable_compaction_binding.py tests/test_compaction_request_token_accounting.py tests/test_responses_compaction_preserves_complete_exchanges.py tests/test_verification_candidate_prompt.py tests/test_context_source.py tests/test_agent_native_loop.py tests/test_agent_completion_repair_context.py -q
python -m pytest tests/test_ghost_boundary_repairs.py tests/test_ghost_source_selection.py -q
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_boundary_repairs.py tests/test_ghost_source_selection.py tests/test_ghost_provider_send_boundary.py --python-version 3.11
pyrefly check tests/test_ghost_boundary_repairs.py tests/test_ghost_source_selection.py tests/test_ghost_provider_send_boundary.py --python-version 3.11
ty check tests/test_ghost_boundary_repairs.py tests/test_ghost_source_selection.py tests/test_ghost_provider_send_boundary.py --python-version 3.11
```

五个全树静态门全部通过：mypy 覆盖 709 个生产文件；Pyrefly 为 0 errors，既有
35 suppressed／160 warnings 单列，没有新增豁免。三个改动测试文件的针对性三检查均通过。
7 个生产文件与 3 个测试文件另做 `--disallow-any-expr --disallow-any-explicit
--disallow-any-unimported --disallow-subclassing-any` 的 owned-file mypy 检查，0 诊断；
这项附加检查用 `--follow-imports=silent` 限定自有文件，不能冒充全依赖树的额外精度审计。
普通生产树和针对性测试 mypy 没有使用该参数。未使用 cast、type ignore 或修改检查配置。
早期类型／Ruff 诊断及最终重验日志分别保留。

## 独立审查：旧研究测试

`tests/test_task_entry_cutover.py` 中两项与本批编码／区段修改没有依赖的夹具仍失败：

| 测试 | 当前失败与代码核对 |
|---|---|
| `test_task_entry_research_builds_default_search_provider` | 传入 `SimpleNamespace(root=...)` 冒充知识库，并以 `object()` 冒充 search provider；现行 `build_research_tools` 要求真实 `KnowledgeStore` 及 search 协议，先拒绝 `invalid knowledge store`。 |
| `test_open_hit_uses_source_locator_in_shared_research_kernel` | 自建 `Tools` 不满足当前研究执行器接口，返回 `unknown tool executor: source_search`；且模拟搜索只返回文本，未提供现行来源观察回执。 |

两个测试及其生产 owner `codey/operations/task_execution.py` 均无本轮修改。
它们需要单独迁移到有效研究资源／回执夹具，保留搜索工厂选择和 source locator 的行为验证。
本批不为这两处过时夹具放宽生产资源合同，也不将整份旧研究测试的类型迁移混入 Ghost 修复。

## 限制与回滚

不完整的压缩分片、未知编码、摘要不被当作完整历史；没有扩大到压缩调度或自动重组运行时。
历史被真正压缩掉后，普通发送的严格检查保持不变。这个补修不证明模型事实准确率提高。
项目历史仍可能包含明确标为助手输出的旧错误，原实验的限制继续有效。

开发回滚本批修复无需数据迁移；可回退代码提交。用户关闭现有 Ghost 控制仍有效。
方向／进度索引同步本记录，开放学习和 World Model 的未验证状态不变。
