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
