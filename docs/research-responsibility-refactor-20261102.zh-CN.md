# Research 相关职责拆分与行数门禁收紧

基线：`4f321f790471d0562ace2c5adab5217bfc72d786`。本轮处理此前登记的五个超长文件，按已有职责迁移实现并修改真实消费者，没有增加另一套执行循环、状态镜像或持久化写入入口。

## 职责归属

| 原文件 | 重构前 → 后行数 | 当前职责与迁出位置 |
| --- | --- | --- |
| `research/tools.py` | 1,027 → 866 | 保留研究工具操作；`StagedKnowledgeStore`、`StagedKnowledgeChanges` 迁入 `research/staging.py`，延续原来的提交、冲突检测和回滚 |
| `research/browser_search.py` | 1,233 → 903 | 保留浏览器工作线程、页面和会话；HTTP/PDF 获取、重定向和下载限制迁入 `research/source_download.py`；可见正文质量判断归入已有 `research/extract.py` |
| `providers/api_transport.py` | 1,053 → 871 | 保留唯一的 `AttemptControl`、`BoundedBodyReader`、连接借还、关闭及响应校验；完整 `generate()` 迁入 `providers/api_generation.py` |
| `providers/api_provider.py` | 1,147 → 986 | 保留主请求、历史提交和生命周期协调；上下文快照、计数、压缩提交、辅助请求及维护调度迁入无独立状态的 `providers/api_context.py` |
| `runtime/core/operation_state.py` | 1,473 → 831 | 保留唯一的合法转移表、转移函数、store 和 checkpoint 适配；状态数据类型、常量和载荷解析归入已有 `runtime/core/operation_payload.py` |

新模块分别为 staging 171 行、source download 282 行、API generation 198 行、API context 185 行。已有 extract 为 157 行、operation payload 为 748 行。这些尺寸不是拆分目标；迁出的完整职责和调用关系才是边界。

暂存仍包住原 `KnowledgeStore`，最终写入继续使用原发布事务；API context 函数接收原 provider，继续使用其历史、锁、ledger 和 compaction coordinator；API generation 直接调用原 transport，共享同一个取消控制和连接归还流程。原位置没有另加转发函数或兼容执行分支。

`_json_object()` 同时服务普通 JSON 和 SSE 响应，因此继续由低层 transport 维护。generation 与 stream 都依赖 transport；移除了 transport 对 stream 的无用转发导入，也避免了 stream 反向依赖 generation。Operation 的 schema 仍为 **6**，没有新增格式或第二份转移表。

## 门禁只有一份记录

`tests/architecture-size-limits.json` 同时记录超长文件名单与精确尺寸；`tests/support/architecture_limits.py` 负责检查。原来分开的集合和上限字典已移除。

- 未登记文件超过 1,000 行，失败。
- 已登记文件超过尺寸，失败。
- 已登记文件缩短但仍超过 1,000 行，要求同次下调记录。
- 文件降到 1,000 行以内，或文件已不存在，要求删除豁免。
- 非整数、布尔值或不超过门槛的豁免尺寸，失败。

五个本轮目标文件都已移除豁免。其余 **14 个**超长文件保留精确尺寸，没有增加增长余量，也不宣称这些文件的结构债务已解决。九种门禁场景有独立检查。

## 消费者迁移与回归发现

同步修改了生产导入、测试补丁目标、手工实验和基准脚本。OpenCode/Pi 对照适配器继续只禁用当前实例的维护调度，不改写全局函数。维护阈值校准脚本修改候选快照中的 `api_context.py`；旧版工作树的 JSON 响应观察仍使用原有 transport 入口。

首次扩大回归选择 296 个文件或节点，冻结期间源码未变化，结果为 3,037 passed、589 个 subtest 通过、27 skipped、18 failed。失败没有被剔除：

| 类别 | 数量 | 原因与处理 |
| --- | --- | --- |
| 本轮消费者迁移漏项 | 6 | 五项下载测试还在原模块修改下载策略或常量，另有校准脚本读取旧文件；改到当前实现和查找位置，保留重定向拒绝、下载上限、线程归属和原工作树不变的断言 |
| 原提交已复现 | 12 | 删除入口的结构测试、旧 schema 断言、恢复测试替身缺少当前请求参数、过时的运行时文件清单、缺少问题登记的旧合同夹具，以及忽略时钟消息的结果位置断言和缺少 task 状态的 UI 夹具 |

12 项原有失败在独立的 `4f321f79` worktree 复现，原日志和失败节点保存在产物目录。schema 检查现在从实际载荷模块导入，明确锁定 v6 并拒绝旧版及非整数版本；旧报告合同测试显式登记问题，仍验证缺字段、非法任务引用和格式要求。当前整篇提交合同没有为这些旧测试放宽。

恢复实验的脚本 provider 原先替换整段 `_generate()`，已经绕过当前的请求准备。现在仅在 `_generate_attempt()` 返回确定性响应，让生产的合同、计数、请求策略、历史和持久化继续执行。文本合同断言据实际发出的 system 消息验证唯一性；Results 断言按 `function_call_output` 角色识别结果，避免将末尾时钟消息误当作工具结果。

运行时清单通过原来的 `python -m tools.publish_runtime` 本地生成。清单中的新增模块与此前删除的模块同步更新；没有另建清单来源或发布外部运行环境。

进一步扩大到 311 个选择后，原默认临时目录中有两项发布测试因验证副本的模块导入失败；失败路径恰好达到 260 个字符。同一源码改用短临时目录后，完整 7 项发布集成测试通过。最终第三组按完整选集在短目录重跑，前两组和第三组使用同一份源码与测试字节；没有用零散节点复测拼接结果。这次只调整测试运行目录，不宣称修复生产任意深层路径的 Python 导入限制。

## 行为一致性证据

- **106 个迁出定义**：99 个 AST 完全相同；其余 7 个只规范化所属模块、方法改函数的名字、`self` 参数及必要工厂导入后相同。保留的 **38 个 operation 定义**逐 AST 相同，包含原合法转移表。该比较不替代行为测试。
- **20 组模型界面**逐字一致：整篇、旧 strict、hybrid、chat、兼容补查分类，分别覆盖初始／已有观察与文本／原生工具。只证明这些冻结场景。
- **327 组状态载荷**解析结果逐字一致，包含固定时间的新状态、叶状态和字段异常值；并不穷尽所有合法状态。
- **10 次受控 HTTP 请求**逐字一致：Completions 与 Responses 两种协议各覆盖两轮普通对话、原生工具调用／结果和辅助摘要。比较 URL、method、headers、实际 UTF-8 body 与提交后的历史；时钟由同一固定夹具提供，没有修改生产时钟。
- AST 搜索未发现仍从原位置导入迁出符号的消费者。冷启动、进程退出、取消和恢复仍由功能测试验证。

## 最终验收

| 检查 | 结果与边界 |
| --- | --- |
| 扩大相关回归 | **3,184 passed、589 个 subtest 通过、27 skipped，0 失败**；311 个文件或节点选择分三组完整运行，未运行全仓 pytest |
| 同版本复核 | 前两组分别为 943、1,049 passed；第三组短目录整组重跑为 1,192 passed；源码及测试未修改，没有拼接不同版本的节点复测 |
| 源码冻结 | 共 1,955 个文件在运行前后字节一致：全部生产、tests、tools Python 文件，以及门禁／运行时 JSON 清单 |
| Mypy | `codey --python-version 3.11 / 3.12 / 3.13` 三个目标均通过，各检查 579 个生产源文件 |
| Pyrefly | `check codey --python-version 3.11 / 3.12 / 3.13` 均为 0 errors；仍有原来的 35 suppressed、79 warnings |
| ty | `check codey --python-version 3.11 / 3.12 / 3.13` 三个目标均通过 |
| Ruff／Git diff | `ruff check codey tests tools`、`git diff --check` 通过 |

完整选集、分组结果、尺寸记录、类型检查、比较摘要和生产文件指纹见 [机器可读报告](reports/research-responsibility-refactor-20261102.json)。最终日志为 `final-0.log/xml`、`final-1.log/xml` 与 `final-short-2.log/xml`；`validated-results.json` 关联三个完整分组，原默认目录下两项失败的 `final-2.log/xml` 继续保留。

本轮没有请求本地研究模型：这里修改的是实现归属、消费者和门禁，不改变研究提示或提交合同。以上证据用于验证接线和行为保持，不作为研究正确率提高的证据。

原始产物：`.e2e-artifacts/responsibility-refactor-20261102/`；包含基线、首次失败、基线复现、迁出 AST 清单、固定输入比较、逐轮复测、最终选择、类型检查和源码冻结记录。
