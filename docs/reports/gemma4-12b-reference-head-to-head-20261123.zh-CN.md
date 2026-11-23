# Gemma 4 12B 原生工具实测

2026-11-23。八个项目共用本机 KoboldCpp 的 Gemma 4 12B。编码用同一个两文件维护任务，网页用实际打开的公开练习站，研究用家庭记账应用的 SQLite 部署问题。各项目只参加有对应能力的任务。CLI、薄工具适配和录制请求复放分别记账。每题是一次样本，适配层和工具协议也有差异，结果适合定位机制。

| 项目 | 任务与入口 | 实际结果 |
|---|---|---|
| Codey | 编码，JSON Schema 文本兼容与普通文本 | 当时试验配置请求了原生工具，但实际请求没有 `tools`。KoboldCpp 1.117.1 兼容层切换到了 JSON Schema 文本协议。前两次请求成功读取文件，随后两次编辑参数生成截断，独立测试 0/2。普通文本复跑修改了两个文件，但显示函数没调用规范化函数，独立测试 1/2。真正的原生实测见后文。 |
| Codex | 编码，安装版 CLI 通过 Responses | 本地模型请求 HTTP 200。工具帧留在答复内容里，CLI 没有执行读取或修改，独立测试 0/2。安装版与参考 checkout 分开记。 |
| OpenCode | 编码，参考 checkout 通过 Bun | 前几次为启动环境问题。更正入口后收到 10 次成功模型响应，观察器在超时时汇总失败。该次工作区独立测试 1/2，终态缺失。修正观察器后另跑 90 秒，9 次请求后超时，独立测试仍未全部通过。 |
| DeepSeek Harness | 编码，参考 checkout 的 headless CLI | 原生工具实际执行，较早直接读取两个目标模块和测试。受限请求预算耗尽，独立测试 1/2。 |
| Pi | 编码，参考 checkout 的原生工具 | 有真实目录、读取和编辑动作。用完请求预算，独立测试 1/2。进程退出码 0 没有计作业务完成。 |
| agent-browser | 网页，0.41.2 原生命令加类型化模型适配 | 6 次生成，成功打开 Travel 分类和最便宜的商品详情。答案、真实页面 URL、价格和库存四项一致。适配器把点击后的 URL 与正文交还给模型。 |
| agent-desktop | 网页，1.2.0 Windows 原生命令 | 实际打开独立 Edge 窗口并执行点击。一次看见 443 个节点，引用上限 200。遇到非唯一目标时明确拒绝派发，10 次生成未完成商品核验。 |
| HyperResearch | 研究，参考 checkout 的 BuiltinProvider 组件和原生 CLI | 自主模型回合没有执行取证便回答，错误推荐跨机器网络共享上的 WAL。直接复验获取官方来源时，内置网络守卫拒绝了本环境把域名解析到的非公开地址。原生 CLI 的独立研究库初始化成功，`fetch` 复现相同的拒绝。尚无完整原生研究流水线成绩。 |

## 网页与研究的独立判据

网页题核对分类、实际到达的商品详情 URL、书名、价格与库存。agent-browser 返回《The Road to Little Dribbling》、£23.21 和 3 本，并有真实点击、URL 和正文回执。Codey 的两次自主原对话与紧凑观察复跑均耗尽 6 轮。复放区域观察路径后，实际打开了同一商品，Gemma 从正式读取回执给出了正确答案。复放格用于确认修复，未计自主通过。 [Travel 分类](https://books.toscrape.com/catalogue/category/books/travel_2/index.html) · [商品详情](https://books.toscrape.com/catalogue/the-road-to-little-dribbling-adventures-of-an-american-in-britain-notes-from-a-small-island-2_277/index.html)

研究题要求区分同一电脑和跨电脑共享，给出可靠共享与备份方式，每项关联实际打开的第一方资料。Codey 在配置独立研究库后能完成搜索、打开和发布，但仅打开了 DEV Community 二手文章。最终报告错误地推荐两个场景都启用 WAL，完成状态不能作为事实正确的证据。SQLite 官方说明 WAL 依赖同主机共享内存，跨电脑直接打开网络共享上的 WAL 数据库不适用。官方资料另有联网部署和在线备份说明。两条研究路径均未通过独立判据。 [WAL](https://www.sqlite.org/wal.html) · [跨网络部署](https://www.sqlite.org/useovernet.html) · [在线备份](https://www.sqlite.org/backup.html)

## 从执行记录修复的断点

- **先取证再解析。** 对静态路径明确且有读取权限的只读 CodeMode 程序，排入已有的正常 `read_file`，后续 `read_data` 仍从真实回执解析。同一路径在脚本中只补一次，已有读取则直接沿旧路径，程序自己写了读取也原样保留。之前漏读的模型脚本原样复放即通过，两份文件各读一次，返回 `0.175` 和 `0.325`，各选 2 行。冻结输入合同记录选择时已观察的路径，旧版本保持原行为。
- **浏览器的区域身份与观察预算。** Codey 第一次把已显示的 `region-*` 当作范围参数时，运行时返回了 `unknown_ref`。现在通过现有节点注册表查到同一文档中已观察的容器，沿用连接检查，区域标识只提供观察权限。借鉴 agent-browser 的紧凑呈现，模型视图省略坐标和无意义空字段，正式回执保留几何、状态和身份。分类页实发的结果块从 23,561 字符降到 11,805 字符，这里是字符数。缩短数据后自主运行仍会耗尽轮次。
- **本地工具帧。** agent-browser 首次收到的 Gemma 帧是 `open(url="…")`，仅接 JSON 对象的适配器拒绝了它。复用 Codey 的字面值解析器解析完整的关键字参数帧，随后继续走原工具参数校验。重复参数、表达式、位置参数和多余文本仍拒绝。未增加提示模板。
- **编码检查。** 把真实的 `unittest` 失败块和进程退出码接入原先只识别 pytest 的检查指纹。失败数和名称必须对齐，工作区版本仍参与比较。原始失败可稳定辨认为 `test_app.Names.test_display`，与重复启动进程的身份分开记录。修后自主复跑卡在编辑匹配，本次没有进入测试阶段，不能据此归因端到端收益。

## 工件

原始模型请求、返回、操作回执、独立测试输出和接入失败均保存在本地 `.e2e-artifacts/gemma-eight-20261123`。主要入口为 `coding-native`、`codex-rerun`、`opencode-bun-established`、`opencode-observer-fixed`、`agent-browser-native-tools`、`desktop-codec`、`hyperresearch-native-tools`、`hyperresearch-cli`、`hyperresearch-source-replay`、`codey-csv`、`codey-scope`、`codey-browser-compact`、`codey-coding`、`codey-coding-recheck` 和 `codey-research-rerun`。最终 244 项定向回归通过，覆盖测试快照、文件回执、浏览器观察与作用域、本地协议和进度识别。全树 Ruff 和生产树 mypy、Pyrefly、ty 均通过。

## 回执交接与真实协议复验

后续记录在 `.e2e-artifacts/gemma-closure-20261123`。测试项目由小型验收样例创建。继续使用原有 KoboldCpp 1.117.1 和 Gemma 4 12B。

- **网页。** `public-first` 的初次改版已经交付导航后的快照，但模型选择的 `purpose=action` 仍令链接点击只交付了送达回执，6 轮耗尽。修复后 `public-second` 在 4 轮中完成，自主打开分类和商品页面。真实商品快照与答案中的链接、£23.21 和库存 3 本一致。跟进步骤沿用 CodeMode 的子回执和浏览器文档身份。旧的下一步说明只进入当轮上下文。
- **慢页面。** 延迟 4.8 秒的取件页排查出两个分支断点。`slow-first` 的 `controls` 等待绑在旧文档，`slow-second` 的 `result` 试图在旧文档确认局部结果。两个原始失败均保留。`slow-third` 用原点击回执等待新文档，记录 3 秒超时和当前快照，随后正式读取目的页并答对 K-18、BRANCH_71、页面链接，实际点击一次。初始评分器只认详情快照，漏掉这次正式读取。独立重评分 `browser-independent-score.json` 直接核对同一个运行的回执和页面正文。
- **编码。** 原生小合同以 `auto` 和 `required` 分别完成只读调用、结果交接及下一次调用。完整原生测试从记录真实 `tools` 的请求开始，`code-native-first` 在模型配置绑定阶段失败，`code-native-retry` 更正验收入口的地址后通过两个独立测试。该诊断通过现有 ApiProvider 显式选择原生连接。本地生产兼容规则保留。JSON Schema 文本路径 `code-json-first` 同样通过两个独立测试。两条路径均曾写出会失败的中间修改，原始回执保留。补测 `coding-extra-oracle.json` 显示，原生结果额外丢弃重音字母，两条路径均删除了题目只要求删除 ASCII 标点时应保留的全角标点。2/2 指原有验收用例。
- **研究。** `research-first` 真实打开 SQLite 官方 WAL 文档，回答正确拒绝跨电脑直接使用 WAL，但仍误导共享与备份。随后 `research-second` 和 `research-third` 先遇到检索二手文章的问题。第一方来源补取阻止了旧证据集上的首次完整提交，后续模型引用了官方来源，仍然误述跨主机的共享内存限制。这些报告均保留原样，第一方来源资格和结论正确性各自验收。 [SQLite WAL](https://www.sqlite.org/wal.html) · [SQLite 跨网络使用](https://www.sqlite.org/useovernet.html) · [SQLite 在线备份](https://www.sqlite.org/backup.html)

## 再次验证的边界

- **同文档弹窗。** 两次实机均只派发一次点击，核对了 M-24 和 MODAL_72。`spa-first` 的新文档等待如实超时，子快照已观察到弹窗，但父调用错误地要求恢复，正式读回延至第 4 轮。`spa-rerun` 沿原点击、等待和快照回执确认同文档结果，正式读取及父调用在第 2 轮成功结算。无变化的慢导航继续阻止重复点击，后来出现的其他观察不能追溯改变一次已冻结的跟进。
- **编辑。** `code-symbol-limit` 把第 3 个请求限到 36 token，JSON Schema 路径第一次重试产生重复转义。隔离采样约束的 `code-symbol-plain-probe` 实际提交了符号编辑并通过 2/2。生产实现用冻结的读取回执收窄下一次编辑合同。`code-symbol-production` 的第一个重试产生无效转义，随后写入的代码漏了 import，独立验证停在 1/2。后续再次截断的原始编辑参数缺少结束引号，内核没有派发。多行字面量任务的原生自主 `code-symbol-multiline-first` 走常规编辑并通过 2/2。`code-symbol-multiline-probe` 通过真实 JSON Schema 连接进入 CodeMode，模型仅传文件、符号和定义，内核绑定上一轮的唯一完整读回执，2 项独立断言确认字符串精确匹配且外围定义保留。
- **研究。** 补搜可以覆盖漏搜的第一问，并考虑 WAL、NFS 等短缩写和已读段落。补搜后同一段落仍可引用。报告重放验证了部分完成尾注幂等，尾注不生成正文声明。按用户要求引用的整组链接进入已有来源登记路径。`research-known-sources-rerun` 实际打开并引用三份官方文档，仍给出直接共享数据库文件的错误建议，正文停在半句话。原始响应的结束原因是 `stop`。来源核验通过，业务答案未通过。项目官方文档的内置信任策略目前只列了 SQLite。
- **原文预算。** 前一次研究在正式回执中保存了 WAL 的同主机限制，发给模型的请求却只有段落索引。按整页淘汰时，三份原文均超出展示预算。第一次改版 `research-excerpts-rerun` 还暴露了任务识别的长度限制。带多个主题的完整短语没有命中官方文档要求，导致证据视图的回退未启用。修复后，常规整窗能放下时照常展示，整窗全部被淘汰时可在同一预算内选完整的第一方段落，沿用原有引用编号和范围。`research-excerpts-fixed` 的首个真实请求包含同主机限制、客户端／服务器方案和在线备份原文。Gemma 引用这三段，主方案改为单机托管 SQLite 并经代理访问，或用客户端／服务器数据库，备份用 Online Backup API。附带的 rollback 模式建议仍缺网络文件锁与同步可靠性的前提。
- **自然组合。** `natural-mixed-final` 只提供用户目标。模型第一次写入被规格读取条件拦截，随后从 README 找到规格并实际打开。三个业务文件正确，Python 与 Node 独立测试均退出 0，页面读回确认当前构建使用 347 g、CEDAR_947。独立评分仍失败，因为最后正文只给出记录链接，遗漏规格链接。内核现有的修改来源绑定和规格读回提供了两个引用身份，按用户明确要求交给现有引用渲染器补齐，避免模型转抄 URL。`natural-mixed-citations-rerun` 的九项独立检查均通过。模型原始摘要的最后一条说明仍未写完，记录和规格链接由宿主生成。

交接手册的命令在当前分支通过 127 项，A/B 自测也验证了两臂的 brief 各自出现在首个请求。浏览器定向组 70 项、编码和研究组 187 项、弹窗及 CodeMode 回归 122 项、研究证据预算回归 106 项均通过。组合与引用组首跑出现六项旧合同断言失败，保持原生产合同并修正断言后，受影响的来源间隙合同 79 项通过。全仓 Ruff 和 CI 定义的生产代码 mypy、Pyrefly、ty 矩阵通过，覆盖 Python 3.11、3.12、3.13。定向组之间有重复覆盖。
