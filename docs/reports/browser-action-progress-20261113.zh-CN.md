# 浏览器进展比较与冻结动作待办

基线：`0992ac85`。在原工作区修改，没有创建或复制 worktree，没有运行全量 pytest。

## 修改范围

进展检查增加临时回执投影，信息去重和重复调用指纹使用同一份比较内容。原始回执仍负责执行、审计、引用及完成核验，没有增加执行器或完成状态。

- 快照仅去除协议位置的 ref、generation 等观察标识，保留正文、URL、控件属性及截断状态。填入成功后的展示文本不会把 ref 再带回比较。
- 点击保留正式页面观察、投递及核验状态；失败保留原因。eval 的业务对象原样比较，下载路径、句柄和哈希保留。
- browser_read 比较正式回执中实际展示的段落窗口，避免来源时间、引用变化制造进展，也避免把未展示的正文变化计为新信息。
- 结构化父回执按 child_effect_ids 顺序投影正式子回执，保留父级状态及失败前已执行的操作。内部选控件快照不增加模型尝试次数。通用 CodeMode 的程序返回值沿用原语义。
- 显式 ref 仅在完整正式快照能唯一识别控件时归并；同名或截断列表保持保守。浏览器同一调用的 `A → B → A` 可见变化仍能推进。

可见信息重复与停止分别判断。浏览器效果操作可以增加无新信息计数并提示观察及处理剩余要求，不能仅凭相同页面正文提前停止。连续只读重复仍受现有重复和停滞预算约束，总轮次上限保留。补充修复了停滞预算大于 16 条历史容量时可能失效的问题。

实机原始记录还暴露了周期提醒缺口：稳定指纹已识别 `click → read → click → read`，但原提醒只检查相邻两次相同调用。新增直接调用和结构化调用的回归在修复前均失败；修复后沿用原周期检测发提醒，仍不根据相同观察提前停止效果操作，并用生产入口测试确认提醒进入下一模型请求。

待办从下一请求的冻结动作分支、叶子规范及原完成规则生成，覆盖初始指导、每轮工作状态和 done 拒绝后的反馈。完整候选通过原参数校验器和固定降级程序校验；缺参数、动作未公布、URL 不兼容或底层 edit 参数无法由 edit_json 保证时，明确说明原因。相容的 eval 建议调用可以合并，各条完成规则仍独立凭回执通过。

清除了结构化初始指导及版本冲突反馈中的旧 JavaScript 编辑示例。只有确认写入前版本冲突时才建议重交相同 edit_json；已写入或投递不确定时要求先观察。原始冲突详情仍留在审计记录。

结构化实机脚本通过同一份 WAIT_ARGS 同时生成任务文字及已有 require_tool_success 参数声明；生产代码没有填入验收夹具的等待文字或超时。

## 验证记录

完整日志及原始请求、回执保存在 `.e2e-artifacts/browser-actions-progress-20261113/`。首次失败与重跑使用独立记录。

早期定向回归曾有 55 项通过、3 项冷恢复失败，错误是 captured implementation incompatible：测试运行期间修改生产源码，使进程间缓存的实现指纹不一致。随后恢复测试在源码冻结后运行。新增回归的早期失败另包括断言误用校验器成功返回值、测试回执缺少正式必填字段及恢复夹具未领取新任务执行轮次；均修正夹具，没有放宽生产合同。

相关生产回归按改动阶段运行，没有运行全量 pytest：

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 第一阶段相关回归 | 306 通过，696.14 秒 | `regression-first.txt` |
| 停滞预算修正后定向回归 | 96 通过，87.50 秒 | `regression-after-budget-fix.txt` |
| 周期提醒回归，修复前 | 2 项预期失败，确认缺口 | `alternating-cycle-regression-before.txt` |
| 最终源码相关回归 | 120 通过，312.86 秒 | `regression-final-cycle.txt` |
| Ruff：全部 codey、三个新增测试及修改工具 | 通过 | `ruff-after-cycle.txt` |
| mypy：`python -m mypy codey --python-version 3.11` | 683 个文件通过 | `mypy-after-cycle.txt` |
| `tools.kernel_type_precision_gate` | Any 计数为零，逃逸检查通过 | `type-precision-after-cycle.txt` |

最终 120 项覆盖三个新增测试文件、structured_actions 单元与生产入口、只读停滞、完成拒绝进展、当前验证修复预算和 full-surface 合同。包含真实 Chromium 点击投递后正文观察失败，以及生产 kernel + SQLite 进程退出、同批重投不重复执行/计数、下一请求继续使用冻结接口的检查。

pytest 成功退出后曾报告 RAM 盘清理 warning（Chromium 文件占用或路径已消失），日志保留。没有为清理 warning 修改生产行为或删除其他进程目录。

实机使用本地 KoboldCpp `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`、seed `123456789`、真实 Chromium 和生产入口。受控网页夹具用于覆盖动作及回执，不代表任意公网网站的端到端保证。

| 周期提醒修正前的实机运行 | 结果 |
| --- | --- |
| `full-surface-first`，12 场景 | 11/12；basic 失败 |
| `full-surface-basic-rerun` | basic 通过，首次失败未覆盖 |
| `structured-first`，Chat / Hybrid | 1/2；Chat 通过，Hybrid 达到轮次上限 |
| `structured-rerun`，Chat / Hybrid | 0/2；两者均达到轮次上限 |

basic 原始响应连续重复无法编译的 JavaScript，末尾错误的回车转义使 `return` 留下 `eturn`。正式回执均为 `syntax_error`、`child_calls=0`，修复提示已送达；分类为模型输出错误，没有替模型修改程序或放宽合同。

结构化 Hybrid 的失败包含两个层面：模型已执行全部浏览器要求，却没有调用 edit_json，仍重复点击/读取；程序对周期循环缺少提醒。后一层已通过独立回归确认并修复，不声称提醒必然改变模型选择。该夹具在任务中明确要求编辑，公布了 edit_json，并检查最终文件，但没有声明 edit 的工具成功规则，因此按规则生成的待办不会凭空增加这条规则。

结构化 Chat 首次在 done 被拒绝后补调带完整参数的 wait 并完成；动作与输出 Schema 合同通过，但“两段”排版诊断未通过。现有 gate 的通过字段不包含该排版诊断，结果如实保留。

最终源码完整 full-surface 矩阵位于 `full-surface-final/`，首次 9/12 通过；三个失败均独立重跑：

| 场景 | 最终源码首次 | 独立重跑 |
| --- | --- | --- |
| basic / Chat | 失败：模型重复语法错误，未执行子动作 | 通过 |
| structured / Chat | 通过 | — |
| legacy_structured / Chat | 失败：模型查找不存在的 Wait/Read 按钮，跳过实际动作却返回成功文字 | 通过 |
| reread / 快照恢复 | 通过，恢复快照 10 次 | — |
| stale_ref | 通过，真实陈旧 ref 拒绝后恢复 | — |
| stale_edit / Hybrid | 失败：模型误读 read_file 返回值，访问不存在的 `.value`，未执行 edit | 失败：模型程序含非法转义及伪造 repair token，未执行 edit |
| upload | 通过 | — |
| download | 通过 | — |
| strict Research | 通过 | — |
| strict Research sequence | 通过 | — |
| source_failure | 通过，按正式来源失败回执阻断 | — |
| fixed_failure | 通过，按正式来源失败回执阻断 | — |

重跑目录分别为 `full-surface-final-rerun-basic/`、`full-surface-final-rerun-legacy_structured/`、`full-surface-final-rerun-stale_edit/`。原始请求、响应和 SQLite 正式回执均检查过；每个失败另存 `*-failure-audit.json`，不覆盖主结果。上述最终失败没有显示新的生产确定性问题，分类为模型输出或规划错误。

用户指定的 `python -m tools.structured_actions_live_gate` 已运行，最终结果保存在 `structured-final/`：

| 模式 | 最终源码首次 | 独立重跑 |
| --- | --- | --- |
| Chat | 合同通过；首次 done 缺 wait，拒绝反馈后补调 wait 并完成。恢复快照 11 次；两段排版诊断仍为 false | 修正前的独立重跑及首次结果均保留在前表 |
| Hybrid | 失败：max_turns，浏览器要求均完成，未执行 edit_json。第 15–18 轮原始请求确认已包含周期提醒 | 失败：no_progress，连续三次相同 read 后正常触发只读停止；仍未执行 edit_json |

最终 Hybrid 重跑位于 `structured-final-hybrid-rerun/`，恢复快照 12 次。它与最终首次的失败均属于模型没有继续执行编辑及交付；没有修改动作合同或替模型补写文件。最终实机的 Hybrid 编辑冲突恢复仍未获得通过结果；修正前完整矩阵曾实际通过该场景，最终生产回归也通过了真实版本冲突恢复检查，两者不能代替最终模型实机通过。

所有实机批次 `summary.json` 的 source_before/source_after 均相同。另用包含未跟踪新增生产模块的 `runtime_implementation_id()` 校验最终回归与实机期间源码冻结：前后实现身份均为 `e182951f0ad01f0f07c57f8ef3601d240daf7ad71024f341308b7eab449f5606`。实机没有发现环境类失败；测试进程退出后的 RAM 盘清理 warning 另按上述限制保留。

## 限制

反馈不能保证模型选择正确的下一步。待办依据现有声明及正式回执，不从自然语言推导另一套完成合同。重复页面观察不能代替业务效果核验；仅有这些观察时，效果操作的最终停止仍依赖总轮次预算。旧 checkpoint 指纹不能换算，新投影从下一批起生效，切换时可能多一次首次观察。原有跨版本实现身份限制保留，没有承诺跨版本无缝恢复。
