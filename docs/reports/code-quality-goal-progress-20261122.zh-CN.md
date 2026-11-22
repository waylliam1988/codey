# 代码质量治理进度（2026-11-22）

## 目标及执行约束

目标持续覆盖本仓库自有 Python、TypeScript/JavaScript 源码、工具和测试；排除依赖、第三方 QuickJS、生成的网页资源及构建产物。尚未完成全仓治理，不能据本记录推断仓库没有其他 bug。

当前状态：goal 已恢复。以下第一、二批是恢复前的历史证据；本轮从干净的 `7555d488` 继续，按文末全仓分区队列推进。历史未完成项仍保留，不将历史提交或本轮局部验收视为全仓治理完成。

- 直接使用当前工作目录，不创建、复制或切换 worktree，不覆盖用户原有修改。
- 禁止全量 pytest；每次显式指定文件，按变更依赖逐批扩大。
- bug 必须先有基于有效契约的失败复现，再用相同用例验证修复。
- 测试与生产实现都不预设正确；不以放宽断言或跳过失败换取通过。
- 深入检查文件的 Any 治理和验证进度分别记录，不把阅读、收集当成运行通过。
- 测试和类型检查串行执行，避免同时启动高内存检查。

## 初始状态

- 起始 HEAD：`45912473`。
- 用户已有修改：`docs/README.md`；未跟踪的 `codey-feral-ui-prototype.html`、`design-proposal/`、`docs/linger-codey-analysis-20261120.zh-CN.md` 以及四份 20261121 分析报告。保留不动。
- 已核对根目录 AGENTS.md、README、pyproject、测试索引、前端 package/tsconfig、类型检查说明和测试隔离配置。没有发现其他 AGENTS.md。
- Python 3.12.8；pytest/mypy/Pyrefly/ty/Node 均可定位。pytest 使用项目现有 RAM 盘隔离。

## 分批计划

1. 工作区路径、有界文件读取、终端输出裁剪和本地存储边界：建立行为基线，复现确定性问题，验证直接与调用方测试，清理该批文件动态类型。
2. 扩展至变更快照、工具执行和持久化调用方，核对共同职责和可证明的死代码。
3. 按模块推进 runtime/task、provider/protocol、browser/research、control/app、memory/ghost、前端及 tools；每批按实际依赖限定测试。
4. 对照文件清单与累计测试记录补齐未检查模块、动态类型和交互验证；全部要求有当前证据后才完成 goal。

## 第一批证据

| 项目 | 契约依据 | 修改前观察 | 状态 |
| --- | --- | --- | --- |
| 悬空符号链接读取 | `workspace.paths` 的 no-follow 读取边界和链接拒绝约束；缺失文件与已存在的链接并非同一事实 | 实际链接复现返回 None；回归报 `ValueError not raised` | 已修复，先检查链接再判断缺失 |
| Node ESM 项目帧被删除 | `prune_dependency_stack_frames` 明确要求保留用户代码证据；`project_run_command_result` 使用该结果展示终端输出 | Windows/POSIX/UNC 及百分号编码路径共 4 个子例失败 | 已修复，解析 file URL 后比较文件路径；真实 Node 24 ESM 异常也验证通过 |
| 不同盘符的依赖路径混同 | 不同驱动器上的同名目录不是同一个项目目录 | C 盘依赖帧错误获得 E 盘项目根目录的保护 | 已修复，比较时保留驱动器 |
| JSON 读取突破字节预算 | `max_bytes` 与 oversize -> StoreCorruption 的已声明契约 | 实际文件从 2 字节增长至超过 16 字节，固定旧 metadata 后两个读取器仍接受 | 已修复，打开文件后最多读取预算加一字节，按实际字节判断 |
| JSON 解析资源错误未规范化 | 宽松读取损坏状态返回 None；严格读取损坏状态抛 StoreCorruption | 5000 层数组导致两个入口泄漏 RecursionError | 已修复，解析异常进入既有损坏策略；未限制原本可成功解析的数据深度 |
| 状态目录被当成缺失文件 | 严格读取只在文件缺失时返回 None，其他 IO 错误属于损坏 | 已存在目录传入严格读取却返回 None | 已修复，真实打开错误进入 StoreCorruption |
| fdopen 失败泄漏描述符 | 打开的原始文件描述符在交接失败后仍必须由原所有者关闭 | 两条读/写错误路径返回后，真实 `os.fstat(fd)` 仍成功 | 已修复，共享二进制 fdopen 交接入口在错误时关闭 fd；回归关闭后 fstat 失败 |

读取器共用一个严格入口，宽松入口只映射 StoreCorruption；local_store 的写入 API 改为重导出 atomic_io 的唯一实现，保留公共符号。删除重复的纯转发函数及无行为的 `except OSError: raise`，合并三个重复的可选 OS 标志组合路径。未据静态搜索删除其他公共 API。

## 实际验证记录

| 命令 | 退出码 | 结果 | 覆盖及限制 |
| --- | --- | --- | --- |
| `python -m pytest -q tests/test_workspace_paths.py tests/test_text_budget.py` | 0 | 15 passed, 2 skipped | 修改前基线；Windows 没有 O_NOFOLLOW 的两个平台测试跳过；不是全量 |
| 临时 Python 脚本（实际创建悬空链接；输入 Node ESM 堆栈字符串） | 0 | 输出 `dangling symlink read: None`、`user ESM frame preserved: False` | 问题探查；尚不是回归用例通过证据 |
| 定向运行新增的悬空链接、ESM 及不同驱动器 4 个用例 | 1 | 7 failed, 1 passed（包含子例） | 修改前的目标行为失败，非环境或导入失败 |
| `python -m pytest -q tests/test_workspace_paths.py tests/test_text_budget.py` | 0 | 19 passed, 2 skipped, 4 subtests passed | 修复后；随后补充了畸形 file URL 保留用例，最终复核待记录 |
| 真实 Node ESM 临时工程脚本 | 0 | Node 预期退出 1；带空格且位于 node_modules 同名祖先下的项目帧原样保留 | 首次探针误假设 pathlib 与 Node 对 `~` 的 URL 编码相同，断言失败；修正探针依据后通过，没有据此改变生产代码 |
| 新增 JSON 增长与目录回归 | 1 | 目标行为失败 | 字节预算边界的有效文件用例本来通过，作为正向对照保留 |
| `python -m pytest -q tests/test_local_store.py::LocalStoreTests::test_readers_handle_json_parser_recursion_failure_as_corruption --tb=short` | 1 | 两个 RecursionError 子例失败 | 早期 2000 层与按 Python 递归上限推算的输入仍能解析，不能作为此 bug 证据；实际 5000 层复现才是依据 |
| `python -m pytest -q tests/test_atomic_io.py::AtomicWriteTests::test_fdopen_failure_closes_raw_descriptor --tb=short` | 1 | 两个读/写子例失败 | 真实 fd 在失败后仍开放；用例 finally 负责清理，避免复现本身泄漏 |
| `python -m pytest -q tests/test_atomic_io.py tests/test_local_store.py tests/test_atomic_write_transient_windows_sharing_violations.py tests/test_strict_store_loads.py` | 0 | 44 passed, 2 skipped, 6 subtests passed | 修复后；跳过项是 Windows 不提供 POSIX 文件模式语义 |
| `python -m mypy codey/storage/local_store.py codey/storage/atomic_io.py codey/utils/object_fields.py codey/workspace/paths.py codey/utils/text_budget.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated` | 0 | 5 个生产文件通过 | 这是额外禁止动态表达式的定向检查；普通 strict 通过不等于整个仓库无 Any |
| 路径/终端/快照调用方 11 文件批次（见下方命令） | 1 | 269 passed, 1 skipped, 107 subtests passed, 1 failed | 唯一失败为过时 CLI 会话夹具，后续单独处理 |
| `python -m pytest -q tests/test_coldstart_review_batch.py tests/test_memory_boundary.py` | 0 | 45 passed | 文档明确要求已有会话；补建会话，按当前 CLI 的 JSON 错误契约断言无 scope 的 reset 被拒绝。未修改生产 CLI |
| `python -m mypy codey --python-version 3.11`（首次） | 1 | 736 文件、9 个调用方、19 处诊断 | 返回类型由 Any 收窄后显露的数据边界问题，未关闭检查或恢复 Any |
| `python -m mypy codey --python-version 3.11`（调用方收窄后） | 0 | 736 文件通过 | 调用方仍有历史 Any，尚未完成整文件动态类型治理 |
| 10 个存储调用方测试文件批次（见下方命令，首次） | 1 | 164 passed, 4 failed | 1 个过期进程 mock；3 个依赖过期 runtime 清单的启动测试 |
| 项目完成用例的临时观测脚本 | 1 | StopIteration；前一工具结果明确为 `command not found: npm` | 旧 tool_fns mock 未拦截现在的进程入口；生产正确拒绝无通过回执的 done |
| `python -m pytest -q tests/test_project_facts.py::ProjectFactsTests::test_project_completion_records_successful_change_with_check_cwd --tb=short` | 0 | 1 passed | mock 移至外部 npm 进程边界；其他进程照常执行；原来的 command/cwd 和完成断言均保留 |
| `python -m tools.publish_runtime` | 0 | 刷新 codey/runtime.json | 原清单 830 个文件、实际 876 个文件，既有缺项和过期前端资源；此命令只生成清单，不复制 worktree |
| 存储调用方 10 文件复核（session 81063，已退出） | 1 | 167 passed, 1 failed | 唯一剩余失败由本次 file_index 解码重构提前报错、遗漏“directory metadata”类别引起；保留生产诊断类别修复，没有修改测试断言 |
| `python -m pytest -q tests/test_file_index.py` | 0 | 25 passed | 上述诊断回归修复后通过；其余 9 个文件在复核批次已通过，未为计数重复运行 |
| 最终受共享存储影响的 21 文件批次（下方命令） | 0 | 407 passed, 5 skipped, 117 subtests passed | 最新状态；5 个跳过为两个 O_NOFOLLOW、两个 POSIX 文件模式、一个 POSIX 绝对路径用例，均为 Windows 既有限制 |
| `python -m mypy codey --python-version 3.11`（最终源代码） | 0 | 736 文件通过 | 没有全量 pytest；此处是完整生产类型门禁 |
| `pyrefly check codey --python-version 3.11` | 0 | 0 errors，35 suppressed，164 warnings not shown | 修复新增的 5 处空列表隐式类型推断；未修改任何抑制或警告设置，未将这些历史项说成已消除 |
| `ty check codey --python-version 3.11` | 0 | All checks passed | 完整生产类型门禁 |
| `python -m tools.kernel_type_precision_gate` | 0 | typed contract escape checks passed | 没有放宽现有保护范围或基线 |
| 修改过的 20 个 Python 文件的 `ruff check` | 0 | All checks passed | 仅处理了新增导入顺序、测试闭包绑定和冗余 encode 参数 |
| 六个修改过的测试文件：mypy strict + `--disallow-any-explicit` | 0 | 6 文件通过 | 路径、文本预算、本地存储、原子 IO、coldstart、项目 facts；这不等于无隐式 Any |
| 同六个文件额外启用 `--disallow-any-expr --disallow-any-decorated` | 1 | 62 errors in 5 files | 完整诊断：`.e2e-artifacts/code-quality-20261122/test-any-audit.txt`。包含 unittest 上下文推断、动态属性及上游知识库/HTTP fixture 返回类型；不是 62 个已确认 bug，也不能视为 Any 治理完成 |
| `git -c core.safecrlf=false diff --check` | 0 | 无输出 | 用户已有文档和设计改动仍保留 |

路径/终端调用方批次命令：

```text
python -m pytest -q tests/test_coldstart_review_batch.py tests/test_project_map.py tests/test_project_map_split.py tests/test_workspace_scan_integrity.py tests/test_changes.py tests/test_knowledge.py tests/test_run_command_semantics.py tests/test_run_command_characterization.py tests/test_tool_runtime.py tests/test_context_source.py tests/test_shared_helpers.py
```

存储调用方批次命令：

```text
python -m pytest -q tests/test_model_preferences.py tests/test_zen_catalog_lifecycle.py tests/test_zen_catalog_free_protocols_and_scoped_identity.py tests/test_file_index.py tests/test_project_facts.py tests/test_work_checkpoint.py tests/test_work_checkpoint_flow.py tests/test_control_service_startup.py tests/test_control_runtime_ownership.py tests/test_context_compaction_transactions_and_lifecycle.py
```

上述进程均已从工具确认退出，没有等待中的测试进程。一次早期命令误写不存在的 `test_protocol_context_compaction.py`，未运行任何测试，不计覆盖。

最终共享存储影响批次命令：

```text
python -m pytest -q tests/test_workspace_paths.py tests/test_text_budget.py tests/test_atomic_io.py tests/test_local_store.py tests/test_atomic_write_transient_windows_sharing_violations.py tests/test_strict_store_loads.py tests/test_coldstart_review_batch.py tests/test_memory_boundary.py tests/test_project_map.py tests/test_project_map_split.py tests/test_workspace_scan_integrity.py tests/test_changes.py tests/test_knowledge.py tests/test_run_command_semantics.py tests/test_run_command_characterization.py tests/test_tool_runtime.py tests/test_context_source.py tests/test_shared_helpers.py tests/test_project_facts.py tests/test_work_checkpoint.py tests/test_work_checkpoint_flow.py
```

第一批累计实际运行了 28 个不同的 pytest 文件。不同批次和回归重跑的通过数不能相加当作唯一用例数；其他测试仍未覆盖。测试实际执行环境为 Windows / Python 3.12.8，类型门禁目标为 Python 3.11，不能据此宣称已经在其他平台或解释器运行通过。

## 第二批：事实缓存与检查点边界

开始前对检查点的 20 个 Python 文件逐一重算 SHA-256，均未发生额外变更。延续原目录工作，没有创建或复制 worktree。

新增回归保存在 `tests/test_fact_checkpoint_payload_boundaries.py`。预期来自事实缓存的“无效状态忽略、仅保留成功的项目内检查”约束、检查点的真实执行记录定义，以及相对文件路径不得含盘符/根路径/NUL 的文件系统约束。

| 问题 | 修改前的实际失败 | 处理 |
| --- | --- | --- |
| JSON 中 command/cwd 不是字符串 | 加载带整数 command 的缓存泄漏 AttributeError，连后续有效事实也无法加载 | 校验字段类型，忽略无效行并保留有效行 |
| facts 集合不是数组 | `commands: 42` 导致 TypeError，render 无法返回 | 按数组边界读取；不可用的缓存集合不产生事实 |
| Windows 绝对/盘符相对路径被当成项目相对路径 | `C:/outside`、`C:outside` 等可进入 cwd、文件和持久化检查点 | facts 与 checkpoint 复用 change_paths 的路径规则，目录作用域显式允许 `.` |
| 无效检查目录被改写为根目录 | `record_run(cwd='../outside', ok=True)` 写入 cwd=`.` 的成功检查 | 无效 cwd 不进入成功检查记录，保留正常成功/失败记录行为 |
| 字符串被拆成文件名数组 | `hash_unavailable_files: 'src/app.py'` 被加载成逐字符文件标记 | checkpoint 要求数组容器，错误形状不加载为有效检查点 |
| 路径包含 NUL 字节 | `safe_change_path('src/\0app.py')` 返回非空字符串 | 统一路径入口拒绝 NUL |

8 个新增用例修改前分别获得 `6 failed` 和 `2 failed`；修改后同一文件 `8 passed`。随后运行 facts/checkpoint/strict-store 的 5 文件批次，`59 passed`，退出码 0。

本批也发现一个**测试本身过时**的问题：原子写入的临时文件名已改为 UUID，清理测试仍只匹配旧 `.readonly.txt.*` 前缀。临时禁用 `_cleanup_temp_file` 后，旧测试仍然通过。现在断言目录内除原文件外不得有剩余项；相同故障注入触发 1 个断言失败且无环境错误，说明测试能够识别真实清理失效。正常清理路径另行复核，没有为测试修改生产行为。

类型与职责收敛：

- facts/checkpoint 的序列化使用明确的 JsonObject 和字段投影，移除 Any 与 `vars()` 动态结果；这两个完整模块及两个路径/容器工具文件通过额外禁止 Any 表达式、显式 Any、动态装饰器的 mypy 检查。
- JSON 数组形状统一由 `object_fields.sequence_items` 收窄；条目进入具体类型前仍逐项校验。
- 删除 `_successful_checks_from_payload` 的重复转换，保留单一检查转换入口。HEAD 中其唯一运行调用位于被重构函数；其余引用是历史 parity AST 清单，消费者仅列声明差异，不按此名称动态调用。历史 fixture 保留不改。
- 知识库批读元数据声明为 `NoteDocumentMetadata`（id/path/revision/content_hash）；API 的只读投影使用同一合同，查询次数、返回字段和顺序不变。store.py 其他旧 Any 尚未治理，不能据此宣称整文件无 Any。
- `tests/http_context.py` 为上下文、事件关闭回调和提交参数补齐类型，复用现有提交 TypedDict；提交时仍动态绑定测试上下文。该完整文件通过禁止 Any 表达式的定向 mypy 检查。
- 知识库、HTTP 夹具和路径调用方的 5 文件批次：`134 passed`，退出码 0。

本批测试命令：

```text
python -m pytest -q tests/test_fact_checkpoint_payload_boundaries.py --tb=short
python -m pytest -q tests/test_fact_checkpoint_payload_boundaries.py tests/test_project_facts.py tests/test_work_checkpoint.py tests/test_work_checkpoint_flow.py tests/test_strict_store_loads.py
python -m pytest -q tests/test_knowledge_sqlite_authority.py tests/test_knowledge.py tests/test_coldstart_review_batch.py tests/test_task_submit.py tests/test_changes.py
```

收尾检查和最新 Any 审计结果见本文件末尾；未完成事项没有被删除或标记为已完成。

## 检查范围和未完成事项

- 已深入阅读：`workspace/paths.py`、`utils/text_budget.py`、`utils/change_paths.py`、`utils/object_fields.py`、`utils/positive_int.py`、`utils/failure_output.py`、`utils/scan_report.py`、`types/json_value.py`、`storage/atomic_io.py`、`storage/local_store.py` 及路径、文本裁剪、本地存储测试。
- 已局部追踪调用关系：`workspace/changes.py`、`knowledge/changes.py`、`toolchain/runtime.py`、`workspace/map.py`；尚未对这些大型模块作完整治理。
- 已消除 `workspace.paths` 排序回调、存储 API 的显式 Any；Windows 可选标志与错误字段通过有类型的边界收窄。上表 5 个生产文件的强 Any 检查已通过。
- 第一批适配文件中，`workspace/facts.py` 和 `runs/work_checkpoint.py` 已在第二批完成整文件强 Any 检查。`providers/model_preferences.py`、`providers/zen/access.py`、`providers/zen/catalog.py`、`providers/context_ledger.py`、`workspace/file_index.py`、`control/client.py`、`app/service.py` 仍待整文件 Any 治理。
- `tests/test_project_facts.py` 的旧 mock 已修复；该文件与另外五个修改过的测试文件通过普通 strict/显式 Any 检查，但第一批额外 Any 表达式审计曾有 62 条诊断。第二批清理了知识库元数据和 HTTP 上下文来源；最新余量将在收尾时记录，整批测试脚本仍未全部清零。
- 复用 `tests/support/kernel_runtime.py` 的共享 `run_agent`，补充该入口的 AgentRequest/RunResult 注解，删除项目 facts 测试内重复的转发包装；支持文件其余未注解 helper 尚未治理，不能宣称整个支持模块无 Any。
- JSON 容器/反射隐式动态类型和其余已阅读工具文件的强类型检查仍待完成。
- `tests/conftest.py` 的框架 monkeypatch 生命周期存在未注解回调，属于已阅读但尚未治理文件，后续需要单独验证；目前没有修改其运行行为。
- 其他模块和大部分测试尚未检查/执行。没有删除仅凭无静态引用而怀疑的代码。
- 文件盘点保存在忽略目录 `.e2e-artifacts/code-quality-20261122/inventory.json`：主要源码树 2716 个源码文件、1408 个非 manual 的 pytest 文件。这只是盘点，未计为阅读或测试通过；fixture、根目录及其他源码位置还需要分类，不能据此缩小全仓目标。
- 新阅读但仅局部追踪的文件包括 provider_services、launcher/runtime、launcher/releases、tools/publish_runtime、tests/http_context、tests/release_helpers、tests/support/kernel_runtime 及进程取消入口。尚未对这些文件声明治理完成。

## 恢复 goal 后的起点（目前 paused）

1. 从 `test-any-audit.txt` 区分局部字面量推断、框架注解与真实 Any 传播；优先在知识库 row 返回类型和 HTTP fixture 的上下文返回类型处收敛，不添加 cast/ignore，也不改弱断言。
2. 完成其余存储调用方、知识库 store 与 kernel 测试支持模块的整文件类型治理，扩大实际受影响的定向测试；当前普通类型门禁通过不能代替此项。
3. 继续检查 facts/checkpoint 等缓存载荷字段校验、剩余 utils/JSON 容器边界，以及有独立契约依据的 bug 候选；先运行复现再决定生产修改。
4. 再按总计划扩展至未检查模块和测试。全仓目标尚未达到，不得将第一批结项当成 goal complete。

## 本次提交收尾

- goal 已按用户要求设为 `paused`；本次只收尾并提交已开始的改动。
- 最终收尾 pytest：`220 passed, 2 skipped, 2 subtests passed`，退出码 0。覆盖原子写入、新增载荷边界、facts/checkpoint、strict-store、知识库 SQLite 权威、coldstart、task submit、changes，以及 HTTP 提交/续接的 6 个精确 nodeid。两个跳过是现有的 POSIX 文件模式用例。
- 额外的强 Any 检查：8 个完整生产文件，加 `tests/http_context.py` 和新增载荷边界测试，共 10 文件通过；检查包含 `--disallow-any-expr --disallow-any-explicit --disallow-any-decorated`。
- 最终生产门禁：mypy `736 source files` 通过；Pyrefly `0 errors (35 suppressed, 164 warnings not shown)`；ty 通过；kernel type precision gate 通过。8 个修改过的测试/HTTP 夹具文件通过普通 strict 加禁止显式 Any 的 mypy 检查；全部 26 个改动 Python 文件的 Ruff 和 Git diff 空白检查通过。
- 六个既有测试文件的同级额外审计由第一批的 62 条降至 **40 条，分布于 5 个文件**；退出码仍为 1，不视为通过。完整输出保存在忽略目录 `test-any-audit-closeout.txt`，后续需继续处理容器字面量的上下文推断、旧框架装饰器/反射和剩余动态返回类型。没有增加忽略项或降低类型检查设置。
- 此前新增的 Pyrefly 警告来自已具体化元数据路径的多余 str 转换；该转换已随 TypedDict 合同移除，其他旧警告不在本次扩改范围。
- 截至本次收尾，累计实际执行了 **32 个不同的 pytest 文件**；其中 `test_server.py` 只执行下列 6 个用例，不能将整个 server 测试文件说成已通过。不同批次包含重跑，所有通过数均不累加为唯一用例数。
- 用户原有的 `docs/README.md` 修改、设计原型目录和其他未跟踪分析文档不纳入本次提交。

最终收尾测试命令：

```text
python -m pytest -q tests/test_atomic_io.py tests/test_fact_checkpoint_payload_boundaries.py tests/test_project_facts.py tests/test_work_checkpoint.py tests/test_work_checkpoint_flow.py tests/test_strict_store_loads.py tests/test_knowledge_sqlite_authority.py tests/test_knowledge.py tests/test_coldstart_review_batch.py tests/test_task_submit.py tests/test_changes.py tests/test_server.py::RunSnapshotTests::test_new_task_expires_stale_shell_approval tests/test_server.py::RunSnapshotTests::test_submit_task_reserves_before_browser_queue tests/test_server.py::RunSnapshotTests::test_shell_continuation_does_not_submit_after_user_stop tests/test_server.py::RunSnapshotTests::test_shell_continuation_waits_for_approval_run_slot_release tests/test_server.py::RunSnapshotTests::test_shell_continuation_uses_its_session_lane_when_other_session_runs tests/test_server.py::RunSnapshotTests::test_shell_continuation_uses_the_atomic_stop_guard
```

未运行全量 pytest、没有新增 skip/xfail，没有创建或复制 worktree，也没有将本次阶段提交标记为全仓 goal complete。

## 本轮恢复：全仓分区和固定队列

- 本轮基准：`master` / `7555d4887d5a9a73b149e098312a0917f4a5b2b0`，工作区和暂存区均干净；旧记录中的用户修改已经进入当前历史，本轮不将其当作待提交内容。
- 目标来自外部 `goal-objective.md`，包括全仓检查、分批提交和禁止复制 worktree；仍使用原总 goal，不新建或缩小目标。
- 已重新核对唯一适用 AGENTS.md、README、当前项目职责地图、runtime 架构、pyproject、MANIFEST、CI、前端 package/tsconfig、类型检查说明。版本：Python 3.12.8、mypy 1.18.2、Pyrefly 1.3.2、ty 0.0.87。
- 完整归属清单：[code-quality-scope-20261122.json](code-quality-scope-20261122.json)。盘点 **974 个自有源码文件、40 个生产/工具区域、288 个初始批次**。每个源码文件恰好归属一个批次，集合比较及去重断言已实际运行通过，退出码 0；这不是源码审查或测试通过。
- 包含 Python、源码 TS/JS/MJS、PS1、Go、UI HTML/CSS 和工具。测试/夹具另列为验证支持；排除第三方 QuickJS、发布生成的 codey/web、依赖/缓存/构建目录以及未作为运行入口的设计原型。发布工具仍在治理范围。
- 按实际包职责分区；大包再按任务执行、上下文、会话、来源、模型连接、证据投影等职责拆分。超过约 900 行的文件独立成批；其余按相关导入组织，每批不超过 8 个生产文件。初始自动分组仅用于排队，进入批次时核对语义和动态入口；必要调整必须保留全部文件并记录原因。
- JSON 清单的静态测试候选数量仅是路由提示，不表示已收集或运行；每批明确选定实际命令。区域依赖与调用方来自 AST 导入分析，仍须补查动态使用。
- 通用验收：整文件审查、契约证据、Any 审计、相关明确范围测试/类型检查、diff 审查、记录、独立本地提交。测试/类型重型命令串行，不运行全量 pytest，不创建或复制 worktree。

| 顺序 | 批次 | 范围 | 当前状态 / 验证目标 |
| --- | --- | --- | --- |
| 1 | Q001 | types/json_value、types/__init__、utils/object_fields | 进行中；严格 JSON 与 transport 的输入边界、资源预算、容器类型收窄；先跑 test_json_value_contract |
| 2 | Q002 | text_budget、failure_output、scan_report、positive_int、utils/__init__ | 未开始；输出边界和整数转换；完成旧记录遗留的整文件 Any 项 |
| 3 | Q003 | citation_scanner、references、refs | 未开始；有界引用及调用方；不扩大为全仓引用重构 |
| 4 | Q004 | atomic_io、local_store、workspace/paths、change_paths、storage/__init__ | 未开始；核对历史成果和剩余测试 Any；无影响不机械重跑历史批次 |
| 后续 | Q005～Q288 | 启动、agents/app、automation、codemode、completion/control/durable、knowledge/memory、operations/policies/protocols/providers、research/runtime、workspace、frontend、tools | 全部未开始；逐批入口核对后执行，详见清单文件 |

历史未完成类型事项归入所属区域：model_preferences、zen/access/catalog、context_ledger、file_index、control/client、app/service、knowledge/store；历史测试 Any 40 条及 conftest/kernel_runtime 作为关联验证债务保留，未计为已完成。

本轮进度：**0 / 288 批完成，0 / 974 个生产文件验收完成**。已阅读 Q001 三文件及其旧测试；为后续路由阅读的 failure_output、scan_report、refs、references 和 positive_int 未验收，类型义务仍归 Q002/Q003。

### Q001 已完成：JSON 和动态容器边界

- 分区清单提交：`fbf80021`。Q001 实际深入检查全部三个生产文件及 `tests/test_json_value_contract.py`；没有证据充分的业务 bug、可证明死代码或重复职责，不制造相应修改。
- 修改 JSON 编码边界：Enum 值、反射字段值以 object 接入原递归校验；Mapping/Sequence 元素显式声明为待校验对象；dataclass 字段仅投影实际使用的 name 协议；排序结果仍沿用原 sorted，之后逐项递归校验。没有新增 cast/ignore，也没有取消校验。保留字段名预算检查先于属性读取的执行顺序。
- `_CLASS_TYPE` 是原 builtin type 对象的注解别名，用于避开 type 元类未参数化产生的 Any，不改变 class 检测。原 is_dataclass 判断顺序保持不变。
- 补齐整个 JSON 契约测试的参数/返回/递归容器类型。增加字符串数组的正反向边界：元组/标量/错误元素拒绝，空数组及 Unicode 保持，不复用输入数组。未修改既有断言或预期。

| 实际命令（均仓库根目录） | 退出码 | 结果与限制 |
| --- | --- | --- |
| `python -m pytest -q tests/test_json_value_contract.py --tb=short` | 0 | 修改前行为基线 12 passed，无 skip；不是 bug 的失败复现 |
| `python -m mypy codey/types/json_value.py codey/types/__init__.py codey/utils/object_fields.py tests/test_json_value_contract.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated` | 1 → 0 | 基线 25 条；中间依次 1 条、2 条、0 条；恢复原判断顺序后的最终检查仍 4 文件通过。错误均为类型诊断，不冒充行为 bug |
| `python -m pytest -q tests/test_json_value_contract.py tests/test_json_codec.py tests/test_fact_checkpoint_payload_boundaries.py --tb=short` | 0 | 修改后 29 passed，无 skip；JSON 直接合同、协议 codec 和已治理缓存边界 |
| `pyrefly check codey/types/json_value.py codey/types/__init__.py codey/utils/object_fields.py tests/test_json_value_contract.py --python-version 3.11` | 0 | 0 errors |
| `ty check codey/types/json_value.py codey/types/__init__.py codey/utils/object_fields.py tests/test_json_value_contract.py --python-version 3.11` | 0 | All checks passed |
| `python -m ruff check codey/types/json_value.py codey/types/__init__.py codey/utils/object_fields.py tests/test_json_value_contract.py` | 0 | All checks passed |
| `python -m tools.kernel_type_precision_gate` | 0 | 精度/逃逸门禁通过，未更改保护范围 |
| `python -m mypy codey --python-version 3.11` | 0 | 736 生产文件通过 |
| `pyrefly check codey --python-version 3.11` | 0 | 0 errors，仍有既存 35 suppressed / 164 warnings；不声称已消除 |
| `ty check codey --python-version 3.11` | 0 | All checks passed |
| `git -c core.safecrlf=false diff --check` | 0 | 通过；无无关格式化 |

当前：**1 / 288 批完成，3 / 974 个生产文件验收完成**。本轮实际执行 3 个不同 pytest 文件；历史的 32 文件记录仍单独保留，不能直接相加。未覆盖其他生产区域和大多数测试。下一批 Q002（5 个输出/数值工具文件），不反复重新打开 Q001。

### Q002：终端输出和数值小工具

- Q001 提交：`055abab8`。本批完整审查 text_budget、failure_output、scan_report、positive_int、utils/__init__ 五文件，以及完整 text_budget 测试。正整数转换与 refs 的整数转换对 bool/失败值等语义不同，没有为了相似代码强行合并；现有公开工具入口未据“无引用”删除。
- **确定性 bug**：Node 帧位置从最后一个 `(` 或空格切分，会把路径内括号/空格误当成函数边界。有效契约来自 `prune_dependency_stack_frames` 的明确说明“preserving user-code evidence”，以及项目根目录与目录名无关的身份不变量；调用方需要原终端用户代码位置来定位错误。
- 实际 Node 探针在临时 `quality-(workspace)-*/node_modules/project/entry.cjs` 执行 `throw new Error(...)`，Node 如期退出 1；修改前输出真实 `at Object.<anonymous> (...entry.cjs:1:7)` 被过滤，断言失败，探针退出 1。修改后相同脚本/关键断言通过，退出 0，项目帧保留。只创建单文件临时工程，没有复制 worktree。
- 新增的两个回归入口在生产修改前出现 **5 个子例失败、2 个外层用例通过**，整体退出 1；失败均为误删项目帧或遗漏依赖折叠，非环境/导入失败。之后修改位置识别，优先识别绝对/file URL 位置起点，保留虚拟/相对帧原行为。相同入口修复后 **2 passed、5 subtests passed**。
- 随后扩展 Windows/POSIX/UNC、ESM 编码路径、函数别名含括号、async 和裸帧含空格的边界。没有新增 skip/xfail，没有改弱原断言。

| 实际命令（仓库根目录） | 退出码 | 结果及范围 |
| --- | --- | --- |
| `python -m pytest -q tests/test_text_budget.py tests/test_failure_result_presentation.py tests/test_automatic_edit_assistance.py tests/test_fail_closed_numeric_guards.py tests/test_shared_helpers.py --tb=short` | 0 | 修改前 63 passed、9 subtests passed；修复后首次 65 passed、14 subtests passed |
| `python -m pytest -q tests/test_text_budget.py::TextBudgetTests::test_prune_keeps_node_project_frames_below_parenthesized_ancestor tests/test_text_budget.py::TextBudgetTests::test_prune_folds_node_dependency_frames_with_parenthesized_paths --tb=short` | 1 → 0 | 修改前 5 failed、2 passed；同输入修复后 2 passed、5 subtests passed |
| `python -m pytest -q tests/test_text_budget.py --tb=short` | 0 | 扩展边界后的最终 14 passed、14 subtests passed；其他四个直接相关文件未因仅新增测试而机械重跑 |
| `python -m pytest -q tests/test_run_command_semantics.py tests/test_run_command_characterization.py tests/test_tool_runtime.py tests/test_bounded_capture_and_context.py --tb=short` | 1 | 140 passed、1 skipped、100 subtests passed、2 failed；跳过为既有 POSIX 平台用例。两个失败见下，不写作全绿 |
| `python -m mypy codey/utils/text_budget.py codey/utils/failure_output.py codey/utils/scan_report.py codey/utils/positive_int.py codey/utils/__init__.py tests/test_text_budget.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated` | 0 | 修改前及最终六个完整文件通过，没有新增 Any，也不声称其调用方已全部无 Any |
| `pyrefly check codey/utils/text_budget.py codey/utils/failure_output.py codey/utils/scan_report.py codey/utils/positive_int.py codey/utils/__init__.py tests/test_text_budget.py --python-version 3.11` | 0 | 最终 0 errors |
| `ty check codey/utils/text_budget.py codey/utils/failure_output.py codey/utils/scan_report.py codey/utils/positive_int.py codey/utils/__init__.py tests/test_text_budget.py --python-version 3.11` | 0 | 最终通过 |
| `python -m ruff check codey/utils/text_budget.py codey/utils/failure_output.py codey/utils/scan_report.py codey/utils/positive_int.py codey/utils/__init__.py tests/test_text_budget.py` | 0 | 通过 |

**已有测试失败的定位和处置**：`tests/test_bounded_capture_and_context.py` 的 `WaitProcessCleanupOwnershipTests::test_close_owned_pipes_skips_live_reader`、`::test_external_holder_does_not_pin_close` 使用 SimpleNamespace 替代进程，但缺少当前 `_close_owned_pipes` 诊断使用的 `args`（其中一个也无 pid）。从 `055abab8` 通过 `git show` 读取修改前 text_budget，在独立 Python 进程内编译后替换 `sys.modules['codey.utils.text_budget']`，再用 `pytest.main` 运行上述两个精确 nodeid，实际仍为 **2 failed、退出 1**，同样因缺失 args。cancellation 没有依赖 text_budget，也没有本轮修改。按已有进程 mock 不满足契约的问题归入 cancellation 所属批次，不改生产日志或本批修复来迁就它们；后续修 fixture 前仍需核对 Process 契约，并完成该大型测试文件的 Any 治理。

本批只为根因定位阅读 cancellation 的局部清理函数和上述夹具，未把整文件算作完成。已有失败不由本批引入，有修改前复核证据；允许独立提交本批已验证的修复。其余生产区域和相关测试仍在队列。

Q002 提交前最终门禁：`python -m mypy codey --python-version 3.11`（736 文件）、`pyrefly check codey --python-version 3.11`（0 errors，原有 35 suppressed/164 warnings）、`ty check codey --python-version 3.11`、`python -m tools.kernel_type_precision_gate`、`git -c core.safecrlf=false diff --check` 均退出 0。本轮累计实际运行 **12 个不同 pytest 文件**（部分包含重复运行，不累加为唯一用例数）。当前 **2 / 288 批完成，8 / 974 个生产文件完成**；Q189 保留两项已有夹具失败。下一批 Q003：引用扫描和 refs，先建立行为及 Any 基线。

### Q003 进行中：有界引用扫描及 refs

- Q002 提交：`aeaba3e2`。本批三个完整生产文件已阅读，先处理 refs 的独立 bug，再收窄 citation_scanner 和 references 的动态类型。未把读取算作完成。
- **裁剪预算 bug 的独立依据**：refs 原模块声明其职责为“bounded text/ref primitives”和 clipped identifiers；limit 是调用方提供的字符上限，省略标记也属于输出。可论证不变量为输出长度不超过非负预算，不能用省略标记反而突破上限。该依据不来自另一裁剪实现或测试断言。
- 触发：`clip('abcdef', 1)`、`clip('abcdef', 2)`（Unicode 同样）；原结果均为 `...`，长度 3，派生 identifier 也突破预算。新增 `test_ref_text_budget.py` 修改前实际 **4 failed、11 passed、退出 1**，均为长度超限；随后按预算截短标记，保留正常预算、规范化及短文本不变。相同文件修复后 **15 passed、退出 0**，含 -1/0/1/2/3/4/8 的边界。
- refs 全文件以 `Iterable[object]` 收窄动态迭代元素后再规范化/去重；没有新增 cast/ignore。未合并 clip_tail：它与 refs 对 None/False/0 的规范化、marker 和模块依赖约束不同。

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_citation_scanner.py tests/test_fail_closed_runtime_sweep.py tests/test_proof_ref_parity.py tests/test_research_identity.py tests/test_search_scan_split.py --tb=short` | 0 | 修改前 82 passed、37 subtests passed |
| `python -m mypy codey/utils/citation_scanner.py codey/utils/references.py codey/utils/refs.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 | 基线 27 条、2 文件：正则 Match 的下标/findall/回调和可选 Path getattr。独立 cache-dir 避免强 Any 检查使全树正常检查反复失效；不更改检查强度 |
| `python -m pytest -q tests/test_ref_text_budget.py --tb=short` | 1 → 0 | 修改前 4 failed、11 passed；修复后 15 passed |
| `python -m mypy codey/utils/refs.py tests/test_ref_text_budget.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 两个完整文件通过 |
| `python -m pytest -q tests/test_fail_closed_runtime_sweep.py tests/test_proof_ref_parity.py tests/test_research_identity.py tests/test_persisted_receipt_exit_code_roundtrip.py --tb=short` | 0 | 64 passed、37 subtests passed，无 skip |
| `pyrefly check codey/utils/refs.py tests/test_ref_text_budget.py --python-version 3.11` | 0 | 0 errors |
| `ty check codey/utils/refs.py tests/test_ref_text_budget.py --python-version 3.11` | 0 | 通过 |
| `python -m ruff check codey/utils/refs.py tests/test_ref_text_budget.py` | 0 | 通过 |

本轮累计实际执行 **19 个不同 pytest 文件**。当前完整批次仍为 **2 / 288**，另 Q003 部分完成；生产文件验收 **9 / 974**。Q003 剩余两个生产文件及深入检查的 citation_scanner 测试仍需类型审计和行为验证，不能宣称整批已完成。

Q003 第一个独立提交前的全树门禁实际失败：mypy（`python -m mypy codey --python-version 3.11`，退出 1）报告 tool_tasks 引用已改名的 restore_text_wait_snapshot；Pyrefly（同 Q002 全树命令，退出 1）报告 browser_followups 列表类型和 kernel_execution 缺失 require_json_string；ty（同 Q002 全树命令，退出 1）报告后者。随后 Git 检查证实出现了本轮之外的并行改动：automation/browser_runtime/session/snapshot、codemode/host、operations/browser_followups/waits/kernel_execution/task_execution/tool_tasks、toolchain/tool_spec、旧浏览器测试以及新增 browser_control_continuation 测试。这些文件在检查期间仍发生更新，部分诊断位置已被其所有者修正，但没有据此宣称门禁通过。

以上不是 refs 的返回类型或实现引起的失败；本批没有修改上述浏览器文件，也不覆盖或夹带提交它们。refs 定向三种类型检查、全部直接/调用方验证通过，允许先独立提交预算修复。全树最新成功结果仍是 Q002 时刻，本次失败保留，后续须在现有外部修改上重新核验。精度门禁及 diff 空白检查本次均退出 0。后续继续处理没有文件冲突的 citation_scanner/references。

### Q003 第二步：正则和 Path 类型边界

- refs 独立修复提交：`6b12a394`。
- citation_scanner 使用有注解的正则替换/排序回调、明确 capture 字段类型，以 finditer 的有类型 Match 代替 findall 的动态元组。可选 page group 映射为空串仍保留，顺序/偏移/异常和遮罩规则不变。字段类型来自固定正则的 capture 约束；未添加不受约束的类型断言或忽略。
- references 的可选 getattr 值先声明为 object 并检查 callable，再通过原 Path 接口调用 lstat/resolve。缺失方法的 test double 分支保留；没有额外扫描预算、跟随链接或改变输出规则。
- citation_scanner 整个测试文件的预期容器加具体注解，值与断言不变；空集合明确为 set[str]。test_shared_helpers 也已整文件阅读并通过强 Any 检查，未作不必要修改。
- 原三生产文件基线 27 条 Any 诊断；两关联测试初审 7 条，均在 citation_scanner 测试。迭代中 18 条 → 3 条 → 0 条，最后补齐 capture 局部值和新裁剪测试一起检查，6 完整文件通过。诊断减少是类型证据，不冒充 bug 修复数量。

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m mypy tests/test_citation_scanner.py tests/test_shared_helpers.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 | 修改前 7 errors in 1 file |
| `python -m pytest -q tests/test_citation_scanner.py tests/test_search_scan_split.py tests/test_tool_runtime.py tests/test_shared_helpers.py --tb=short` | 0 | 115 passed、23 subtests passed |
| `python -m pytest -q tests/test_chat_programmatic_citations.py tests/test_research_report_tail.py tests/test_structured_research_report.py --tb=short` | 1 | 67 passed、3 failed，53.79s；完整失败未隐藏，见下 |
| 用 6b12a394 的 citation_scanner 在进程内覆盖当前模块，再执行上述三个失败精确 nodeid | 1 | 同样 3 failed，48.05s；同关键断言失败，排除本次 citation 类型修改所致，不复制 worktree |
| `python -m pytest -q tests/test_citation_scanner.py tests/test_knowledge.py::KnowledgeStoreTests::test_brief_builder_uses_shared_citation_scanner_shapes --tb=short` | 0 | 最终 capture 注解补齐后 7 passed；knowledge 只执行该精确用例，不写为全文件通过 |
| `python -m mypy codey/utils/citation_scanner.py codey/utils/references.py codey/utils/refs.py tests/test_citation_scanner.py tests/test_ref_text_budget.py tests/test_shared_helpers.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 6 完整文件通过 |
| `pyrefly check codey/utils/citation_scanner.py codey/utils/references.py codey/utils/refs.py tests/test_citation_scanner.py tests/test_ref_text_budget.py tests/test_shared_helpers.py --python-version 3.11` | 0 | 0 errors |
| `ty check codey/utils/citation_scanner.py codey/utils/references.py codey/utils/refs.py tests/test_citation_scanner.py tests/test_ref_text_budget.py tests/test_shared_helpers.py --python-version 3.11` | 0 | 通过 |
| `python -m ruff check codey/utils/citation_scanner.py codey/utils/references.py codey/utils/refs.py tests/test_citation_scanner.py tests/test_ref_text_budget.py tests/test_shared_helpers.py` | 0 | 通过 |

三个集成失败的明确处置：`test_research_report_tail.py::test_production_rejects_unfinished_report_then_publishes_repair_without_reacquisition[False/True]` 期望 browser_read 次数 2，实际 4；`test_structured_research_report.py::test_native_completion_guidance_matches_frozen_phase_schema[research]` 期望不暴露 done，实际暴露。已在修改前 citation 实现下重现，故不是本次类型回归；生产 bug、测试过时或外部浏览器修改的影响仍待按有效契约区分。未改变这些测试或预期；已路由到研究完成/内核准备所属批次，保留为未解决事项。精确复核命令为进程内加载 `git show 6b12a394:codey/utils/citation_scanner.py` 后 `pytest.main(['-q', <上述三精确nodeid>, '--tb=short'])`。

当前累计实际执行 **23 个不同 pytest 文件**，其中 knowledge 仅执行一个 nodeid；未运行全量 pytest。仍保留 Q189 两项进程夹具失败和上述三项研究集成失败，不能报告全绿。

Q003 最新全树核验（737 个生产 Python 文件，三个 checker 串行）：mypy 退出 1，外部修改的 `research/answer_contract.py:150` 有两个 TypedDict.update 关键字诊断；Pyrefly 退出 1，外部新增 `operations/browser_result_evidence.py:47` 的空容器产生隐式 Any；ty 退出 0。完整命令、日志和源码 SHA-256 快照保存在 `.e2e-artifacts/code-quality-20261122/q003-full-types.json` 及相邻三个 log。核验前后全部生产 Python 源码哈希一致，`changed_during_checks=[]`，因此这次失败是可复核的当前状态，而非将瞬时输出当成通过。上述源码均不属于本批修改，未覆盖/提交。全树失败仍待其所属区域处理，不能以本批定向成功替代全树成功。精度门禁、本批 diff 空白检查均退出 0。

范围更新：检测到外部新增的 `codey/operations/browser_result_evidence.py`，将其明确加入 Q084，待审查、待 Any 治理；该批从 8 扩为 9 个紧密相关的 browser 完成/后续证据文件，仍符合用户每批约 3～10 文件的范围。总范围由 974 更新为 **975 个生产/工具源码文件**，批次数保持 288，没有删除旧范围或掩盖未完成项。去重与归属断言已运行通过，当前 codey 的全部 737 个 Python 文件都有归属。外部新增测试也列入验证支持，不计为已执行。

Q003 自有三个生产文件和深入检查的关联测试完成 Any/行为验证；独立于已定位的跨区域失败，可以结项并提交类型收敛。当前 **3 / 288 批完成、11 / 975 个生产文件验收完成**，没有删除或强行合并不能证明的代码。下一批 Q004：原子存储、路径及其历史测试 Any 债务。总 goal 继续 active，所有剩余区域、五个测试失败和当前全树类型错误仍在记录/队列中。
