# Gemma 4 12B 原生工具实测

2026-11-23。八个项目共用本机 KoboldCpp 的 Gemma 4 12B。编码用同一个两文件维护任务，网页用实际打开的公开练习站，研究用家庭记账应用的 SQLite 部署问题。各项目只参加有对应能力的任务。CLI、薄工具适配和录制请求复放分别记账。每题是一次样本，适配层和工具协议也有差异，结果适合定位机制。

| 项目 | 任务与入口 | 实际结果 |
|---|---|---|
| Codey | 编码，原生和本地文本协议 | 统一预算的原生试验在两次输出截断后结束，独立测试 0/2。文本协议复跑实际修改了两个文件，但显示函数没调用规范化函数，独立测试 1/2，四次运行均报告同一失败用例。 |
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
