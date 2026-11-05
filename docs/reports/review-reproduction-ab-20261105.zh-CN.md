# 独立 Review 与可执行复现 A/B（2026-11-05）

## 当前决定（后续用户澄清后的收口）

允许有限发现与漏检，不以“找出全部缺陷”验收。评价有效发现、误报、实际修复、回归、
正常结束与成本。用户要求保留原生产 Review 默认，撤掉尚未启用且收益不稳的生产实验
接线；复现 helper 移入 `tests/manual/review_reproduction_support.py`，不再由 `codey` 导入。
撤掉可选 parent_policy/replay_probes API、ReviewResult 附加字段和复现持久化分支；移除
只针对被撤实验服务的生命周期测试，保留实验执行/授权/候选冻结/独立 oracle 测试。

新实例 HEAD 2096d91e 的三组共同完成 2 类任务 × 缺陷/正确候选（每组 4 行）：

| 方案 | 修复/验证且正常结束 | 最终独立质量通过 | HTTP | 实际输入 token | 正确控制误修 |
|---|---:|---:|---:|---:|---:|
| baseline 原生产 Review | 3/4 | 3/4 | 10 | 38844 | 0/2 |
| candidate_static 干净材料/结构化审查 | 3/4 | 3/4 | 10 | 41136 | 0/2 |
| candidate 加可执行复现 | 2/4 | 3/4 | 27 | 245592 | 0/2 |

candidate 发现两类缺陷，但折扣代码正确后重复旧写入/验证停 no_progress；标签 Writer
引入多余 `}`，生产编辑器和 pytest 均已返回语法错误，模型重复旧 SEARCH 而未修复，
最终质量从 3/4 降至 0/4。不能因为“成功发现”掩盖返修回归，也不能归因证据丢失。
原方案/独立静态只修好折扣、漏标签；漏检本身不抹去折扣修复的收益。

这一完整 A/B 批原计划 24 行，实际保存 13 行；跨文件静态组再度卡住后停止。上表只用
三组共同完成的前 12 行，不是四类任务全部完成，也不把服务卡住的未完成行混入模型
质量分母。更早开发组 candidate 曾 4/4、其他组各 3/4，不能挑成功批替代后续不稳。

撤出前原样生产 headless `edit/project_review` 实机 2/2 通过，均 done，13 项 Review
生命周期检查全通过。它验证原默认方案，未启用复现分支；smoke 不代表完整 release。

## 服务卡住的独立根因

KoboldCpp 1.117.1 连续两次在相同 malformed Gemma 工具文本（replacements 数组缺 `]`）
生成 EOS 后不结束 HTTP。独立提取安装包原样 `toolcall_to_normalized_json` 与
`fix_unquoted_keys`，不导入 Codey、不加载模型：补齐括号控制立即返回，原始文本 3 秒
超时且 faulthandler 堆栈停在 `read_array`。当数组读到 `}` 时 read_value 不前进，
read_array 又只认 `]`，因此无限循环。未修改 KoboldCpp 安装包。

原先 native DLL 诊断无效：未加载模型时返回 null，首个控制还在 DLL 加载前超时，不能
用它定位 native parser。Python fallback 隔离才锁定上述确定性缺陷。隔离脚本曾对
TimeoutExpired.stdout 一律 decode，遇到 str 报错；已修实验脚本，非 Codey 生产问题。

用户已授权自行重启；已恢复同模型服务。本地 provider 外层在冻结新任务协议前，识别
标准 `/v1` 地址、`KoboldCpp` 精确版本 `1.117.1` 和 Gemma 4 模型名，复用现有文本工具
避开服务端 native 工具解析。其他模型（含 Qwen）、版本、Responses、自定义代理路径、
未知元数据均不改变。复用已有受限元数据读取：0.8 秒、262144 字节、禁止重定向转发密钥。
不修改安装包或保存偏好，不猜缺括号，不重发结果未知的生成。旧任务若冻结了受影响的
native 协议，恢复前明确拒绝并提示新建任务，不在恢复中偷换协议。

### 文本兼容实机中的第二个问题

首批纯文本 gate 在 grep/read 成功后重复生成 `<|channel>thought` / `<channel|>`，服务
仍持续生成 token。保存生成现场后主动 abort，因此该批不算通过，也不能称为 parser
再次死循环。同一实际请求以 128 token 对照：默认与 thinking=false 均重复标记；
thinking=true 正常推理。给 true 1024 token 后 249 token 正常结束，但服务把完整思考段
留在 content，reasoning_content 只有空白。同题 thinking=false 加既有 JSON Schema
约束，90 token 返回合法 edit。请求/响应/实际模板保留，不靠修改采样 seed 挑成功。

因此未开启 thinking（false/null）时选择既有 `json_schema` 文本协议并显式 false；null
沿用本模板原先 false 的默认值，不把用户显式 false 改成 true。明确开启时保留 true/plain，
本地 codec 只分离以已知标记开头且完整关闭的思考段，最终段仍经过既有文本/工具校验。
思考段中的工具不执行；缺失/重复 channel 拒绝，最终工具缺括号仍拒绝；不影响 Qwen 格式。

首轮回归暴露新测试漏配 WorkspaceRevisionStore，实际 edit 和 pytest 通过而 done 正确
被拒；补齐测试依赖，未放松生产完成证明。思考段测试又漏配读取授权、误把协议层
read_file 名称写成执行层 read，修正测试；不是生产错误。

第二批 `edit/references/project_review`：edit done、独立验证通过；Project Review done、
13 项生命周期与独立文件检查通过，但 gate 复制临时 baseline 时再次报 artifact_error。
根因是原分类只匹配中英文系统错误文字；子进程解码后本地文字乱码。新故障注入先得到
3 failed / 2 passed，再按稳定的 `[WinError 2/3]` / `[Errno 2]` 标识判断，仍要求源文件
确已不存在；权限、源仍存在但目标缺失、混合错误继续失败。修后相关 gate/兼容 43 passed。

references 的失败已定位到模型提出不存在的 SEARCH（先添加 Tuna，后使用字面量 `\\n`
与尾随空格），并重复旧参数；源码、精确编辑错误、测试失败均完整回传。模型在预算内
未修好，不能算成功；不把保留准确匹配守卫称为生产 bug，也不放宽为猜测替换。
### 最终生产复核与剩余边界

- 修后 `edit/project_review` gate **2/2 PASS**，都真实修改、执行 unittest 并 done。
  Project Review 的 13 项生命周期检查全通过，包括只读、持久结果、当前文件的验证与
  终态顺序。12 个实际 HTTP 请求全部没有 API tools/tool_choice，其中 10 个编码请求
  使用 JSON Schema，另 2 个是原 Review 请求。`--protocol native` 是请求偏好，实际
  已被兼容层改为文本；不能把这一结果写成 native wire 通过。smoke 的 release_ready=false
  来自未跑完整 release 矩阵，不能写作完整发布认证。
- 额外使用 `run_headless` 的**默认生产 registry/connector**，只包 HTTP 观察器，未替换
  provider 工厂或工具行为。默认 thinking=null 在准入时冻结 false/json_schema；5 次实际
  HTTP 无 API tools，真实编辑、unittest、完整性、当前验证及 done 通过。本轮原策略
  返回 Reviewer unavailable，随后正常单模型交付，不能把它冒充成功的 Review。
- 显式 thinking=true 的默认连接器补测：完整思考段第一次成功分离并执行 find_references，
  下一请求推理后反复 channel 标记。服务仍持续生成，保留现场后人工 abort，任务 error，
  不能写成通过或 server parser hang。第一次脚本错误把自动发现前的空地址当作选择，
  准入拒绝且未发模型请求；改为先正常发现端点，未更改生产身份验证。
- 因此本兼容**绕过已定位的服务端解析死循环**，没有证明显式 thinking/plain 在该量化
  模型上稳定，也没有保证跨文件编码质量。保留用户显式 thinking，不靠强制关闭或换 seed
  隐藏失败；未开启 thinking 的现有 JSON 路径已获得真实完成证据。无需把所有模型失败
  都强行改造成工程 bug，也不能把未完成任务算成成功。

最终相关 provider/协议/冻结/终态回归 **172 passed**；JSON 工具、恢复、原 Review、
实验 helper、快照与只读/冷复用扩大组 **144 passed**；gate 错误码修复后 gate/兼容组
**43 passed**。组间重叠，不相加声称独立测试总数。未运行全量 pytest。Ruff、生产树
Mypy（581 文件）、Pyrefly、ty 通过；Pyrefly 保留原有 35 suppressed / 80 warnings。

- [A/B 与服务诊断原始证据](review-reproduction-ab-evidence-20261105.json)：19 个 JSON
  批次/诊断，含失败与部分批；13.41 MB 去重为 6.79 MB，expand 后与原解析对象逐项相等。
  含安装包原函数全文/哈希、括号控制/超时堆栈、同请求 thinking/JSON 对照、原输出及回复。
- [生产与兼容实机证据](kobold-parser-compat-production-evidence-20261105.json)：172 份
  文本（含 8 份相关源代码），2.93 MB，逐文件 SHA-256 校验通过；含原生产、失败/中止批、
  最终 gate 与默认连接器的输入、provider/events、结果、文件、Review 与 ledger/trace。
  未归档 SQLite、重复 managed-output、链接、缓存和完整日志，不称为完整 durable 镜像。

当前采用：原 Review 保留，复现只留测试目录；本地兼容接入正式选择路径，不新增内核
专用业务规则或服务端补丁。更大任务和显式 thinking 的稳定性仍有上述实测限制。
分批提交：`9ab73efc` 撤出生产实验；`9830a558` 本地兼容、思考段 codec 与 gate 错误码
修复。最终实机测量时为待提交的同一份源码；640 份生产、39 份工具源码哈希均与测量
metadata 相等，提交没有夹入测量期间的新生产改动。

## 目标与准入

用户授权先 A/B，确认后接入生产，再使用原样生产代码实机验证。主树 D:\codey；保留
未跟踪的 docs/cline-integration-implementation-plan.zh-CN.md。串行使用 KoboldCpp。
不运行全量 pytest；扩大相关 Review、恢复、权限、provider 合同与执行测试；Ruff 与
Mypy/Pyrefly/ty 按生产 Python 3.11 检查。分批 commit，逐步记录失败与根因。

现状：生产 Review 已 new_chat，返修以 fresh=False 回到 Writer；Reviewer 只有静态请求，
没有实际复现工具。它仍收到 Writer summary、recent log 和 ChangeBrief。旧 counterexample
脚本只支持 app.py/select，不能作为通用生产实现。

## 预登记方法

- baseline：现有生产审查，维持现有影响图与相关上下文。
- independent：独立审查仅使用原始任务、diff、必要源码/调用方与地图，去掉 Writer 结论。
- reproduce：在 independent 相同材料上，由模型提出有需求依据的独立测试，程序在临时
  候选副本执行，将真实命令、退出码和输出送入独立最终审查，再交回 Writer。
- 每个缺陷都有对应正确控制，不能为了找到问题强制报告 bug；模型测试的预期不是 oracle。
  测试执行失败必须区分 AssertionError、导入/环境/语法错误。只有确定的需求违背才应返修。
- 已知题覆盖折扣边界和大小写去重；扩大题包含跨文件调用方与保留 falsy 默认值。
  预置候选是实验种入，不冒充自然 Writer 产出。独立 oracle 只在 arm 完成后评估。
- 先跑开发题三 arm，再跑扩大题和反序确认；保留失败，不通过挑 seed 或扩大预算掩盖倒退。
- 分别记录发现率、正确控制误报、实际复现有效性、Writer 修复后最终质量、正常终态、
  文件范围、HTTP 请求与实际 tokenizer 输入。缺失 usage 为 null，不当零。
- 接入生产前需要证明至少修复旧漏检且无新增误报、返修质量不倒退；若不满足，追查 prompt、
  工具接线、响应协议、执行环境与模型行为。不能因为能写失败测试就自动认定 bug。

## 生产边界候选（尚未接入）

复现只在拥有验证授权的编码任务启用；独立 Review 保持正式工作区不写入。临时副本不是
操作系统沙箱，沿用既有测试执行授权与命令守卫，不声称防御任意恶意代码。生成测试不得
扩大父任务权限。缺少依赖或不支持执行时明确标记未验证，不能伪造通过或把错误当 bug。
对新鲜度、快照、模型身份、证据持久化与 Writer 原会话继续保持原边界。

## 日志

- 2026-11-05：已核对现有生产调用与旧实验边界；开始构造可执行测试与静态上下文独立
  对照。当前生产代码未修改，原始结果计划保存在 tests/manual/results/review-reproduction-*。
- 开发批进行中：折扣缺陷三个 arm 都真实返修后通过；baseline 与 independent 仍漏检
  标签大小写去重。reproduce 已真实运行折扣边界脚本，修前失败、修后通过，正确折扣控制
  无误报。生成测试按需求核对，不把 AssertionError 本身当作语义判决。
- 执行器和独立评分定向测试 16 passed；覆盖原工作区不写入、无授权禁止执行、导入错误、
  0 tests、四类候选的正确/缺陷独立判定。Ruff 实验脚本通过。尚未接入生产。
- 开发首批完整 12 行：三组均修复折扣、均未误改正确控制，但三组都漏检标签缺陷。
  实际脚本仅检查同大小写重复和排序，未检查多项需求的交互；脚本真实执行、回执完整，
  不是 KoboldCpp 或生产协议失效。先修实验提问：要求指出源码中的具体假设，生成能区分
  实际表达式与需求的输入，优先组合约束，不硬编码标签答案。明确保留首批失败。
- 复核实验接线：baseline 的 impact map 先前单独传给生产服务，factory 未拿到同一字段；
  已统一 kwargs，保证 independent/reproduce 两边材料相同；新增实验源码 SHA-256。
  首批因此仍为开发诊断，不与修正后的确认数据混算。生产仍未修改。
- 假设版开发批：reproduce 4/4；baseline、independent 各 3/4，均仅漏标签缺陷。
  标签复现生成 'Apple, apple, APPLE, Banana'，真实得到重复 apple，需求明确要求 lowercase
  后去重；Writer 修复后独立 oracle 全过。不是靠手工向模型提供反例。所有正确控制无误报。
  CLI 因 baseline 漏检返回 1，不能误写成 reproduce 失败。
- 扩大前补证据交回：程序直接把真实失败脚本/输出带回 Writer，并在最终文件上原样重跑
  同一脚本，不只信 Reviewer 的自然语言摘要。初版补丁缩进错误由 Ruff 立即发现并修正，
  未进入模型运行或生产。接下来跑跨文件/默认值两类扩大题及四类反序确认。
- 扩大批定位到跨文件结论接线边界：模型正确发现未改 report.py 的 import，但把 finding
  path 写成未修改的 report.py；生产仅接纳 Changed files 的锚点，格式重试也重复错误路径，
  导致真实问题未交回 Writer。保留守卫，最终提示末尾明确列出允许的锚点，要求调用方问题
  绑定到引发它的 changed definition；不把未改文件伪造成 diff。
- 导入失败也要保留真实脚本/输出：本题不是第三方依赖缺失，而是候选自身旧符号 import。
  交回所有已执行的非零脚本，由 Writer 核实，不仅收 AssertionError。独立静态组的默认值
  二审还把显式保留 falsy 的需求读反，显示无证据的意见仍会误报，不能用 verdict 代替质量。
- 候选执行模块和边界回归已编写但未接入生产调用，28 项真实执行/授权/冻结文件与实验
  独立评分测试通过；单模块 Mypy、Ruff 通过。仍等待修正后扩大与确认数据。
- 扩大默认值 reproduce 已真实锁定 10 != 0，却在自由文本最终 JSON 的 issue 中输出未转义
  双引号；一次格式修复仍无效，结论未交回 Writer。这里是输出协议脆弱，不能说模型没找到
  bug。候选改为 native `submit_review_result` 结构化提交，path 用本次 changed-files enum，
  仍经过既有 Review 解析/路径守卫；不做盲目字符串修补或放宽锚点。下一批冻结源码后验证。
- 早期队列的 confirmation 实际加载的仍是自由文本最终提交版本（adapter hash
  8dd62d20893e53cbcf61b2e6a1529c8662f6d14668f71548989d5d6ee612693d，wire 无
  submit_review_result）；不能把它算作 native 修复确认。旧数据完整保留为诊断。
  此批跨文件代码和独立检查已正确，但 Writer 通过 pytest 后重复原 edit，最终 no_progress；
  实际 current context 和错误回执完整，不放宽门禁。交回证据明确标记 PRE-REPAIR，修好且
  当前检查通过应 done，旧输出不是当前文件仍坏的指令。此后每个模型批次运行期间冻结相关源码。
- 新一轮使用候选生产 helper，比较 baseline / candidate_static / candidate。后两者完全相同
  的干净材料和 native 最终提交，仅 candidate 增加实际复现；返修后重跑原失败脚本，避免
  二审重新出题漏过旧问题。parent_policy 尚未从自动编码入口接入，因此未改变默认执行行为。
- 候选 helper 固定版首批暴露确定性协议接线错误：native 提交声明了工具，但没有设置
  provider 已支持的 require_tool_call；模型合法输出普通 JSON，候选却只接受 tool_call，
  导致正确控制也无法完成 Review。已停止该批以免重复浪费推理，部分结果保留，不计作有效
  质量消融。修复为 proposal/final 两次请求均 require_tool_call=True，后续核对实际 wire。
- 新服务回归曾把 Path 误传给要求 ReviewArtifactStore 的读取函数；修正测试调用，非生产
  问题。新增授权/新对话/实际执行/持久化/原样重跑/未改调用方陈旧/未知发送不重试测试，
  修正后 26 passed。此前扩大现有 Review、coordinator、自动流程组 71 passed；
  Mypy 581 文件、Pyrefly（35 suppressed/80 warnings）、ty 无错误。
- required-native 两行 pilot 仍未提交工具；真实 wire 两次均 tool_choice=required，说明仅
  设置参数并不能迫使当前 KoboldCpp 返回工具。进一步发现候选把生产静态 Review 的
  “Return only JSON”与 JSON 示例原封不动送入 native 最终请求，和工具合同冲突。
  修正为仅替换自有输出指令，保留全部审查指导/项目数据/用户原文；增加幂等与原文保留回归。
  不修改 Gemma codec，不把普通 prose 捏造成执行记录。继续小 pilot 通过后才扩大。
- native-contract pilot 已真实找到/修复折扣，并原样重跑失败用例转绿；仍在二审终态失败：
  模型有时返回无 verdict 的 fenced JSON，或 approved 同时把“修复正确”的赞扬写进 findings，
  现有 parser 保守转 changes_requested。不能删除这些守卫来让控制通过。补齐 native 合同
  的三字段要求与“只报告当前未修缺陷，批准时 findings=[]”语义。只读最终回复若为普通
  文本，交给原 strict parser/一次格式修复，不捏造成工具执行；此兼容与具体模型无关。
- verdict-contract pilot（HEAD 184518bf）两行通过：缺陷折扣由真实脚本锁定并返修、
  正确折扣不返修；最终均 done、4/4 外部检查及当前快照 approved。分别 10/2 请求、
  71259/3890 实际输入 token。不能由此推断四类全部通过，继续固定版本 24 行三组 A/B。
- 本轮扩大相关回归（helper、实际 Review 服务、实验 oracle、自动同模型生命周期、
  身份、只读和冷启动复用）68 passed。模型批次运行时未修改生产/实验源码。
- 固定版 A/B 标签缺陷出现真实返修失败：生成用例以 `Apple, apple` 锁定重复，另一个
  blank/trim 用例原本通过；Reviewer 建议 replacement 遗漏 `strip()`，Writer 照做后
  旧 pytest 2/2 仍通过、原复现仍失败，二审正确拒绝批准。模型/回执均完整，非服务中断。
  这不能记为成功，也不能把 one-repair 限制解除直到碰巧通过。待本批冻结运行完，检查
  Reviewer 是否应只交回证据/被破坏的不变量、避免不经验证的 replacement，以及交回与
  重跑全部相关用例（包含原本通过的保留行为），而不是只保留失败用例。
- 扩大执行/快照/完成/收据回归 136 passed、2 skipped、82 subtests；Ruff 与完整生产树
  Mypy（581 files）、Pyrefly、ty 通过。Pyrefly 既有 35 suppressed/80 warnings 未改。
- 首轮 fixed-ab 在跨文件静态组的 Writer 请求卡住：已有 13 行完整结果，后续样本未完成。
  服务 `/generate/check` 留有完整工具文本，`perf.total_gens=1529` 长时间不变且 idle=0，
  Python 连接仍 Established；不能凭这些局部状态判定 GPU/服务内部根因，也不能把缓存
  文本当作已完成的 HTTP 工具调用。用户确认 KoboldCpp 卡住并授权自行重启。停止仅本次
  实验 PID 3152，保存 server-stall JSON 后重启原 exe 的父/子进程。原批保留为部分诊断，
  不补写成功或跨服务实例合成同批结论。
- 重启读取已有 `Gemma4-12B-QAT/2.kcpps`，同模型、262144 context、jinja/jinja_tools、
  GPU0；使用隐藏窗口，stdout/stderr 保存。旧实例运行时 GUI 设置无完整快照，因此新
  实例必须重新同批比较各组，不将新旧实例视为完全相同配置。服务已恢复 /models。
- 根据标签根因，候选改为只报告实际反例与被违背的需求，suggested_fix 为可选的高层
  不变量说明，不要求未经验证的 replacement 代码；Writer 交回与复核保留全部生成用例，
  包括原本通过的行为，避免修好一个断言却破坏另一个约束。仍仅一次有界返修。新增保留
  passing regression 测试，相关 59 passed、Ruff 通过；自动生产入口仍未接入。
- 新实例 regression-handoff pilot 标签缺陷未发现、正确控制通过，2/2 HTTP 均正常。
  实际生成的是“同大小写重复+空白”和“不同标签大小写+排序”两个分开的样本，没有将
  大小写和去重作用于同一批值；最终错误 approved。这是可见的模型测试选择漏检，非
  生产未执行或丢失用例。补充通用提问约束：交互要作用于同一输入值、操作组合要考虑
  顺序不同的输入；hypothesis 给出简短具体输入/实际与需求预期，不给任何题目答案。
