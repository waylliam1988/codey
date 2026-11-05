# 编码管线候选 A/B 预注册与运行日志

主树 `D:\codey`，起点 `2c000ebd`。本轮只审计候选价值；删除或默认关闭必须有质量证据。
用户授权在发现确定性生产问题时修复、定向扩大测试、检查 Ruff/类型、分批提交。

## 固定方法

- 当前 KoboldCpp 单模型，原生 native Writer，串行请求，600 秒 provider 超时。
- 同题 arm 用独立临时目录与全新聊天；保存每行结果、原始回复、文件、工具回执、真实 wire
  请求和 usage。失败也计入分母，不把 `no_progress` 直接归为模型能力或服务故障。
- 质量由项目外的独立行为检查判定；可见测试和被模型修改的测试不作为唯一质量依据。
  授权文件范围、生产终态、有效验证、耗时与实际 API usage 分别记录。
- 首轮固定顺序，确认轮反转顺序。不得把更改代码、任务或预算后的样本与旧批次混算。
- 核心任务覆盖已有 bug、功能、跨文件重构和从零生成；不是仅改常量。

## 六个候选及可回答范围

| 候选 | 对照 | 接线与范围 |
|---|---|---|
| Project Map | baseline / map | 相同 AgentRequest，只注入生产地图 |
| coding current context | baseline / context | 相同 AgentRequest，只切生产开关 |
| 自动规划 | 无规划 / 同模型额外规划 | 仅从零任务，复用生产规划 prompt 和 ChangeBrief；不伪称独立 advisor |
| 前置 audit | 无 audit / 同模型只读 audit | 已有项目，调用生产只读 advisor kernel 和 ChangeBrief；不改 advisor provider 选择策略 |
| 项目记忆 | 无注入 / 生产事实注入 | 冷启动空 store 与真实成功运行后的续作分别测试；持久化再加载，不手写答案冒充记忆 |
| Review / 返修 | 不启用 / Review 无返修 / Review 加一次返修 | 相同候选 diff 的缺陷留出与正确控制样本；生产 Review attempt、snapshot 与 Coordinator，最终行为独立检查 |

规划与 audit 的真实自动调度在只有一个 provider 时不会选到独立 advisor。本轮明确检验
“同模型多走这一步值不值”，不通过注册假 provider 改写产品语义。Review 使用本地
self-review，其结果也不等于第二模型独立审查。

## 运行日志

- 2026-11-05：核对生产入口和已有实验脚本。旧四类 fixture 中存在评估歧义：功能题预置了
  已有覆盖却强制要求测试文件变化；重构题强制要求修改未引用旧符号的测试。新任务用
  独立行为 oracle 与授权文件范围判定，避免把“不做无意义测试改动”算失败。
- 下一步：建立统一证据与计费观察，先跑地图/current context，再跑规划/audit、持久记忆与
  固定 diff 的 Review/返修。每批结果追加到本日志和主审计报告。
- 2026-11-05：新 native 实机已有项目的 bug、功能、跨文件重构都完成并通过独立检查。
  两道从零题代码正确，却停在反复执行未授权的 `python test_*.py` 与不可用的 shell approval。
  进一步定位：native continuation 提前返回，coding current context 只在初始请求准备，
  没有在后续请求交付。修复为 native 每次请求冻结前刷新投影；支持 request-scoped 的
  provider 通过临时 observations 交付，其他 native provider 通过保留 call id 的结果旁文交付。
  确定性回归先红后绿，包含开/关、两类 native provider。修复前十五行批次单独保留，
  不把那个未真正生效的 context arm 当作有效候选消融。
- 计费观察：当前服务没有在 SSE usage 中报告 token；保留 `missing/null`，使用实际请求
  送出前的 KoboldCpp tokenizer 计数比较输入，另记录真实 HTTP attempt。最初 pilot 显示的
  usage 0 是新脚本把未知值当零的错误，原始 usage 已保留；后续已修为 null，并单列 tokenizer
  计数。不是生产计费报告零用量，也不据此判断服务故障。
- 从零题复测继续定位：模型改为合法 `python -m unittest test_*.py`，运行确实成功，但门禁
  选择的是完整 discovery；保守覆盖判定不应放宽。原拒绝反馈没有写出缺少的具体命令，
  模型反复 done。补充门禁失败时的所需命令与 cwd 后，两道题各 6 轮 done、独立检查
  4/4。修复不依赖 current context 开关、不放宽权限，也不把局部测试当完整覆盖。
- 修复批定向验证：127 passed、3 skipped、20 subtests；Mypy 580 源文件无错误，
  Pyrefly 0 errors（保留既有 suppressed/warnings）、ty passed。后续正式 A/B 固定这版生产。
- 固定版地图/current context 首轮 15 次：各 arm done+外部质量+范围合计均 4/5；外部
  产品行为合计均 20/20。baseline/map/context 的请求总数 40/43/39，输入 tokenizer
  总数 244053/284935/245105。五题不能只摘已有项目的地图节省：slug 从零题三边都失败。
  baseline 重复未授权脚本与无审批通道 shell；map 除此之外多次无视门禁给出的 discovery；
  context 自写测试错误地要求 apostrophe 被删除（合同要求替换为 hyphen），随后反复改正确
  产品代码、复用已失效 read receipt。真实失败回执和提示完整，尚未发现该分支协议丢失。
- 固定 diff Review 首轮 12 行：折扣缺陷 baseline/review-only/repair 外部得分 2/2/4；
  标签大小写去重缺陷 3/3/3，后两边 reviewer 错误 approved。两份正确控制均 4/4且未返修。
  缺陷折扣返修消耗 7 请求、35414 输入 token（含重新 Review）；正确代码审查各加 1 请求。
  不能把 approved 当代码正确，也不能因一个漏检宣称 Review 全无收益。
- 当前串行队列（session 35632）：同模型 plan→baseline 两个从零题；audit→baseline 三个
  已有项目题；memory 冷启动/真实历史续作；context→map→baseline 反序确认五题；
  repair→review→none 反序 Review 确认。原始输出均在 tests/manual/results/coding-value-*。
  不同时向单个 KoboldCpp 服务发起多个实验。
- 记忆实机开始前补齐 harness：除生产 facts/context 外，开启组也连接生产
  `build_research_tools` 与实际 store，使模型可以主动 knowledge_search/read；关闭组使用同
  一构造器但无 store。不能因为实验漏接检索工具而宣称知识笔记无价值。两边均按生产释放
  搜索资源；不额外提示模型去检索，也不把手工搜索结果塞进提示。
- 扩大定向测试：上下文/门禁/恢复组 127 passed、3 skipped；记忆/Review/audit 组
  68 passed；headless 与完成生命周期组 57 passed；consensus、自动 Review、snapshot
  与已知失败不重发组 92 passed；归档可逆性与 oracle 等新增 harness 测试 5 passed。
  各组有交集，不相加宣称独立测试数；一次测试命令引用不存在的文件后未执行任何测试，
  用 rg 确认真实模块名后重跑，是命令编写问题，不是测试落后或生产故障。
- stage 首轮完成：plan 两题与 baseline 两题均未终态完成，产品外部行为各 4/4；不能由
  双方失败证明规划必要。audit 三题已产出可用报告、只读范围未变，但质量成功为 2/3，
  同批 baseline 3/3。重构 audit 准确给出三文件修改建议，Writer 却重复读 report.py。
  wire 核对显示 Writer 新聊天已清空 audit 会话、写工具仍可用、报告已交付，没有角色/历史
  泄漏。请求额外成本与模型未执行建议分别记账。
- 首轮只测各候选独立存在；补登记组合验证：加 `combined`（map+context）补齐 2×2 角，
  并在两边保留 map/current context 的条件下确认 plan/audit。不能把关闭其他候选后的结果
  直接外推为整条默认生产管线删除收益。
- 记忆首轮 8/8 成功，真实 headless 历史两条均 done、各持久化 implementation/verification
  两笔；两道续作 memory 事实注入分别 312/355 字符。没有显式 knowledge_search/read。
  仅看 builder 的 knowledge_context 为空还不够：共享内核另有 knowledge_background 检索。
  核对实际 wire，本批未召回笔记正文，冷启动也注入了约 105–110 token 的 owner/写入范围
  说明。因此冷启动不是完全相同提示，不把少一两次读文件直接归因于“空记忆里的事实”。
- 新增精简候选：`memory_compact` 只移除不具备 knowledge.write 时的 owner/写入作用域
  说明，保留全部事实、background recall、检索工具与服务端权限。先在同一真实历史和相同
  目录上与原 memory 双向 A/B，确认后才接入生产。原 memory/none 批次仍按原版保留。
- 反序确认结束：map/current context 三组仍各 4/5，slug 的 context 再次生成错误测试并
  重复无效编辑。Review 反序完全复现：折扣缺陷能修复、标签去重缺陷漏检、正确控制无误改。
- 扩大评分边界：最初四个 oracle 只评估产品行为；现在另用项目外进程执行最终可见测试，
  unittest 0 tests 不算通过，并补足 slug oracle 的 apostrophe 断言。`quality_score` 保留
  四项产品行为计数，`quality_passed` 同时要求可见测试通过。旧 raw 不重写，新增
  evaluate_coding_pipeline_results.py 从存档文件离线重评（不再次调用模型）。
- 当前串行 session 33297：combined 五题；保留生产上下文的 baseline→plan 两题与
  baseline→audit 三题；memory→memory_compact 冷/暖四题；反序四题。生产代码仍固定
  7f05b589；本轮候选精简尚未接入。离线重评 session 32270 可与推理并行，不访问模型。
- 离线重评已完成，包含生成测试的最终质量：两轮地图/context 共 30 行，各有 1 行生成
  测试失败；plan 首轮 4 行有 1 行生成测试失败；audit 6 行有 1 行产品行为失败；memory
  8 行均通过。Review 两轮每轮 12 行最终行为通过 7 行，可见测试却 12/12：再次说明可见
  测试不能取代独立边界 oracle。原始 JSON 不改，重评单独归档。
- map+context 联合五题 5/5 done、独立产品行为和最终测试都通过。该角单独显示组合可能
  比单个投影稳定；不能由“context 单独总成本更高”直接推导默认组合可以删掉 context。
- 保留 map+context 的规划确认：baseline 两题 2/2；plan 1/2。slug 计划把非 ASCII 示例
  的结果错误写成 hello-world（合同只替换，不翻译），Writer 将该例照搬为测试，真实测试
  失败后反复无效编辑。正确产品行为 4/4 不能覆盖错误测试。该错误已出现在 plan 原文，
  不是 harness 修改验收，也不是生产吞掉命令。与 inventory 多 1 请求、输入 36688→53683
  且质量相同合看，本地同模型额外规划未见净收益；不为此伪造第二 advisor 身份。
- 保留生产上下文的 audit 确认：baseline 3/3、18 请求、108757 输入 token；audit 2/3、
  27 请求、177140 输入 token。tags 审计耗完四轮没有 done/报告，单独记录 stage_usable=false；
  Writer 本身仍成功。重构只改 totals.py 后反复复用旧 read receipt，调用方和测试仍旧符号，
  独立 pytest ImportError；不是 pytest 过时，不修改期望去迎合未完成重构。
- scope 说明精简首轮已确认实际处理生效：memory 请求仍有 owner 写说明，memory_compact
  请求计数为零；保留 facts、store、检索与独立 suite。目前已完成冷启动两题和续作首题，
  均通过。等待完整反序结果后再改生产 `knowledge_context.py`，不覆盖原版证据。
- 只读说明 A/B 和反序共 16 行全部通过；每个 arm 为 8/8。原 memory 44 请求、257814
  输入 token；compact 43 请求、247475 输入 token。首请求减少 102–107 token，固定删除
  455 字符写入说明；总 token 降幅约 4.0% 还含一次请求路径差异，不全归给固定删字。
- 已接入 `knowledge_context.py`：仅 knowledge.write 被授权时提供写入 scope 指南。
  新回归先红（只读仍收到写入说明）后绿，并证明真实作用域内 recall 仍存在、可写任务
  继续收到原指南。定向知识/上下文/harness 组 69 passed；Ruff 与三种生产类型检查通过。
  下一步用无 monkeypatch 的标准 memory arm 和真实 headless edit/project_review 再验生产。
- 最终生产复测完成，固定 HEAD `c4bbb18e`：标准 memory arm 冷启动两题、真实历史续作
  两题全部 done、产品行为/可见测试/范围通过，23 请求、133155 输入；本批实际 seed
  历史两条也 done 且质量通过。全部请求不含只读写入指南，没有替换生产函数。
- 完整 headless smoke 选定 edit / project_review 均 PASS；前者 5 Writer turn / 6 HTTP
  attempt，后者 7 HTTP attempt / 一次真实 self-review，13 项生命周期检查全过；无 retry。
  最终独立验证通过。gate 的 `release_ready=false` 是其余发布矩阵未跑，不是选定任务失败。
  metadata 中生产文件哈希已与当前源码逐项核对，运行时无未提交生产修改。
- 正式证据已归档 `coding-pipeline-value-evidence-20261105.json`：14 批、107 行任务/控制、
  8 条实际成功历史；675 个不同请求条目，30.25 MB 去重为 7.85 MB，expand 与原始解析
  JSON 完全一致。107 行包括无模型调用的固定 diff 控制，不叫 107 次模型编码。
  `coding-pipeline-production-evidence-20261105.json` 保存 33 份生产文本制品，明确排除
  SQLite 与重复 managed-output，不冒充完整 state 快照。失败与旧证据均未删除。
- 主报告阶段 17 已收口六项结论：保留地图/当前上下文组合；不强制同模型规划和 audit；
  记忆保留可信事实/命令、删除无权限写指南；保留有界 Review 返修与二次 Review，公开漏检。
  两个生产接线/反馈修复 `7f05b589`，提示精简 `c4bbb18e`。尚未测试独立多 provider、
  跨模型、大项目与长时间记忆，不再把“本地六候选本轮完成”写成所有代码都必要。
- 收口复查：harness/只读知识指南/native 实际编辑与完成门禁 12 passed（13.98s）；
  14 批可逆归档、8 条成功历史、33 份 gate 文本哈希、639 个生产源文件哈希、
  2 条生产任务和 13 项 Review 生命周期、证据链接逐项通过。只改报告/归档后未无故重跑
  已通过的全套类型检查；未运行全量 pytest，未触碰用户未跟踪的 cline 计划文档。
