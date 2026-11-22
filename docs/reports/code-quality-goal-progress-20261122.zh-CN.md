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

### Q004 已完成：存储/路径及历史测试类型债务

- Q003 类型提交：`3d76583c`。完整检查 atomic_io、local_store、storage/__init__、workspace/paths、utils/change_paths 五个生产文件，以及 atomic_io/local_store/workspace_paths 三个测试文件。历史 bug 修复仍通过；没有新增准入充分的 bug、可证明死代码或语义相同且值得合并的实现，不额外修改生产代码。
- 生产文件强 Any 检查通过。测试基线有 18 条 Any 诊断，集中于 unittest 的容器上下文、os.fdopen 重载、既有 skipIf 泛型、动态 OS 标志和 JSON 解析结果；现已清零，整个八文件范围通过额外禁止 Any 表达式/显式 Any/动态装饰器的 mypy 检查。
- 测试预期值和原断言不变；stdlib 二进制 fdopen 绑定精确签名，JSON 未验证结果保留为 object 后由原完整等式断言验证。O_NOFOLLOW 先检查整数类型再做原位运算。
- 原两个 skipIf 使用相同函数、相同条件、相同理由，只将返回装饰器保存为具体测试签名的变量；没有增加 skip。pytest 原有 4 个平台跳过的数量、用例和理由均保持；native unittest 也实际复核，仍有原 2 个 POSIX 模式跳过。

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py tests/test_atomic_write_transient_windows_sharing_violations.py tests/test_strict_store_loads.py --tb=short` | 0 | 修改前 53 passed、4 skipped、6 subtests passed |
| 同一 pytest 命令增加 `-rs` | 0 | 修改后仍 53 passed、4 skipped、6 subtests passed；两个 Windows 无 O_NOFOLLOW、两个 POSIX 权限位用例 |
| `python -m mypy codey/storage/atomic_io.py codey/storage/local_store.py codey/storage/__init__.py codey/workspace/paths.py codey/utils/change_paths.py tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 → 0 | 修改前 18 errors in 3 files；修改后八个完整文件通过 |
| `pyrefly check codey/storage/atomic_io.py codey/storage/local_store.py codey/storage/__init__.py codey/workspace/paths.py codey/utils/change_paths.py tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py --python-version 3.11` | 0 | 0 errors，2 warnings not shown；不将其说成无警告 |
| 同一 Pyrefly 命令增加 `--output-format json` | 0 | errors 数组为空，仍报告两个未展示 warning |
| 同一 Pyrefly 命令增加 `--min-severity warn` | 1 | 实际展示两个 missing-attribute-patch-target：原有条件 mock 的 os.fchmod 在 Windows 不存在。代码先 hasattr 再添加 mock，实际测试不进入该分支，属于平台静态目标诊断；未增加 ignore 或改变 mock 目标。展示 warning 的检查退出 1 如实保留 |
| `ty check codey/storage/atomic_io.py codey/storage/local_store.py codey/storage/__init__.py codey/workspace/paths.py codey/utils/change_paths.py tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py --python-version 3.11` | 0 | 通过 |
| `python -m ruff check codey/storage/atomic_io.py codey/storage/local_store.py codey/storage/__init__.py codey/workspace/paths.py codey/utils/change_paths.py tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py` | 0 | 通过 |
| `python -m unittest -q tests.test_atomic_io` | 0 | Ran 23 tests，OK（skipped=2）；不是 23 个全部执行通过 |
| `git -c core.safecrlf=false diff --check -- tests/test_atomic_io.py tests/test_local_store.py tests/test_workspace_paths.py` | 0 | 通过 |

本批只有测试类型改动，没有使已通过的生产调用方测试失效，不机械扩大/重复此前验证。全树最新结果仍为 Q003 的外部源码错误，未凭本批定向通过宣称修复它们。当前 **4 / 288 批完成，16 / 975 个生产文件验收完成**；本轮累计执行 **28 个不同 pytest 文件**，knowledge 仍只算一个精确用例的覆盖。历史六测试文件的 Any 债务中，本批三个文件已完成；coldstart/project_facts 等其余文件及 conftest/kernel_runtime 义务仍保留。

下一批固定为 Q005、Q006 的顶层包版本和启动/环境入口，再按清单进入 agents/app。不重新打开已完成基础工具批次，除非有新增复现或后续依赖变化证据；Q189 的两项进程夹具问题、Q096/Q106/Q161 的三项研究失败及外部全树类型错误继续留在所属队列。总 goal 未完成，继续 active。

## 续轮：顶层入口与 agents

- 上一 goal turn 有实际修复、验证与六个提交，属于 progress；本轮从 `0b999da8` 核对记录继续。暂存区为空，浏览器/研究等外部改动仍在，保留不动。没有创建或复制 worktree。
- Q004 提交为 `0b999da8`。

### Q005 / Q006 / Q007 已完成：版本、启动和包入口

- 完整检查 codey/__init__、__main__、env_names 和 agents/__init__，以及整个 test_env_names。__version__ 是 pyproject 动态版本读取入口；__main__ 是 README 的 python -m 入口；两个包 init 对当前 setuptools namespaces=false 的打包发现有必要。环境常量由公开导出和浏览器/provider/HTTP/shell/工具调用使用，不因文件短或导出看似未用而删除。
- 生产代码无需修改；未发现准入充分的 bug 或同语义重复实现。Q005/Q007 为“检查完成，无需修改”，不创建空提交。CLI 实现只定位参数分发，未将整个 app/cli.py 算作已深入治理。
- 环境测试原有 6 条类型诊断：反射 Any、空/排序容器上下文，以及严格 mypy 禁止隐式重导出导致的属性诊断。测试只检查导入常量身份，不代表消费者模块承诺公开导出，故不为测试增加生产导出。反射边界以 ModuleType、object 和运行时字符串检查收窄，继续使用原 assertEqual/assertIs；已消除整文件 Any，没有改弱预期。

| 实际命令（仓库根目录） | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_env_names.py tests/test_http_plumbing.py tests/test_cli.py --tb=short` | 0 | 修改前 21 passed、14 subtests passed，无 skip |
| `python -m codey --help` | 0 | 实际 CLI 帮助正常，不启动服务/浏览器 |
| `python -m mypy codey/__init__.py codey/__main__.py codey/env_names.py tests/test_env_names.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 → 0 | 基线 6 条，修整后中间剩 1 条，最终通过 |
| `python -m pytest -q tests/test_env_names.py --tb=short` | 0 | 最终 4 passed、14 subtests passed，原断言不变；未机械重跑未改动的 CLI/HTTP 测试 |
| `python -c "import codey; import codey.agents; print(codey.__version__); print('package imports passed')"` | 0 | 0.5.11；两个包实际导入成功 |
| `python -m mypy codey/__init__.py codey/__main__.py codey/env_names.py codey/agents/__init__.py tests/test_env_names.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 五个完整文件强 Any 检查通过 |
| `pyrefly check codey/__init__.py codey/__main__.py codey/env_names.py codey/agents/__init__.py tests/test_env_names.py --python-version 3.11` | 0 | 0 errors |
| `ty check codey/__init__.py codey/__main__.py codey/env_names.py codey/agents/__init__.py tests/test_env_names.py --python-version 3.11` | 0 | 通过 |
| `python -m ruff check codey/__init__.py codey/__main__.py codey/env_names.py codey/agents/__init__.py tests/test_env_names.py` | 0 | 通过 |

本轮生产代码未改，不重复全树检查来掩盖上轮未解决错误；历史五项跨区域测试失败和外部全树类型错误仍留在队列。累计实际覆盖 **31 个不同 pytest 文件**，仍包含 knowledge 的单 nodeid，不能将部分执行写为整个文件通过。

进入 Q008 时，按模块 docstring/声明核对发现原“shared contracts”自动分组包含四种独立职责，现拆为 Q008（请求/工具分发，4 文件）、Q008B（状态与循环守卫，2 文件）、Q008C（对话 handoff，1 文件）、Q008D（只读多模型顾问，1 文件）。两个单文件各有独立的完整状态/顾问契约和专用测试，适合独立验收。原 8 文件全部保留，其他批次 ID 不变；总批数 **288 → 291**，不是缩小源码范围或制造完成率。清单归属/去重断言通过。

当前 **7 / 291 批完成，20 / 975 个生产文件验收完成**。下一主要批次 Q008；其他三个子批次未开始，未把 AST 声明盘点算作深入检查。总 goal 继续 active。

### Q008 进行中：请求、工具契约与只读审计

- Q005～Q007 提交：`26c7cbf4`。完整读取本批 request、tools、tool_execution、project_audit_tools 四生产文件；相关深入测试为 agent_tools、project_audit_tools_ownership、audit_resolve_paths、consensus_audit_split 及新增 root_paths。
- 本轮之外的浏览器/研究改动已由其所有者提交为 `1292e668`；没有混入本批暂存/提交。后续全树门禁基于此提交和本批工作区。

**bug 准入证据**：审计明确拒绝项目内符号链接，根目录的展开形式不应改变该拒绝策略；既有 read/list 已 expanduser，搜索的 safe_join 也接受展开根目录，但原搜索/引用前置检查在未展开根目录上查链接。真实探针在用户 home 下独占临时目录创建 actual.py、alias.py 符号链接，传入 `~/<临时目录>`，实际 search 返回 actual.py 的 `needle = 1` 并标记成功，违反拒绝策略，断言失败、退出 1。新增 root_paths 测试在生产修改前实际 **2 failed、2 passed**（展开根目录的对照本来通过），均是目标行为失败。为了不要求 CI 拥有创建链接的权限，自动回归只在 Path 的文件系统边界模拟 expanduser/resolve/is_symlink，其余扫描和文件内容真实执行，没有新增 skip。

修复：原共享 `_audit_search_resolve_start` 在检查子路径链接前展开/解析根目录；references 迁移到同一解析入口，并对后续扫描使用展开根目录，删除重复的拒绝/解析/存在检查。相同回归修复后 4 passed；真实符号链接探针的 search、references 都拒绝、退出 0。随后补充普通文件 search/references 在 tilde 根目录的正向边界，最终该文件 6 passed。

**重复职责收敛**：公开 visible_entries、execute_read_only_call 原来各只转发一个私有同签名实现。完整模块无注册/反射入口，__all__ 和 operations 消费者只公开/调用原公开名；配置、CLI、打包没有引用私有名。全树搜索另外只找到 ownership 测试对 consensus 禁止旧定义的字符串，以及 kernel_parity 冻结 JSON 的 inventory.ast_surface 元数据，非 cases/执行入口。保留该历史 fixture，不据其元数据删除测试。已将两个实现直接归入原公开入口、更新内部调用并删除两层转发；公开名称、参数、输出和错误路径保留。合并前审计基线通过，合并后 26 个审计用例通过。

**类型治理**：read/search/write/run 端口使用明确 Protocol，带运行上下文的回调使用六参数 Callable；可选参数以 TypedDict/Unpack 表达，校验/投影保留键缺省语义。ReplayDecision、文件迭代器和工具返回值明确，删除 Any/cast。字段依据是现有 toolchain/definition 的冻结 schema：offset/limit/context 为 integer，开关为 boolean，include 为 string，exclude 为 string 数组；非法字段类型走既有 ToolOutcome 错误通道，不修改底层工具自身分页归一化。测试验证省略字段与显式 False/0/空值保留、九种非法类型拒绝。旧测试 root=None 改为 Path('.')，独立依据为执行入口的 Path 合同；原 command/cwd/profile/phase 断言不变，未让生产接受无效 fixture。

| 实际命令 | 退出码 | 结果与范围 |
| --- | --- | --- |
| `python -m pytest -q tests/test_agent_tools.py tests/test_agent_request_no_dead_fields.py tests/test_project_audit_tools_ownership.py tests/test_project_audit_advisor_kernel.py --tb=short` | 0 | 修改前 18 passed |
| `python -m mypy codey/agents/request.py codey/agents/tools.py codey/agents/tool_execution.py codey/agents/project_audit_tools.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 | 修改前 22 条、3 文件；request 本来通过 |
| `python -m pytest -q tests/test_project_audit_root_paths.py --tb=short` | 1 → 0 | 修改前 2 failed/2 passed；修复后 4 passed；扩展边界后最终 6 passed |
| `python -m pytest -q tests/test_project_audit_root_paths.py tests/test_agent_tools.py tests/test_agent_request_no_dead_fields.py tests/test_project_audit_tools_ownership.py tests/test_project_audit_advisor_kernel.py tests/test_audit_resolve_paths.py tests/test_consensus_audit_split.py --tb=short` | 0 | 第一次 31 passed；补充边界后 33 passed、9 subtests passed |
| `python -m pytest -q tests/test_agent_effect_sandwich.py tests/test_tool_replay_policy.py tests/test_read_before_edit_ab.py tests/test_tool_runtime.py --tb=short` | 0 | 调用方 125 passed、33 subtests passed，无 skip |
| `python -m pytest -q tests/test_project_audit_root_paths.py tests/test_project_audit_tools_ownership.py tests/test_project_audit_advisor_kernel.py tests/test_audit_resolve_paths.py tests/test_consensus_audit_split.py --tb=short` | 0 | 转发合并后 26 passed |
| `python -m pytest -q tests/test_agent_tools.py --tb=short` | 0 | 最终显式 False/0/空值边界后 6 passed、9 subtests passed |
| `python -m mypy codey/agents/request.py codey/agents/tools.py codey/agents/tool_execution.py codey/agents/project_audit_tools.py tests/test_project_audit_root_paths.py tests/test_agent_tools.py tests/test_project_audit_tools_ownership.py tests/test_audit_resolve_paths.py tests/test_consensus_audit_split.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 九个完整文件通过；中间诊断包括 TypedDict 可选字段合并 1 条、关联旧测试 17 条及一处测试字面量推断，均实际修复，未忽略 |
| 同九路径 `pyrefly check ... --python-version 3.11` / `ty check ... --python-version 3.11` | 0 / 0 | 两者通过 |
| 同九路径 `python -m ruff check ...` | 1 → 0 | 仅新增导入间距 I001，修复后通过 |
| `python -m tools.kernel_type_precision_gate` | 0 | 精度门禁通过 |
| `python -m mypy codey --python-version 3.11` | 0 | 最终 737 文件通过 |
| `pyrefly check codey --python-version 3.11` | 0 | 0 errors，35 suppressed / 165 warnings 仍未消除 |
| `ty check codey --python-version 3.11` | 0 | 通过 |
| `python -m tools.source_size_inventory` | 0 | 仅实际行数盘点，不是源码审查/测试结果；第三方和生成文件仍按治理范围排除 |

最终全树类型命令串行执行，737 个生产源码检查前后 SHA-256 一致，`changed_during_checks=[]`；日志/命令/快照在 `.e2e-artifacts/code-quality-20261122/q008-final-full-types.json` 和相邻日志。上轮外部全树类型失败已被当前实际成功结果替代；五个跨区域测试失败没有据此说成解决。

依赖追踪的额外整文件 Any 审计：`python -m mypy codey/toolchain/execution_options.py codey/runtime/effects/replay_policy.py codey/toolchain/search_page.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` 退出 1；前两个无诊断，search_page:70 有一处 json.dumps 字面量的隐式 dict[Any, Any]，保留到其所属批次，未计为治理完成。仅追踪的 project_completion_context:247 仍有回调 Any 声明，已归属原批次，不无限扩展当前区域。

当前额外运行项目机器契约门禁 `python -m tools.machine_contract_gate`：已核对其固定 191 个明确测试文件，完整选择和门禁源码哈希保存在 `.e2e-artifacts/code-quality-20261122/q008-machine-selection.json`；不是全仓收集。该次门禁在约 25% 处出现慢用例和 3 个早期失败；采样完成后明确终止已核对命令的 PID 36624，session 49562 最终退出 1，未取得最终汇总，不计为通过或完整覆盖。现有唯一 pytest 文件实际覆盖先保持 **41 个**；门禁选择不能当作已执行。Q008 仍进行中，完成计数仍 **7 / 291、20 / 975**。


### 用户插入优先项：持久化结算查询长耗时（进行中）

- 原机器门禁已主动终止，不再存活；不是观察超时后误报退出。未重新启动 191 文件门禁。
- 15 秒 py-spy 采样有 1298 个样本，主线程持有 GIL 执行历史 JSON/检查点反序列化；settled_projections/settled_effects 约 63% inclusive。后台调度线程在等待查询持有的写锁，未观察到锁环；现有证据是 CPU 重复工作，不能据此断言所有竞态均不存在。证据：q008-machine-live.speedscope.json。
- 根因：每次查询为 owner 重建全历史以确定作用域，又为 owner 和工具子任务分别全扫描 workflow commits、重建每一版完整 checkpoint（含冻结输入/工具目录）。
- 最小回归 tests/durable/test_settlement_projection_cost.py：一条结算后增加 16 次状态更新，结果必须仍为原结算且每个 task/cursor 至多构造一个最终 checkpoint。修改前实际失败：42 次构造，断言 42 <= 1 失败。修改后与 workflow_timeline/effect_identity 首批 20 passed（退出 0，2.17 秒）。性能要求来自只读历史投影所需数据：结算和截止点作用域，不需要构造已替代的全部检查点；不改变历史截止语义。
- 已实施单次原始日志遍历的批量投影，保留原日志/事务/查询锁、截止点、归属检查和冲突拒绝；不新增缓存、表或第二套事实账本。
- 原 writer 用例首轮修复后实际 1 failed（3.59 秒），暴露新投影未处理合法空 workflow={}。该失败为本次引入，正在修复并增加边界回归；不得作为通过或原问题已解决。
- 新发现其他工作区文件 catalog.py、research/answer_context.py、research/answer_contract.py 的改动不属于本项，保留且不暂存。

- 空 workflow 分支修正后原 writer / repair nodeid 分别 **1 passed，54.96 秒 / 1 passed，104.93 秒**（调用 53.86 / 103.81 秒），两次都保留原始断言。JSON 记录：settlement-writer-post.json、settlement-repair-post.json。
- repair 的第二次 15 秒采样：主线程 1062 样本；settled_effects 约 30.7%，SQLite settlement 批量读约 16.5%；仍有当前任务读取、ToolTaskInput 校验及调用方反复恢复成本。该采样和原门禁采样不是同一个执行阶段，不当作严格加速倍数。191 次非阻塞读取失败为 profiler 采样错误，不能当作竞态证据。
- 最小边界组：`python -m pytest -q tests/durable/test_settlement_projection_cost.py tests/durable/test_settlement_history_projection.py tests/durable/test_workflow_timeline.py tests/durable/test_effect_identity.py --tb=short` → 退出 0，**28 passed，2.17 秒**。
- 扩大存储/只读边界组：`python -m pytest -q tests/durable/test_settlement_history_projection.py tests/durable/test_settlement_projection_cost.py tests/durable/test_workflow_timeline.py tests/durable/test_inbox_placement.py tests/durable/test_queued_input_revision.py tests/durable/test_owner_reverse_index.py tests/durable/test_conversation_mutation_line.py tests/durable/test_store_contract.py --tb=short` → 退出 0，**116 passed，4.28 秒**。
- 调用方与竞态组：`python -m pytest -q tests/test_tool_task_next_stage.py tests/durable/test_execution_fencing.py tests/durable/test_scheduler_wakeup_races.py tests/durable/test_runtime_transaction_owner.py --tb=short --durations=5` → 退出 0，**34 passed，15.91 秒**；包含父子结算顺序冷热恢复、重复输入去重、执行权和调度唤醒竞态。
- 增补重复/冲突结算边界后两份新测试文件重跑：`python -m pytest -q tests/durable/test_settlement_history_projection.py tests/durable/test_settlement_projection_cost.py --tb=short` → 退出 0，**11 passed，1.72 秒**。
- 全文件加严 Any 检查七路径退出 1，**173 条 / 3 文件**：timeline:157 一条旧 Mapping Any；memory:574 三条旧 lambda 参数 Any；sqlite.py 169 条，来自 Row/游标/json.loads 等既有边界，新增方法无诊断。前两处已以类型收窄/明确回调参数修正，待复验。SQLite 整文件治理尚未完成，不据局部方法无诊断称整文件通过；其系统性边界改造留 Q052，不为当前性能修复扩大到整份事务引擎。
- 当前主要批次切到 Q053（用户明确优先的历史投影修复），Q008 标记部分完成、待门禁早期失败处置；已完成计数保持 7 / 291、20 / 975。


### 持久化长耗时修复验收（Q053；相关 Q052/Q055/Q061 保留部分完成）

**最终根因与修改**：这是已采样到的 CPU 历史重建放大，未发现锁环或可复现竞态；调度线程等待正在做重复解码的查询线。批量 API 一次遍历原 workflow 日志，在同一个 cutoff 内保留每次终态结算，仅构造各任务最终 checkpoint。没有去掉查询锁、改用当前效果行代替历史、增加缓存或新账本。effect payload 解码复用同一实现。合法空 workflow={} 跳过，不当任务读取。memory 旧队列排序回调明确参数/返回类型，顺序不变。

**原始文件完整覆盖**：先前 writer 和 repair 两个 nodeid 分别通过；随后 `python -m pytest -q tests/test_denied_probe_preserves_candidate_validation_evidence.py -k 'not test_denied_probe_then_stagnation_gets_actual_current_validation_and_completion' --tb=short --durations=8` 退出 **0，12 passed、2 deselected，632.91 秒**。这两个 deselected 是先前已单独执行通过的两例，不是 skip。合并为该文件全部 **14 个非重叠用例实际通过**；不称为同一次全文件命令通过。余批最慢单例 109.61 秒；review 三例约 100–102 秒，丢失退出码 109.54 秒。无新增 skip/xfail，也未修改该文件的断言、回合数或超时配置。性能成本仍偏高，残余当前任务/工具输入校验及调用方重复恢复成本记为 Q054/Q055/Q062 后续项，不声称所有性能问题都已消除。

最终串行验证（工作目录 D:/codey，日志在 .e2e-artifacts/code-quality-20261122/settlement-final-validation.json 及相邻日志）：

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/durable/test_settlement_history_projection.py tests/durable/test_settlement_projection_cost.py --tb=short` | 0 | 11 passed，1.71 秒；含 SQLite 重启与旧 cutoff、Memory/SQLite 空日志、旧单 task 格式、多任务过滤、重复/矛盾结算、损坏边界 |
| `python -m mypy codey/durable/timeline.py codey/durable/store.py codey/durable/task_queries.py codey/durable/memory.py tests/durable/test_settlement_history_projection.py tests/durable/test_settlement_projection_cost.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 6 个完整文件通过，未添加 cast/ignore/关闭检查；测试计数器用有注解函数，避免 mock 的 Any 传播 |
| `python -m mypy codey --python-version 3.11` | 0 | 737 个生产文件通过 |
| `pyrefly check codey --python-version 3.11` | 0 | 0 errors；既有 35 suppressed / 165 warnings 未消除 |
| `ty check codey --python-version 3.11` | 0 | 通过 |
| `python -m tools.kernel_type_precision_gate` | 0 | 通过 |
| `python -m ruff check codey/durable/timeline.py codey/durable/store.py codey/durable/task_queries.py codey/durable/memory.py codey/durable/sqlite.py tests/durable/test_settlement_history_projection.py tests/durable/test_settlement_projection_cost.py` | 0 | 首次仅新测试导入 I001，修正后通过；无无关格式化 |

737 文件检查前后哈希一致，changed_during_checks=[]；原慢文件余批期间五个 durable 修改文件哈希也未变化。稳定性记录：settlement-final-source-stability.json、settlement-source-snapshot.json。

**类型未完成范围**：SQLite 整文件额外 Any 169 条仍在 Q052（sqlite3.Row/游标返回值、JSON 边界；例如 sqlite.py:87、100、541–551 等），不能以标准 mypy 通过替代加严检查。旧 `tests/test_denied_probe_preserves_candidate_validation_evidence.py` 的未注解测试参数（如 :20、64、98）/mock capture，以及 `tests/project_candidate_recovery_fixture.py` 的动态 fixture 返回和回调，尚未做完整类型迁移；该脚本治理不计完成。根因修复可以独立保留；相关大文件迁移后续分批推进，不连带宣称整个 durable 区域完成。

**进度与未覆盖**：Q053 的原有两个生产文件 store.py/timeline.py 完成，完成 **8 / 291 批、22 / 975 生产文件**。Q052/Q055/Q061 仅完成本次受影响部分；无新增删除项。191 文件机器门禁仍未取得完整结果，其三个早期失败待 Q008 以小批定位；其余未执行范围及既有待确认事项仍保留。接下来恢复 Q008 最终验收，然后回到既定队列，不继续追加可选性能优化。根因修复提交按明确九个路径暂存；commit hash 在提交后记录。

根因修复已本地提交：60f68f9b（fix(durable): avoid rebuilding historical checkpoints for settlements），仅上述九个明确路径；其余 agent、browser、codemode、research 改动未夹带。总 goal 仍 active，非全仓完成。


### Q008 收尾：明确门禁失败归属并提交可独立成果

- 小批门禁：`python -m pytest -q tests/test_kernel_type_precision_gate.py tests/test_tool_execution_lifecycle.py tests/test_read_result_contract.py tests/test_rg_search_contract.py --tb=short` → **退出 1，95 passed / 3 failed，12.85 秒**。三个失败均位于 test_read_result_contract.py，不是导入/环境错误。
- :42 和 :106：旧测试将含 task_control 写操作的完整目录交给默认 allow_writes=False 的宿主/执行器，初始化在进入测试目标 read 结果断言前被拒绝。host.py:72/kernel.py:140 明确要求写目录显式许可，20261120 工具控制验证文档也记录 task_control 已进入 CodeMode。倾向 fixture 范围落后；不能为迁就用例取消宿主权限门，后续在 Q035/Q037 核对并修正合适的测试目录。
- :60：断言完整 read 返回签名必定内联，而实际是 compact catalog 的 describe 发现入口。20261115 收敛报告已有 24KB 内联预算与发现接口合同；但是否本应优先保留 read_file 签名仍须检查具体预算/渲染路径，暂不判定测试或生产正确。归 Q034/Q096 待确认，不直接把实际字符串改成期望。
- 三个触发路径是 CodeMode 目录构造/宿主初始化/声明展示，尚未进入工具调用或 durable 历史查询；所需冻结 ToolDefinition 与 CodeMode 源码没有在 Q008 中改动。该文件及 catalog.py 的既有动态类型/外部改动仍未完成治理，不计为已完成文件。
- durable 存储共享依赖修改后，Q008 最终重跑全部直接和调用方文件：`python -m pytest -q tests/test_project_audit_root_paths.py tests/test_agent_tools.py tests/test_agent_request_no_dead_fields.py tests/test_project_audit_tools_ownership.py tests/test_project_audit_advisor_kernel.py tests/test_audit_resolve_paths.py tests/test_consensus_audit_split.py tests/test_agent_effect_sandwich.py tests/test_tool_replay_policy.py tests/test_read_before_edit_ab.py tests/test_tool_runtime.py --tb=short` → **退出 0，161 passed / 42 subtests passed，12.44 秒**。不与先前重叠批次相加成唯一用例总数。
- 四个 Q008 生产文件与 q008-final-full-types.json 保存的最终哈希一致；前述九文件加严 Any、当前 737 文件三类型门及精度检查仍有实际成功证据。Q008 原有四个文件完成，累计 **9 / 291 批、26 / 975 生产文件**；191 文件门禁仍未完整通过，三个未解决项按其实际模块保留。
- Q008 准备独立提交 agent 工具契约、根目录链接修复、转发合并、四个相关测试文件和两份进度记录，不夹带其他工作区代码。下一批回到 Q008B：state.py / runaway_guard.py（状态与循环保护）。

Q008 已本地提交：3213e921（fix(agents): normalize audit roots and type tool dispatch contracts）。durable 修复后的九文件加严 Any 命令再次退出 0。下一批 Q008B 尚未开始；总 goal 保持 active。


### Q008B：有界信息去重与循环保护（进行中）

- 上轮为实际进展：60f68f9b / 3213e921 已提交。恢复时 HEAD=f328a801（其他工作 browser 提交），工作区仅本进度记录有上一轮 hash 补记；保留。目标、根 AGENTS、构建/测试配置及当前结构文档已重读，无嵌套 AGENTS。
- 声明范围保持原 state.py / runaway_guard.py；调用方 kernel_progress.py 与数据定义 runtime/core/progress.py 只按必要依赖追踪，不将它们算作全模块治理完成。state 的去重键/最大容量/插入顺序淘汰与内存快照使用一致，ToolAttemptRecord 按既有文档重导出 core 的同一类型。DEFAULT_CYCLE_PERIODS、detect_abab_cycle 等在 __all__ 中承诺公开，即使仓内调用少也保留；未以搜索无引用删除 API。_safe_int 对 bool 的行为不同于 coerce_int，不强行合并。
- **确定性 bug**：相同 read 参数读取三次，只要名称为 read 就忽略返回指纹而拦截；返回 first/second/third 或 first/first/changed 都被判为停滞。独立依据：runaway_guard 模块公开约束“only intercepts provably stuck loops / AAA same tool + same args + same result”，以及 kernel_progress.observe 的“新输出不应仅因旧参数而停止”约束。新输出不能证明相同调用/结果的闭环。
- 修改前 `python -m pytest -q tests/test_runaway_result_progress.py --tb=short` → **退出 1，2 failed / 7 passed，1.25 秒**；失败均为目标 read 行为，read_file/search 是对照，完全相同结果仍拦截的正向对照本来通过。生产修改后相同输入与断言通过。修复只删除忽略结果的特例，保留调用/结果双指纹的 AAA 和周期检测。
- `attempt_record` 直接使用 ToolResult.audit 的已验证 Mapping 字段，去掉 getattr 引入的 Any；ToolResult.__post_init__ 已规范 audit 为 dict，保持 changed 的原有 truthiness，不改变执行状态。现有 malformed-result/status 负例未放宽断言：坏 callback 用有注解函数加显式方法故障注入，仍由 execute_turn 判为 receipted error；五个坏 bool 值通过 object.__setattr__ 注入实际 dataclass 后调用原 __post_init__，校验路径与自动生成构造器调用同一方法，不把坏输入声称为 bool。
- 测试使用 JsonObject、明确回调/返回值、parse_json_object；原无类型 kernel_runtime helper 调用迁移到已有的 typed kernel_fixture 同一生产 driver 入口。该用例没有自定义 sink/审批/boundary observer，不涉及两 helper 的不同兼容分支；原畸形回复和 protocol 停止断言保留。旧 _State 的既有 arg-type ignore 未新增或扩展；不是 Any 逃逸的新手段。

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_seen_info.py tests/test_abab_cycle.py tests/test_noab_hardening.py tests/test_tool_execution_status_is_not_display_text.py --tb=short` | 0 | 修改前 36 passed，3.89 秒 |
| `python -m mypy codey/agents/state.py codey/agents/runaway_guard.py tests/test_seen_info.py tests/test_abab_cycle.py tests/test_noab_hardening.py tests/test_tool_execution_status_is_not_display_text.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 | 修改前 73 条 / 4 文件；state/test_seen_info 本来通过 |
| `python -m pytest -q tests/test_runaway_result_progress.py tests/test_seen_info.py tests/test_abab_cycle.py tests/test_noab_hardening.py tests/test_tool_execution_status_is_not_display_text.py --tb=short` | 0 | 修改后 45 passed，4.38 秒；后续负例注入类型调整待扩大组复验 |
| `python -m mypy codey/agents/state.py codey/agents/runaway_guard.py tests/test_seen_info.py tests/test_abab_cycle.py tests/test_noab_hardening.py tests/test_tool_execution_status_is_not_display_text.py tests/test_runaway_result_progress.py tests/support/kernel_fixture.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 1 → 0 | 首轮剩 mock.patch.dict 的隐式 dict[Any,Any] 和坏 bool 的静态合同冲突两条；显式故障注入/原校验方法替代后，8 个完整文件通过，无新增 cast/ignore 或关闭检查 |

依赖债务：runtime/core/progress.py 的持久快照 decoder 有 dict/list 运行时收窄后的隐式 Any 传播与 **record，原 Q190 尚未治理，不计完成；kernel_progress/progress_comparison 的其余业务仍归 Q102，不扩展当前批次。原 191 文件门禁与三个 CodeMode 待确认项保持未解决。当前完成计数仍 9 / 291、26 / 975，待本批最终验证、diff 和提交。


Q008B 最终验收：

- `python -m pytest -q tests/test_runaway_result_progress.py tests/test_seen_info.py tests/test_abab_cycle.py tests/test_noab_hardening.py tests/test_tool_execution_status_is_not_display_text.py tests/test_kernel_rejected_completion_progress.py tests/test_codemode_observation_progress.py tests/test_browser_action_progress.py tests/test_runtime_helper_boundaries.py --tb=short` → **退出 0，133 passed，70.29 秒**，包括持久恢复、周期/重复拦截、完成拒绝、CodeMode 与浏览器进度调用方；无 skip。
- 八路径 scoped Pyrefly / ty / Ruff 首轮分别 0 / 1 / 0。ty 暴露旧 _State 不是有效 TaskState（原 mypy arg-type ignore 不约束 ty）。独立依据为 build_conversation_plan 的 TaskState 契约及当前 durable 只读上下文要求：改为隔离 AppContext，关闭 scheduler/memory，设置相同 local/s 会话绑定、保持无历史上下文；四条 API 预算和 plan 身份断言不变，finally 关闭资源，删除原 ignore。`python -m pytest -q tests/test_abab_cycle.py::test_conversation_plan_applies_selected_api_budget --tb=short` → **退出 0，1 passed，1.52 秒**。这是旧 fixture 合同问题，不更改正确生产接口以迁就它。
- 修改 fixture 后，前述八完整路径的加严 Any mypy / Pyrefly / ty / Ruff **全部退出 0**，Pyrefly scoped 0 errors、0 suppressed；完整命令/输出保存在 q008b-scope-final.json。生产源码未再改动，不机械重跑此前未受影响的 133 例。
- `python -m mypy codey --python-version 3.11` → **0，737 文件通过**；`pyrefly check codey --python-version 3.11` → **0，0 errors，既有 35 suppressed/165 warnings 保留**；`ty check codey --python-version 3.11` → **0**；`python -m tools.kernel_type_precision_gate` → **0**。各命令串行、737 文件哈希前后一致，changed_during_checks=[]，记录 q008b-validation.json（该文件保留 scoped ty 首次失败，最终修复见 scope-final）。
- Q008B 两个生产文件完成；累计 **10 / 291 批、28 / 975 个生产文件**。本批仅提交 runaway_guard、三份既有测试、一份新增回归及两份进度记录，共七路径；state 源码无需修改，不制造空变更。下批 Q008C：handoff.py 的有界会话交接，先验证基线再改动。相关 Q190 快照类型债务、CodeMode 三个失败及其余原队列仍未完成。


Q008B 已提交：`0bafdae2`（fix(agents): distinguish new read results from runaway repeats）。

### Q008C：有界会话交接（进行中）

- 声明范围为 handoff.py。检查模型摘要提取/白名单/字段预算、快照恢复、会话窗口切换及取消/失败回退；公共 ConversationContext/Snapshot 被 app、operation、agents 使用，compact_text 等默认预算与通用 helper 不同，不以薄包装或引用少为由删除公开入口。prepare_model_handoff 与计划激活路径的 trace、窗口接线和记账语义不同，不强行合并。
- 基线 `python -m pytest -q tests/test_handoff.py --tb=short` → **退出 0，11 passed，1.12 秒**。
- 新摘要回归首轮 3 failed，其中一个错误地假定 2000 层嵌套在本机不能解析；实测 Python 3.12 可以解析它，此时前置 unknown 对象按既有“首个可解析对象”语义被选中，该测试的 ready 预期没有依据。生产代码未为此改变；将 fixture 改成按 JSON 语法确定非法的 BROKEN 字面量，原“找到后续合法对象”断言不变，该对照本来通过。其余两个是有效行为失败。
- **bug 准入**：`python -m pytest -q tests/test_handoff_summary_limits.py --tb=short` 在生产改动前最终 **退出 1，2 failed / 1 passed，1.23 秒**。输入均在 6000 字符模型摘要预算内：current_state 为 5000 位整数时默认解码抛 ValueError，未走既有无效摘要回退；current_state 为不可用的深层列表时，所有候选项被丢弃，兜底把 4019 字符原文直接写进字段，突破其他 current_state/字符串项共用的 MAX_HANDOFF_TEXT_CHARS=2000。独立依据是既有有界字段策略与“模型摘要失败仍保留本地事实”的回退职责，而非现有测试断言。
- 修复：两种 JSON 解析资源异常 ValueError/RecursionError 走原无效 JSON 的提取/文本回退；只在解码边界捕获，不捕获业务异常。无有效已知项时也以 compact_text 生成兜底字段。取消传播、原始 goal 和其他事实保留。
- 同类第二边界先单独复现：`python -m pytest -q tests/test_handoff_summary_limits.py -k snapshot_preserves --tb=short` → **退出 1，1 failed / 1 passed / 3 deselected，1.22 秒**。持久快照 conversation_summary=5000 位整数字符串在 to_payload 的 json.loads 泄漏 ValueError，而普通无效文本会进入原文本回退。随后对该解析边界采用相同修复，保留原 MAX_MODEL_SUMMARY_CHARS 文本预算；不截断成功解码的旧 JSON 结构。
- 类型基线（bug 最小修复后、类型迁移前）：`python -m mypy codey/agents/handoff.py tests/test_handoff.py tests/test_handoff_summary_limits.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` → **退出 1，47 条 / 2 文件**。
- 类型迁移将 unknown 限定在 JSON 解码和 from_payload 入场：先校验/收窄各字符串、列表成员和检查标签，再以具体字段构造 dataclass，去掉 Any kwargs；保留 tuple/list、未知元数据忽略及原错误消息。任意 JSON summary 经原 json.loads/json.dumps 验证和序列化，未新加深度/有限浮点/未知键限制改变旧数据兼容性。异构序列化投影为项目既有 dict[str,object] 合同，消费者需继续收窄值；没有新增 cast、ignore 或无参数回调。dict.fromkeys 的库返回值 Any 改为同序字典推导，去重/前 20 项不变。
- Codec 的新增测试补齐旧基线：在同一目录短暂使用 `0bafdae2` 的原 handoff.py 执行 `python -m pytest -q tests/test_handoff_snapshot_codec.py --tb=short` → **退出 0，15 passed，1.19 秒**；脚本 finally 恢复本轮字节并核对无并发改动，未建/复制 worktree。记录 q008c-codec-baseline.json，restored_own_source=true。该对照是在补充 codec 范围时实际执行，不冒充首轮 11 例已覆盖 codec。
- 最终直接组 `python -m pytest -q tests/test_handoff.py tests/test_handoff_summary_limits.py tests/test_handoff_snapshot_codec.py --tb=short` → **退出 0，35 passed，1.96 秒**；新增 ValueError/RecursionError 精确边界注入、后续合法片段和快照文本回退检查。
- 同三测试及 handoff.py 四完整文件的加严 Any mypy → **退出 0**。中间剩余的 dict.fromkeys 库默认 Any、JSONDecoder 类/回调库 Any 已以具体构造与有类型的边界替身消除，未增加宽泛忽略。
- 未计完成的依赖仍在原队列：conversation_plan 的 provider/trace 动态端口、conversation_runtime.py 的 ctx/durable/事件 Any 不在当前修复范围，不因此称整个会话运行时完成。正在按窗口复用、持久恢复和 app 快照使用关系扩大验证；当前仍 10 / 291、28 / 975 完成。


Q008C 最终验收：

- 调用方 `python -m pytest -q tests/test_context_delta_session_window_vs_handoff.py tests/test_recovery_handoff_preserves_original_task_prompt.py tests/test_conversation_resume_reuses_bound_window.py tests/test_conversation_registry.py tests/durable/test_provider_context_authority.py tests/durable/test_conversation_plan_no_provider_export.py tests/durable/test_session.py --tb=short` → **退出 0，45 passed，9.57 秒**，覆盖窗口复用、原任务保留、durable 快照与 app 缓存，不计作全仓测试。
- 四完整文件的 scoped Pyrefly / ty / Ruff → **全部退出 0**。`python -m mypy codey --python-version 3.11` → **0，737 文件**；`pyrefly check codey --python-version 3.11` → **0，既有 35 suppressed/165 warnings 保留**；`ty check codey --python-version 3.11` → **0**；`python -m tools.kernel_type_precision_gate` → **0**。命令串行，源码哈希前后一致，changed_during_checks=[]，记录 q008c-validation.json。先前直接组 35 例和四文件加严 Any 的最终成功仍有效，未放宽或删掉断言。
- Codec 核对：默认值、严格已知字段、changed_files 列表/元组、summary 字符串/数组/元组/字典、未知额外元数据的忽略和错误通道均保留。没有删除公开接口或将计划激活与模型摘要的不同职责强行合并。类型改动没有增加 JSON 预算/未知键限制；只修复已证明的解析回退与字段预算问题。
- Q008C 的 handoff.py 完成；累计 **11 / 291 批、29 / 975 个生产文件**。六个明确路径提交（生产一份、测试三份、记录两份）。下一批 Q008D：consensus.py 的只读顾问请求、报告归一与投票；原全仓队列、SQLite/运行时类型债务与 CodeMode 三个失败仍未完成，总 goal 不变。


Q008C 已提交：`7edc3897`（fix(agents): bound unusable handoff summaries and type snapshot decoding）。

### Q008D 已进入：只读顾问共识（基线完成，治理未完成）

- consensus.py 全文件已读；公开入口调用自 app/consensus_service、task_run 和 project 顾问/完成流程，提示/顾问/聚合预算和资源关闭需保留。该文件 413 行，原测试 test_consensus.py 混合共识与项目只读审计，仍按相关生产职责治理，不以测试文件作为新生产分区。
- `python -m pytest -q tests/test_consensus.py --tb=short` → **退出 0，34 passed，9.66 秒**；不能据此认为其类型已完成。
- `python -m mypy codey/agents/consensus.py tests/test_consensus.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` → **退出 1，119 条 / 2 文件**。生产源：selected_provider 的显式 Any、warn 与 timeout 的 getattr 动态端口；测试源：FakeProvider 的 reply/timeout/location/new_chat 合同、trace 回调、audit_runtime 宽字典展开及无类型 JSON/空容器。尚未修改这两个文件，不把这些错误归为本轮引入。
- 下一步先针对 max_advisors=0 的候选数量上限建立有效复现（目前只是候选，未宣称确认或修复），再处理发送者/告警/超时端口和必要测试 fixture；不改不同语义的 provider 兼容层、不复查已完成批次的可选优化。
- 本轮已完成 Q008B/Q008C，累计仍 **11 / 291 批、29 / 975 文件**。Q008D 进行中，当前没有活跃测试/类型进程，也没有未提交的生产改动；只有这份进度记录和队列状态的持久化更新待下一次记录提交。总 goal 保持 active，不调用 complete。


### Q008D 修复与类型治理（进行中）

- 恢复时 HEAD=7edc3897，只有上轮两份进度补记未提交，保留。目标、AGENTS 与当前源码已重核；原 Q008D 34 passed 基线和 119 条加严类型诊断仍为当前未改源码/测试的实际结果。
- **bug：零顾问预算仍发送请求。** max_advisors 是调用方给定的数量上限，0 的可论证不变量是不能选出/启动顾问。`python -m pytest -q tests/test_consensus_advisor_limits.py --tb=short` 在生产改动前 **退出 1，3 failed / 3 passed，1.23 秒**；选择器实际返回 ('a',)，入口在 draft_first=False/True 都返回 advisor_count=1 并发送消息，违反上限；1/2/10 上限是对照。随后入口选择函数对 0 返回空候选，沿原无候选路径返回 None，保留正数行为。未给未定义的负数预算另设新策略。
- 修复上限后原文件与新回归：`python -m pytest -q tests/test_consensus_advisor_limits.py tests/test_consensus.py --tb=short` → **退出 0，40 passed，9.91 秒**。
- **bug：用户停止被可选错误降级吞掉。** TaskCancelled 在 cancellation.py 的有效契约为“user stops the active task”，共识自身在发送/连接路径也明确传播该类，不能将同一停止事件在其他路径变成正常返回。`python -m pytest -q tests/test_consensus_cancellation.py --tb=short` 在对应生产改动前 **退出 1，3 failed / 3 passed，1.58 秒**；availability、warning、advisor_close 未抛出停止，连接/发送/最终拥有者发送是对照。随后这三处明确重抛 TaskCancelled，清理仍执行；普通 RuntimeError 保持原降级/尽力清理。DeadlineExceeded 代表有界 provider 操作预算，可能是可选顾问自己的预算，不据偏好统一改成任务停止。
- 类型端口：拥有者只需 ConsensusSender.send；连接的顾问保持原 ChatProvider 契约。timeout 通过 object 边界验证为数字或 None，保留缺省的 60/90 秒、显式 None/0/180，不做额外正数/有限性或字符串转换策略；非法元数据在发送前报类型错误。告警按原 getattr 查出钩子后，验证可调用协议（单个 str 参数、忽略其返回值），保留动态代理。
- 中间用 trace 对象整体 Protocol 检查时，动态 __getattr__ 的 warn 代理在 Python 3.12 被遗漏；新增原行为回归实际 **1 failed / 5 deselected**。该问题是本轮类型尝试引入，已改为对取出的回调本身验证，并随最终直接组通过；未为了收窄类型牺牲代理兼容性。
- 测试全文件：FakeProvider 的 Sequence[str|Exception] 与真实错误用例匹配；补齐 location/new_chat(timeout) 合同；timeout 只声明、不设默认，保留原“不存在此属性”语义。audit_runtime 用三个具体资源字段的 TypedDict，trace 收集端口和 JSON 读取有类型，容器与原 source_refs/降级/发送次数断言不变，增加收窄而非跳过坏行。加严检查由 **119 → 37 → 0**，无新增 cast/ignore 或关闭检查。
- 依赖收敛：suppress_assistance 原由 provider_controls 从 providers.assistance 重导出，现直接依赖唯一计数器所有者；三个调用位置、同一个函数和退出/恢复行为不变，旧 controls 公共重导出未删除。全树未发现调用 consensus.provider_controls 的配置/脚本或接口约定。_provider_send_ref 实际被 project_audit_advisor 导入调用，_clip/_trace_model_prompt 带本域合同，不按私有名或薄包装删除。
- 原长 run_consensus 增加停止分支后 Ruff 实际报 C901=21、branches=22，未放宽门槛；将有独立职责的顾问清理抽成 _close_advisor，测试保留普通关闭错误不丢已完成建议，复验通过。

最终直接与类型记录：

| 命令 | 退出码 | 实际结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_consensus_provider_ports.py tests/test_consensus_cancellation.py tests/test_consensus_advisor_limits.py tests/test_consensus.py --tb=short` | 0 | 53 passed，10.73 秒，无 skip；含修复前相同输入/关键断言和端口边界 |
| `python -m mypy codey/agents/consensus.py tests/test_consensus.py tests/test_consensus_advisor_limits.py tests/test_consensus_cancellation.py tests/test_consensus_provider_ports.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` | 0 | 五个完整文件通过 |
| 同五路径 `python -m ruff check ...` | 0 | 复杂度/分支/风格门通过，未新增忽略 |

当前进行 project audit、trace、lazy import 和 app 消费者验证。共同 provider assistance/trace 后端的旧动态字段未据消费者类型通过而标记治理完成，仍归原 providers/runtime 批次；当前完成计数保持 11 / 291、29 / 975。

### Q008D 调用方结论与可独立提交成果（部分完成）

- 恢复后读取完整目标、AGENTS、类型/测试配置和工作区；没有复制 worktree。用户强调的 SQLite 空 workflow 误读已在 `60f68f9b` 修复，原慢文件拆批执行证据见此前 durable 记录，不重复冒充新修复，也不把采样读取失败当竞态。
- 调用方首批 `python -m pytest -q tests/test_project_audit_advisor_kernel.py tests/test_consensus_audit_split.py tests/test_change_brief.py tests/test_server_lazy_state.py --tb=short` → **退出 1，3 failed / 23 passed / 4 subtests passed，15.20 秒**。三处 lazy import 失败在 HEAD 原 consensus.py 上独立复跑 `python -m pytest -q tests/test_server_lazy_state.py --tb=short` → **退出 1，同样 3 failed / 8 passed / 4 subtests，11.85 秒**；finally 恢复本轮文件并验证字节，q008d-lazy-baseline.json。无完整绿色懒加载门禁。
- 实际导入栈追到 consensus_service → project_audit_advisor → task_loop → kernel_driver → kernel_context → kernel_actions → kernel_execution → browser_progress → browser_binding → Playwright。有效依据是 consensus_service 模块的 import-light 文档；这是可定位的既有生产边界问题，不是错误测试。证据 q008d-lazy-import-chain.json；路由 Q085/Q097/Q017，未在 Q008D 扩展修改浏览器模块。不可宣称已修复该问题。
- 服务首批 `python -m pytest -q tests/test_prompt_envelope.py tests/test_shared_task_service_consumers.py tests/test_server.py::ConsensusConnectionTests --tb=short` → **退出 1，1 failed / 23 passed，4.55 秒**。根因为无约束 Mock 的 getattr 自动合成 Mock timeout；ChatProvider.send 的公开契约为 float|None，web provider 本无此属性，API provider 构造器 timeout 为 float。fixture 改为明确删除动态 timeout，模拟真实缺省属性；保留 sibling-tab、返回内容、new_chat、close 断言，并加强发送断言为 60/90 秒。相同命令复验 **退出 0，24 passed，4.39 秒**；未为迁就 mock 放宽生产端口。
- `tests/test_server.py` 全文件加严检查实际 **退出 1，1709 条 / 1 文件**（q008d-server-any.log）；来源包括 68 行无泛型 dict、83 行无类型回调、JSON/Mock 广泛传播，本次涉及的 2190–2221 行也仍有 Mock Any。完成全文件需要独立的共享 fixture/HTTP 场景迁移，超出本模块边界；准确路由 Q015。本脚本和 Q008D **不标记完全完成**，保留独立可验证成果后推进队列。

最终串行验证（源码/相关测试哈希前后一致，changed_during_checks=[]；q008d-validation.json）：

| 实际命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python -m pytest -q tests/test_project_audit_advisor_kernel.py tests/test_consensus_audit_split.py tests/test_change_brief.py --tb=short` | 0 | 15 passed，5.03 秒 |
| `pyrefly check codey/agents/consensus.py tests/test_consensus.py tests/test_consensus_advisor_limits.py tests/test_consensus_cancellation.py tests/test_consensus_provider_ports.py --python-version 3.11` | 0 | 五完整文件 0 errors |
| 同五路径 `ty check ... --python-version 3.11` | 0 | 通过 |
| `python -m ruff check` 同五路径加 `tests/test_server.py` | 0 | 六文件通过，无新忽略 |
| `python -m mypy codey --python-version 3.11` | 0 | 737 文件通过 |
| `pyrefly check codey --python-version 3.11` | 0 | 0 errors，原有 35 suppressed / 165 warnings 保留 |
| `ty check codey --python-version 3.11` | 0 | 通过 |
| `python -m tools.kernel_type_precision_gate` | 0 | 通过 |

此前 53 例直接验证和五文件加严 Any 成功仍有效，生产源未再修改。确定性修复按零预算、取消传播与端口类型两份本地提交拆分；本批仍为 partial_follow_up，累计完整完成 **11 / 291 批、29 / 975 文件**，不把共享脚本待办算已完成。下一主批 Q009：writer_failover.py，接着 Q010 上下文、Q011 协议、Q012 shell 审批；原 191 文件门禁未完整通过、CodeMode 三失败与 SQLite 类型债务继续保留。

Q008D 零预算修复已提交：`94f1cfad`（fix(agents): honor zero consensus advisor budget）。取消传播、类型端口与必要 fixture 的独立验证已完成，以下提交继续保存这部分；没有夹带浏览器或 durable 生产修改。

Q008D 其余独立成果已提交：`deb4bf6a`（fix(agents): propagate consensus cancellation and type provider ports）。工作区清洁后进入 Q009；共享 server 脚本类型与懒加载失败的 partial 状态不变。

### Q009：Writer 接管状态机（进行中）

- 范围 writer_failover.py 全文件，入口 WriterFailoverRunner.run；调用方 project_writer_phase/_repair_writer 与 candidate validation，CheckpointView 也用于 reviews。公开 __all__、文档与手动场景仍引用这些入口，不删作死代码。接管与初始重连保留不同 handoff 语义；未将异常/副作用不同的路径强行合并。
- 原基线 `python -m pytest -q tests/test_writer_failover.py --tb=short` → **退出 0，12 passed，1.10 秒**。源与该脚本加严 mypy 初测 **退出 1，186 条，仅测试文件**；生产源没有显式或隐式 Any 诊断。测试的无注解 harness/callback/list 与 dict 假 provider 是传播源。
- 按原生产端口将 fake 包装为 ProviderTransport，保留原字典载荷及 provider 相等性/关闭断言；明确所有 hook/callback/容器类型，不以 object 替换实际需要具体操作的数据。生产代码尚未改动时原 12 用例再跑 **退出 0，12 passed，1.12 秒**，建立 fixture 改动的独立行为基线。
- **bug 准入**：模块 docstring 要求 honour Stop first，TaskCancelled 表示用户停止；真实 project_writer_phase 将当前 run 的 stop_flag.is_set 传给 stopped 回调。原实现仅在 Writer 的 ProviderActionError 分支检查，初次连接、后续接管和清理路径不满足相同契约。
- `python -m pytest -q tests/test_writer_failover_cancellation.py --tb=short` 修改生产前 **退出 1，6 failed / 2 passed，1.24 秒**（q009-cancellation-before.json）。输入为已停止但有/无连接、连接失败时置停止标志、清理直接抛 TaskCancelled、失败 canary 时停止；实际继续连接/运行并正常返回，没有传播停止。普通 RuntimeError 清理对照原来通过。修复加入边界停止检查并仅重抛清理中的 TaskCancelled，保留普通清理容错与 provider 引用先清空的不变量。
- 补充最后一个切换预算边界：`python -m pytest -q tests/test_writer_failover_cancellation.py -k 'final_takeover or failed_canary' --tb=short` 在对应分支修复前 **退出 1，2 failed / 1 passed / 7 deselected，1.24 秒**。最后一次连接/canary 失败且同时停止，实际错误地抛 ProviderActionError；进一步在终止分支前检查停止，使停止优先于 provider 故障。不是宣称线程竞态已复现或所有时序均无竞态。
- 最终直接组 `python -m pytest -q tests/test_writer_failover_cancellation.py tests/test_writer_failover.py --tb=short` → **退出 0，22 passed，1.54 秒**。包含正常接管、共享总 turn/switch 预算、旧 handoff 语义、普通清理失败及新增停止路径。随后仅调整测试容器类型推导和导入顺序，关键断言未放宽。
- 三完整文件加严 Any mypy：**186 → 10 → 1 → 0**，末条来自 assertEqual 为 sorted 表达式提供 Any 上下文，改为有推导类型的局部变量。没有新 cast/ignore 或关闭门禁。当前正在运行五个明确调用方文件；完成计数仍 **11 / 291、29 / 975**，待其结果及最终检查/diff/提交。

Q009 调用方局部阻碍：

- 五文件首批 `python -m pytest -q tests/test_review_repair_preserves_total_turn_budget.py tests/test_writer_checkpoint_scope_reaches_completion_gate.py tests/test_writer_request_forwards_workspace_revision_store.py tests/test_api_connections_do_not_enter_browser_failover.py tests/test_readonly_stagnation_completion.py --tb=short` 在 checkpoint 的 `[True-done]` 节点超过 120 秒。py-spy 实际只有主线程，在 evaluate_completion → wait_tool_task → Event.wait 等待；当前 fixture 只建 MemoryDurableStore，没有执行子工具任务的 worker/scheduler。该节点直接调用 _run_one_writer_attempt，没有经过本批修改的 WriterFailoverRunner。未证明生产等待合同错误，不改完成门让未执行的命令假装完成。
- 已通过 Ctrl-C 终止该次测试进程，工具报告 **退出 1、无 pytest 最终汇总**；打印的进度点不冒充用例完成数。证据 q009-caller-wait-stack.log。初次原版对照的计时器覆盖收集，8 秒时尚未执行到测试，结果不具有行为复现效力（q009-caller-baseline-attempt1-inconclusive.json），不作为基线结论。
- 修正对照探针，让 8 秒计时从 pytest_runtest_call 开始：在 HEAD 原版 writer_failover.py 上实际到达 automatic_command_result → wait_tool_task 的同一子任务等待；记录本模块运行期函数调用数 **0**。主动中断后 **退出 2、无完成用例数**，不是通过结果；q009-caller-baseline.json 含完整栈。finally 恢复本轮字节且哈希一致，未复制 worktree。该 fixture/runtime 调度问题路由 Q094/Q095/Q099，未夹带进本批。
- 后续以明确 nodeid 拆出 checkpoint 的 scope 合并和 `[False-max_turns]`，继续运行原组的其余四文件；`[True-done]` 仍未完成验证，不添加 skip、不删用例或放宽断言。另补充“Writer 尝试中才置停止标志”的有效对照，避免入口提前停止使原异常分支失去覆盖；其运行结果待最终直接组记录。

Q009 最终验收：

- 拆分调用方实际命令：`python -m pytest -q tests/test_review_repair_preserves_total_turn_budget.py tests/test_writer_checkpoint_scope_reaches_completion_gate.py::test_checkpoint_scope_is_combined_with_new_attempt_edits tests/test_writer_checkpoint_scope_reaches_completion_gate.py::test_checkpoint_edit_scope_reaches_real_writer_gate_without_reediting[False-max_turns] tests/test_writer_request_forwards_workspace_revision_store.py tests/test_api_connections_do_not_enter_browser_failover.py tests/test_readonly_stagnation_completion.py --tb=short` → **退出 0，19 passed，178.99 秒，无 skip**。含 review 共享预算、checkpoint 作用域、workspace revision 传递、API/browser 接管排除和只读完成恢复；单个 `[True-done]` 未完成的等待仍明确保留，不宣称该文件全绿。
- `python -m pytest -q tests/test_writer_failover.py tests/test_writer_failover_cancellation.py --tb=short` → **退出 0，23 passed，1.59 秒**；同三个完整路径加严 Any mypy / scoped Pyrefly / scoped ty / Ruff **全部退出 0**。有实际运行的普通失败、停止优先、预算/hand-off 保持验证；没有新增忽略、skip 或放宽断言。
- 生产 `python -m mypy codey --python-version 3.11` → **0，737 文件**；`pyrefly check codey --python-version 3.11` → **0，0 errors，原 35 suppressed/165 warnings**；`ty check codey --python-version 3.11` → **0**；`python -m tools.kernel_type_precision_gate` → **0**。全部串行，源码与本批脚本哈希前后一致，changed_during_checks=[]；q009-validation.json 含真实命令、退出码与输出。
- Diff 已核对，仅状态机、原测试、新增取消回归及两份记录；未改调度器或未完成 fixture。Q009 已完成，累计 **12 / 291 批、30 / 975 生产文件**；Q008D 仍是 partial_follow_up。下一主批 Q010 的 context.py/context_compaction.py，按实际上下文装配/消息组边界建基线，不回到前面文件追加可选优化。

Q009 已提交：`7f1e50ee`（fix(agents): honor task stops throughout writer failover）。

### Q010：上下文装配与消息组（进行中）

- 两个完整生产文件已读：context.py 的根目录指令、受权限约束的事实来源、PromptEnvelope 与 memory spans；context_compaction.py 的索引分组和完整 ID 配对。装配消费 project_adapter；消息分组消费 api_chat.prepare/validate_view/closed_spans，后接 provider compaction。两个生产文件原本加严 Any 无诊断，不为制造类型变更修改它们。
- 初始 `python -m pytest -q tests/test_context_tool_group_requires_exact_result_ids.py tests/test_native_coding_context_uses_native_instructions.py --tb=short` → **退出 0，10 passed，2.80 秒**。两源和 pairing 测试的加严 mypy → **退出 1，16 条，仅测试文件**；原无注解 calls/results、异构 message list 与无返回注解为来源。
- 死分支证明：非空 tool_calls 分支的内层 while 消费该条调用之后所有连续 tool 消息；下一次外层迭代若前一个组仍以该调用开头，当前条必然不是 tool。其他分支建立的组首又没有非空 tool_calls。因此 else 内“当前是 tool 且上一个组首带调用”的合取不可成立。检查 api_chat 的三处消费者只传稳定原始消息记录；无公共入口、导出、配置或插件被删除，_is_tool_message 仍供真实路径使用。
- 为删除建立分组基线：空列表、纯文本、连续多结果、孤立工具结果、被 user 中断的悬空调用、相邻完整调用组，加原坏 ID/乱序正例；`python -m pytest -q tests/test_context_compaction_grouping.py tests/test_context_tool_group_requires_exact_result_ids.py --tb=short` 删除前 **退出 0，15 passed，1.55 秒**，删除后同输入/断言 **退出 0，15 passed，1.56 秒**。只删除不可达追加分支，保持 singleton 和完整 ID 规则；未宣称修复新 bug。
- pairing 脚本补齐 Sequence[str|int]、返回注解、明确消息容器；原 int/空 ID、重复/外来/缺结果负例和 RequestPrepError 断言全部保留。初次 starred comprehension 与 list 不变性产生一条诊断，以 typed result_messages 保留同序/同值构造；最终 **16 → 1 → 0**。`python -m mypy codey/agents/context.py codey/agents/context_compaction.py tests/test_context_tool_group_requires_exact_result_ids.py tests/test_context_compaction_grouping.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` → **退出 0，四完整文件**。
- API 消费者 `python -m pytest -q tests/test_context_compaction_within_long_tool_turn.py tests/test_context_compaction_transactions_and_lifecycle.py tests/test_api_chat_native_turns_and_history.py --tb=short` → **退出 0，41 passed，2.12 秒**，包含后台 tail、并发发送/关闭、未回答工具与请求准备。context.py 没有生产变更，正在补验实际装配调用方。当前仍 **12 / 291、30 / 975**，待最终检查/提交。

Q010 最终验收：

- `python -m pytest -q tests/test_task_entry_cutover.py::test_project_writer_keeps_bounded_project_context_in_first_prompt tests/test_agent_completion_repair_context.py --tb=short` → **退出 0，8 passed，9.15 秒**；实际涵盖根 AGENTS、项目事实/research/map/checkpoint、权限来源过滤、空/续接 repair context 和发送 epoch，不冒充 task_entry_cutover 全文件通过。两批调用方真实记录 q010-callers.json。
- 四完整文件 scoped Pyrefly / ty / Ruff → **全部退出 0**；生产 `python -m mypy codey --python-version 3.11` → **0，737 文件**，`pyrefly check codey --python-version 3.11` → **0，0 errors，保留原 35 suppressed/165 warnings**，`ty check codey --python-version 3.11` → **0**，`python -m tools.kernel_type_precision_gate` → **0**。串行结果与哈希前后一致见 q010-validation.json，changed_during_checks=[]。前述四文件加严 Any、15 例直接验证仍有效，未重复运行无变化范围。
- 没有待确认 bug 被按猜测修复；context.py 已完成全文件检查/类型验证，无需源码修改。唯一生产变更删除不可达分支，测试只补类型/分组基线。Diff/暂存前检查通过，当前批次 **已完成**；累计 **13 / 291 批、32 / 975 生产文件**。Q008D/shared server 类型、既有 lazy import、checkpoint True-done 调度等待、原 CodeMode/SQLite 债务继续留队；总 goal 未完成。下一主批 Q011：protocol.py 的协议纠错、路径规范及验证禁止识别。

Q010 已提交：`b05ae646`（refactor(agents): remove unreachable compaction group append）。

### Q011：协议纠错、路径规范与验证禁止（独立修改已验证，待提交）

- protocol.py 全文件已读，路径键用于 tool_execution/edit_base/kernel_facts/CodeMode，验证禁止用于 project_adapter/task entry；纠错 examples 仍被 manual coding A/B 脚本与 docs/manual/README.md 的公开工具约定使用，不以当前内核改用 kernel_prompt 就删除它们。JSON scanner 是直接 Any 来源，必要扩展至其完整 111 行及配套测试；该文件归 Q118，本轮只记其文件级验收，Q118 的 base/text_turn 仍未深入检查，保持部分完成。
- 初始 `python -m pytest -q tests/test_explicit_verification_denial.py tests/test_coding_repair_prompt_ab.py tests/test_kernel_protocol_repair_keeps_snapshot.py tests/test_unicode_int_hardening_locks.py --tb=short` → **退出 0，32 passed，4.27 秒**。protocol.py 单文件加严 Any **26 条**，加 scanner/两份直接测试的四完整路径基线 **63 条 / 3 文件**，q011-any-before.log。
- 扫描重构先补契约基线：`python -m pytest -q tests/test_json_scanner_candidate_extraction.py --tb=short` → **退出 0，7 passed，1.08 秒**，包括字符串内 think 原文、外部 think 移除、candidate strict=True 拒绝裸控制符、普通提取 strict=False 保留控制符、坏片段后续有效对象、后键覆盖/NaN 与 70 层可解码数据。没有为了类型化加入新有限浮点、重复键拒绝或 JSON 深度预算。
- 将相同“平衡片段 → JSON 解码 → 对象收集”循环移到 scanner 唯一 _extract_objects(text, strict=...)，保留两公共入口的 strict 差异和 candidate 前置去 think；异常策略/对象顺序/字段值不变。JSON 解码先入 object，使用既有 object_fields 验证字符串键；值保持 unknown，由 repair 消费者按 str/dict 等现有语义收窄，而非用无依据 cast 跳过检查。嵌套 done summary 使用同一键校验边界；codec 名称和回调先收窄，保留 getattr 动态回调及 str 转换/TypeError、ValueError 回退。
- 剩余一条来自 re.Match.group 的库返回 str|Any，以 object 入边界后验证 str，正常捕获组保证仍为同一小写工具名。四完整文件加严 Any **63 → 1 → 0**；未新加 cast/ignore 或关闭检查。生产端口未经猜测增加拒绝规则。
- 类型/合并后 `python -m pytest -q tests/test_json_scanner_candidate_extraction.py tests/test_explicit_verification_denial.py tests/test_coding_repair_prompt_ab.py --tb=short` → **退出 0，22 passed，2.34 秒**。扩大 `python -m pytest -q tests/test_json_codec.py tests/test_json_scanner_candidate_extraction.py tests/test_coding_repair_prompt_ab.py tests/test_explicit_verification_denial.py tests/test_kernel_protocol_repair_keeps_snapshot.py tests/test_edit_rejects_legacy_aliases_before_execution.py --tb=short` → **退出 0，34 passed，5.08 秒**；`python -m pytest -q tests/test_text_turn_decoder_contract.py tests/test_kernel_consumes_structured_text_turns.py tests/test_native_final_answer_protocol_repair.py tests/test_fail_closed_numeric_guards.py::test_ascii_digit_gate_rejects_non_ascii tests/test_unicode_int_hardening_locks.py::test_protocol_unicode_fail_closed --tb=short` → **退出 0，15 passed，4.29 秒**。
- **确定性 bug 准入：~ 根目录下的合法相对路径被记账失败。** safe_join 的公开职责是将 rel 解析到 root 内，并明确 expanduser；canonical_project_path 的调用方在成功 read 后需要同一文件的相对 POSIX 键。AgentRequest.project 是 Path，不要求预先展开。原 safe_join 返回 home/项目/pkg/app.py，却拿 cwd/~/项目 当基准执行 relative_to，违反根目录别名不改变相对路径的不变量。
- `python -m pytest -q tests/test_canonical_project_path_roots.py --tb=short` 在该函数修改前 **退出 1，3 failed / 5 passed，1.32 秒**；~ 根的 .、pkg/app.py、pkg/../pkg/app.py 均因错误基准抛 ValueError，绝对根和两种根的 ../ 越界拒绝对照通过。q011-canonical-before.json 保存实际栈。测试只创建独占临时目录（项目 pytest 自带隔离 home），未重设 HOME/USERPROFILE 或复制工作区。
- 入口先展开并解析 root，再把同一 root 交给安全拼接和 relative_to；保留路径越界检查。`python -m pytest -q tests/test_canonical_project_path_roots.py tests/test_edit_rejects_legacy_aliases_before_execution.py tests/test_tool_execution_status_is_not_display_text.py --tb=short` → **退出 0，25 passed，2.78 秒**；相同输入/断言及 read/edit 状态消费者均通过。未宣称修复路径 TOCTOU 或竞态。
- 根路径修改后，五完整文件（上述四份加 root 新回归）的加严 Any / scoped Pyrefly / scoped ty / Ruff **全部退出 0**；生产 mypy **0，737 文件**、Pyrefly **0，原 35 suppressed/165 warnings**、ty **0**、精度门 **0**。所有命令串行且代码/测试哈希前后一致，changed_during_checks=[]；q011-validation.json 为最后结果，q011-validation-before-root-fix.json 是此前类型重构时的结果，两者未混同。
- 将类型/decoder 合并与根路径 bug 拆成两个职责清楚的本地提交；当前仍按 **13 / 291、32 / 975** 统计，待最后提交验收。Q118 scanner 文件虽已验收，未把该批其余两个文件或全仓扫描器声称完成。

Q011 类型与解码收敛已提交：`f7ac1ac6`（refactor(protocols): consolidate typed JSON object extraction）。根路径修复 diff 仅统一 root 展开/解析与使用同一基准，加八个已验证边界回归；暂存前核对没有其他工作修改。本批验收完成，累计 **14 / 291 批、33 / 975 生产文件**（scanner 在 Q118 仅计文件级验证，不加到完整批次覆盖数）。下一主批 Q012：shell_approval.py 的审批输入、事件通知与停止/超时边界。原 Q008D、Q118 其余文件和所有已记录阻碍保留，总 goal 仍 active。

Q011 根路径修复已提交：`d34a04e0`（fix(agents): canonicalize paths against expanded project roots）。

### Q012：审批请求与原生身份编码（进行中）

- shell_approval.py 全文件已读。实际职责是 DTO/持久 pending/UI event 投影、command 显示界限/哈希一致性与原生调用身份，等待/停止/超时执行器属于其他模块，不将它们并入本批主线。公共 __all__ 入口被 request/hooks/approval workflow、测试、stress 与 manual 场景使用，未发现可证明可删的公开入口。
- 源码原本加严 Any **退出 0**；源码与完整 native_identity/event_p2 两测试加严 Any也 **退出 0，3 文件**。没有为了产生类型变更重写已精确字段。shell_contract 是实际过程/调用方验证，不计为其全脚本已治理或已无 Any。
- 基线 `python -m pytest -q tests/test_shell_contract.py tests/test_shell_approval_native_identity.py --tb=short` → **退出 0，29 passed，17.47 秒**，包含真实 shell 的 argv/引用、解释器固定、Unicode、超时、原生调用批准/拒绝与 durable 恢复；无 skip。
- 重复性已确认：to_payload 和 build_shell_approval_pending 的 call_id/invalid flag、严格非负 turn/tool_index 与 window_ref 编码完全相同；二者只读同一请求、无状态/资源副作用。provider_id 在请求中截到 240 字符、pending 中仅 strip，语义不同，保留各自处理；命令预览/哈希和 deferred 上限/持久全记录也未强行合并。持久 artifact 使用 sort_keys=True，身份字段编码顺序不改变原件哈希的含义。
- 重构前新增 `python -m pytest -q tests/test_shell_approval_identity_payloads.py --tb=short` → **退出 0，9 passed，7.82 秒**：call_id 空白/原样空格/256 和 257 边界、window ref、负数/bool 数字字段省略、provider 两种长度策略。随后将相同身份字段实现抽为模块内唯一 _call_identity_fields，两消费者迁移并删除旧重复块。只合并确定重复，不宣称修复新 bug。
- 当前运行新旧 native 身份及 event shape 明确三文件组；源码/三测试的最终类型门、调用方验证和 commit 尚未完成，计数仍 **14 / 291、33 / 975**。

Q012 最终验收：

- `python -m pytest -q tests/test_shell_approval_identity_payloads.py tests/test_shell_approval_native_identity.py tests/test_shell_event_p2_locks.py --tb=short` → **退出 0，22 passed，16.84 秒**。同四完整路径的加严 Any mypy 与 Ruff → **退出 0**；scoped Pyrefly **0 errors / 2 warnings**、ty **通过**，不把 warnings 隐瞒为零。保留原生原 call id、批准/拒绝/恢复和事件边界断言。
- `python -m pytest -q tests/test_shell_approval_record_store.py tests/test_shell_approval_record_initialization.py tests/test_shell_approval_http_restart.py tests/test_shell_analysis_authorization.py --tb=short` → **退出 0，29 passed / 2 skipped，20.93 秒**。跳过来自既有 installed() 的 zsh 缺失，未增加 skip。
- 为确认两项 skip 原因及覆盖命令校验，首次误选不存在的 wildcard[zsh] 参数节点，工具实际 **退出 1、no tests ran，1.26 秒**，仅收集选择错误，不算行为失败或通过。随后 `python -m pytest --collect-only -q tests/test_shell_analysis_authorization.py` → **退出 0，22 collected，1.13 秒**，没有执行用例；据真实 nodeid 纠正为 quoted-separators[zsh]/opaque-request[zsh]，与 coldstart_v1_locks 中 shell_event 的 legacy/digest/preview/current 四 nodeid 一起执行 → **退出 0，4 passed / 2 skipped，1.42 秒**，-rs 明示二者均“shell executable unavailable: zsh”。完整实际命令和退出/数量见 q012-test-records.json，未机械重跑已通过的 29 例。
- 生产 mypy **0，737 文件**；Pyrefly **0，0 errors，原 35 suppressed/165 warnings**；ty **0**；精度门 **0**。按项目命令显式目标 codey/Python 3.11 串行运行，与四文件 scoped checks 的完整命令/输出保存在 q012-validation.json。源码与测试哈希前后一致，changed_during_checks=[]。
- 变更只涉及 shell_approval.py、一个有类型的基线测试和两份记录；没有更改审批授权、命令执行器或持久恢复判定。Diff 核对通过，本批完成；累计 **15 / 291 批、34 / 975 生产文件**。下一批 Q013 为 app 包入口，之后 Q014 的 provider 连接/选择区域。Q008D/shared server 类型、Q118 其余文件、原懒加载/调度等待等待办仍未完成；总 goal 仍 active。

Q012 已提交：`d9988a52`（refactor(agents): share shell approval call identity encoding）。

### Q013：app 包入口（已完成，无需修改）

- `codey/app/__init__.py` 全文件只有包说明，没有执行入口、导出、注册或 Any；pyproject 的 setuptools namespaces=false 要求普通包标记，不以“没有可调用内容”删掉它。
- 新解释器实际导入 codey.app，核对 __file__ 是当前仓库文件，并断言未加载浏览器/Playwright，**退出 0**；该文件 `python -m mypy codey/app/__init__.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any`、`pyrefly check codey/app/__init__.py --python-version 3.11`、`ty check codey/app/__init__.py --python-version 3.11`、`python -m ruff check codey/app/__init__.py` 均 **退出 0**。完整命令/输出 q013-validation.json。这里只执行导入/类型/静态验证，没有执行 pytest，不虚报用例数。
- 没有源码修改或空提交；检查记录持久化在本次进度更新。完成累计 **16 / 291 批、35 / 975 生产文件**；975 文件仍各有唯一归属，清单无丢失/重复。

下一主批 Q014 尚未开始深入检查：已仅用 AST/docstring 映射七文件的入口和规模，未计作源码治理或执行测试。provider_services 是 384 行的门面，registry/status 各有职责，operator_auth.py（49 行）实际负责一次性 launch/session 凭据；进入生产修改前先按真实调用关系细化职责边界并保留所有文件归属。不要重新检查已完成 agents 文件的可选优化。

当前状态：本轮七个本地 commits 94f1cfad / deb4bf6a / 7f1e50ee / b05ae646 / f7ac1ac6 / d34a04e0 / d9988a52，所有生产/测试修改已提交，仅本次进度/hash 补记等待后续记录提交。没有活跃测试或类型进程。总 goal 仍 active，原 191 文件门禁未完整通过、Q008D 的共享 server 类型债务、Q118 未检查文件、lazy import、checkpoint True-done 调度等待、CodeMode 三失败和 SQLite Any 债务继续留队；未将这些项目记为已完成。


### Q014：provider 注册表与异步状态（进行中）

- 恢复时 HEAD=d9988a52，只有本任务两份进度/hash 补记未提交，保留。重读完整目标、AGENTS 与类型/测试配置；上轮七个 commits 和 Q013 导入/检查有实际证据，属于进展。没有复制仓库/worktree，也没有并发测试/重型类型进程。
- 原 Q014 七文件经 docstring/入口/调用关系核对，拆为 Q014（active_provider_registry/provider_registry/connection_status 三基础文件）、Q014B（provider_services/model_settings/connection_actions 三服务消费者）、Q014C（operator_auth 单一会话认证职责）。全部原文件保留，975 文件各有唯一归属；批数 291 → 293。根认证是独立 49 行安全边界，避免混进 provider 状态治理；排序先基础、再消费者。
- 三个基础生产文件已完整检查。active registry 以 run_id+provider 对象身份清理原始 transport，原子操作已有锁，无需改动。session 字典由 AppContext/conversation_coordinator 的同一 application lock 保护，已定位实际 set/forget 调用，不把没有外部协调的任意并发脚本当作其生产竞态证据。状态 coordinator 的 revision/generation/last-good 与 close 边界保留；没有确认正确行为的其他缓存疑点不作猜测修改。
- 基线 `python -m pytest -q tests/test_connection_status_cold_start.py tests/test_provider_registry_app.py tests/test_task_runtime_coordinator.py --tb=short` → **退出 0，16 passed，2.67 秒**；三源及四测试初始加严 Any **158 条 / 4 文件**，q014-any-before.log。
- **bug：活动 Writer 的停止被可用性降级吞掉。** TaskCancelled 的有效契约是用户停止当前任务；project_writer_phase._select_next_writer → hooks/provider_failover_order → ProviderRegistry.failover_order 属于同一运行，不能将该异常当浏览器离线返回其他可用 provider。`python -m pytest -q tests/test_provider_registry_cancellation.py --tb=short` 修改前 **退出 1，1 failed / 1 passed，1.19 秒**；cancelled 回调没有抛出停止，RuntimeError 离线对照保持配置顺序。q014-cancellation-before.json。随后仅该活动 failover 边界重抛 TaskCancelled；未将后台状态探针或系统关闭清理的普通异常策略一概改成任务停止。
- 类型：动态 connector 入场为 object，已声明调用端口验证 status_key/probe_status/shutdown 回调，保留 getattr 动态代理与原 str/错误降级；_connection 返回实际 ModuleType。状态 JSON 对象键通过既有 object_fields 验证，与原 dict[str,...] 合同一致；值维持未知，消费者按需要收窄，无新 JSON 数值/深度/字段白名单策略。来源解码、data/payload/事件回调不再传播 Any。
- 冷启动测试全文件补具体 AppContext/Path/MonkeyPatch/ProbeRequest/回调类型；SimpleNamespace/Mock 改为有类型的 connector 和计数列表，保存调用次数断言。API providers 列表与 preferences 快照在边界校验，无跳过坏行。注册字典恢复仍保持同一对象和插入顺序，替代 patch.dict 的隐式 Any；local_config 直接定位同一原模块，避免私有重导出。runtime coordinator 测试补第二次 reserve_run 非空断言，未把 None 当 RunSnapshot。
- 独立 fixture 基线：临时在本目录使用 HEAD 原 registry/status 两源，运行上述同三文件 → **退出 0，16 passed，2.69 秒**；finally 恢复本轮字节并核对冲突/哈希，q014-fixture-baseline.json，restored_own_source=true。该结果证明类型 fixture 在原生产行为上仍成立，不将本轮修复当作旧期望的依据。
- **bug：已完成 Future 的同步回调仍持有外层状态锁。** concurrent.futures 的有效契约允许 add_done_callback 对已完成 Future 立即在注册线程执行；coordinator 宣明非阻塞状态协调，发布后通知又会进入 AppContext.emit 的 application lock。_publish 虽退出自身 with-lock，_submit 在注册 callback 时仍持有外层 RLock，造成通知中其他线程无法读取已发布状态，也允许与 application lock 形成反向依赖。
- `python -m pytest -q tests/test_connection_status_publication.py --tb=short` 对应生产改动前 **退出 1，1 failed，6.18 秒**，实际 `[False] != [True]`：标准 Future 在注册前完成、回调观察线程直到通知返回都不能完成 current()。探针只控制 Future 完成时机，没有修改锁或 current 实现；这不是泛化竞态猜测。q014-publication-before.json。将 add_done_callback 移出 _submit 的 with，保留 future 注册/版本/generation 原子更新及旧结果拒绝。close/revision 之间若发生切换，原 _publish 校验继续拒绝旧结果。
- 修复后 `python -m pytest -q tests/test_connection_status_publication.py tests/test_connection_status_cold_start.py tests/test_provider_registry_app.py tests/test_provider_registry_cancellation.py tests/test_task_runtime_coordinator.py --tb=short` → **退出 0，19 passed，3.49 秒**；八个完整文件的加严 Any 与 Ruff **退出 0**，诊断 **158 → 101 → 0**，无 cast/ignore/关门。
- 依赖扩大 `python -m pytest -q tests/test_connection_actions.py tests/test_zen_catalog_lifecycle.py tests/test_api_connections_do_not_enter_browser_failover.py tests/test_task_admission_races.py --tb=short` → **退出 0，28 passed，3.21 秒**；覆盖显式目录压过旧 probe、移除/禁用连接、普通控制关闭失败、API 排除与任务准入。当前完成仍 **16 / 293、35 / 975**，待最终调用方/类型门/diff 与拆分提交。

Q014 最终验证：

- 活动 transport 的真实停止调用方：`python -m pytest -q tests/test_task_submit.py tests/durable/test_scheduler_abort_handler.py --tb=short` → **退出 0，18 passed，2.01 秒**；原对象身份清理/abort 阻断行为保留。
- 首轮 scoped Pyrefly 实际 **退出 1，2 条空容器隐式 Any**，三生产类型门也各 **退出 1，同一条 model_settings.py:116 的 JsonObject → dict[str,object] 不变性错误**；不是既有失败。前者明确字符串选择模型列表和模型行列表的空容器类型；后者在 owning observe_catalog 只读入口采用 Mapping[str,object]，保留原 payload 对象引用，不用 cast、更改调用方或关闭检查。首次结果 q014-validation-first.json 未删除。
- 最终八完整路径的加严 mypy、Pyrefly、ty、Ruff **全部退出 0**；生产 `python -m mypy codey --python-version 3.11` **0，737 文件**；`pyrefly check codey --python-version 3.11` **0，0 errors，既有 35 suppressed/165 warnings 保留**；`ty check codey --python-version 3.11` **0**；`python -m tools.kernel_type_precision_gate` **0**。完整命令/输出 q014-validation.json，串行执行且源码/测试哈希前后一致，changed_during_checks=[]。随后仅只读 diff，不机械重跑没有运行期变化的通过用例。
- Diff 已核对：生产两文件，测试两份原文件/两份新增，记录两份；active_provider_registry.py 与 test_provider_registry_app.py 只检查验证、没有制造修改。两个独立 bug 按 provider registry 取消与 connection status 发布锁分别提交；本批最终状态待完整提交后更新。原 shared server/SQLite/CodeMode 等待办仍未解决，总 goal 仍 active。

Q014 取消与 registry 端口已提交：`3df4d47e`（fix(app): propagate cancellation from provider failover probes）。其余状态发布修复、状态 JSON/测试类型验收均已通过，单独提交；三生产文件批次完成后累计 **17 / 293 批、38 / 975 生产文件**。下一主批 Q014B 的 provider service/model settings/connection actions 三文件；Q014C 的 operator 会话认证仍完整留队。


Q014 状态发布已提交：`e5b44737`（fix(app): notify provider status outside the submission lock）。累计仍为 **17 / 293 批、38 / 975 文件**。

### Q014B：provider 服务、模型设置与连接动作（进行中）

- 主批三生产文件已完整检查；HEAD=e5b44737，恢复时三源及新增 test_provider_web_probe_submission.py 为本任务未提交修改，无其他工作区改动。基线 `python -m pytest -q tests/test_connection_actions.py tests/test_model_settings.py tests/test_model_settings_authentication.py --tb=short` → **0，31 passed，3.59 秒**。三源完整加严 Any 初始 **158 errors / 3 files**。
- **确定性 bug：executor.submit 失败永久卡住后台探针标记。** provider_status_payload → _schedule_web_probe 在提交前置 running；标准 ThreadPoolExecutor.submit 在线程启动失败时会抛 RuntimeError，并不存在成功运行的任务。没有运行任务就不能永久占用唯一探针名额，后续 HTTP 状态请求必须仍有机会恢复。新回归第一次 submit 抛 can't start new thread，第二次返回完成的 Future；改前 `python -m pytest -q tests/test_provider_web_probe_submission.py --tb=short` → **1，1 failed，1.25 秒**：两次请求仅一次提交；q014b-submit-before.json。生产修复在 submit 异常下持锁复位 running 并原样重抛，不改外层降级契约。新测试/冷启动最初 **0，11 passed，2.29 秒**。
- 类型边界：lazy registry 用已注解的真实模块满足精确 Protocol；warm/connect/fresh kwargs 用 TypedDict/Unpack 保留底层默认值唯一所有权。模型 JSON、动态 callback 和 status 回调使用 object 入场后校验，无新增 cast/ignore。settings 与 disabled-discovery 相同缓存读取归 _cached_catalog；缺少 model_payload 仍抛 TypeError，保留原捕获边界。
- 删除 use_model 缺锁兼容分支的依据不是单纯无引用：TaskState.providers 明确是 ProviderRegistry，其构造器无条件创建 _model_uses_lock；app 实际调用及直接 lease 测试使用真实 AppContext。保留初次允许性检查及锁顺序，回收路径两层 with 合并，不把检查挪到不同锁范围。
- 三完整生产文件加严 mypy **158 → 0，退出 0**；`python -m pytest -q tests/test_connection_actions.py tests/test_model_settings.py tests/test_model_settings_authentication.py tests/test_provider_web_probe_submission.py --tb=short` → **0，32 passed，4.48 秒**；`python -m pytest -q tests/test_provider_web_probe_submission.py tests/test_connection_status_cold_start.py tests/test_model_preferences.py::test_live_reviewer_lease_blocks_only_its_used_model_until_released --tb=short` → **0，12 passed，2.54 秒**。
- Ruff 首轮三处 import/SIM108/SIM117 失败均属于本轮，已按等价早返回/相同锁顺序/仅 import 排序修复，最终重验待执行。相关测试完整 Any、facade 默认值委托基线、三生产类型门及 diff/commit 尚未完成，不提前计入完成。


Q014B 验收与提交准备：

- connection_actions 全测试从无注解 SimpleNamespace/Mock 改为真实 AppContext（关闭 scheduler/memory）、有类型 ModuleType connector 与精确 callback；模型使用安排保持等价 Counter。logout 仍断言使用中拒绝、无执行，释放后执行时同一 app 锁禁止另线程非阻塞获取，没有把原锁断言删掉或放宽。错误隐私、显式 catalog state、shutdown 继续清理和不导入缺失包的全部断言保留。
- 新 facade 两测试验证省略参数由底层独占默认值、显式 kwargs 原样委托、返回对象身份与异常原样传播。在本目录临时使用 HEAD 原三源运行 connection_actions / facade / model_settings_authentication / 指定 lease node → **0，10 passed，2.43 秒**；finally 精确恢复字节并检查 restored_own_source=true。q014b-fixture-baseline.json。没有创建或复制 worktree。
- 最终直接五文件组 **0，35 passed，4.68 秒**。依赖按冷启动、Zen catalog、model_preferences、API reviewer 独立模型身份、consensus ports/cancellation、backend connection event node 扩大：`python -m pytest -q tests/test_connection_status_cold_start.py tests/test_zen_catalog_lifecycle.py tests/test_model_preferences.py tests/test_api_reviewer_uses_distinct_model_and_identity.py tests/test_consensus_provider_ports.py tests/test_consensus_cancellation.py tests/test_provider_connection_running_event.py::test_connection_event_stream_agrees_with_authoritative_state --tb=short` → **0，79 passed，6.90 秒**；无 skip。完整命令 q014b-validation.json。
- 补充 probe 边界：未完成 Future 阻止重复提交；失败 Future 释放名额；缓存失效后的新请求可再次提交；已完成不可用结果正确发布 unavailable。mypy 对同一模块布尔属性的前后断言误判 unreachable，改为持探针锁读取当前状态的有类型 helper，保留关键断言。最后 probe 两例 **0，2 passed，1.17 秒**。
- 首轮 scoped Pyrefly/ty 因 Protocol 的 Unpack 签名工具兼容、JSON 错误响应不变性和测试空容器失败，均为本轮引入，已修复；端口改明确可选关键字签名，默认以省略号表达可省略，不复制任何浏览器常量。生产 mypy/Pyrefly/ty 对最终修改均 **0**，mypy **737 文件**，Pyrefly 保留原 **35 suppressed / 165 warnings**。精度门 **0**。
- 七完整文件（主批三源、connection_actions / model_settings_authentication / 两新测试）加严 Any mypy **0**、scoped Pyrefly **0 errors**、ty/Ruff **0**；q014b-validation-final.json 的前后 hash 相同，changed_during_checks=[]。三源 Any 原158条已消除；test_model_settings.py 和依赖测试只执行验证，未声明全文件类型完成。
- q014b-validation-first.json、q014b-validation.json、q014b-validation-test-followup.json 保留实际失败过程；min-severity=warn 的一次诊断明确为原表达式多余 bool 转换，已在当前修改行等价删除。最终检查证据不覆盖或抹去旧失败。
- diff/暂存区核对仅本任务路径。按 provider 门面/探针恢复与模型设置/动作类型收敛两独立职责提交；批次仍待全部提交后计完成。全仓 pytest、浏览器 UI、未列明测试没有执行；原已登记 lazy import/SQLite/server/CodeMode 等待办未解决。


Q014B provider 门面与提交失败恢复已提交：`c021a9af`（fix(app): release web probe slot when background submission fails）。模型设置/连接动作的类型与唯一缓存读取入口单独提交，验收全部通过。本批完成后累计 **18 / 293 批、41 / 975 个生产文件**。下一主批 Q014C：operator_auth.py 的 process-local bootstrap/session 凭据和到期/一次性兑换边界；其后继续原 Q015 队列，不重开已完成 provider 文件的可选优化。总 goal 保持 active。


Q014B 模型/动作类型收敛已提交：`165a6efd`（refactor(app): type connection callbacks and share cached catalog reads）。

### Q014C：一次性启动凭据与会话认证（进行中）

- operator_auth.py 全文件检查：issue/exchange 用同一锁保证一份凭据只可成功兑换一次，截止时间采用 monotonic；未兑换成功不清除有效凭据。server 的 operator 路由和 service 的 HMAC 派生浏览器 token 调用关系已定位，与文档的一次性 UI 认证/服务替换后共享 session 契约一致。没有凭猜测改 session 格式、空值或 Cookie 解析策略，没有确认新的生产 bug。
- 四相关测试文件基线 `python -m pytest -q tests/test_operator_auth_rejects_non_ascii_credentials.py tests/test_operator_auth_blocks_unauthenticated_http.py tests/test_operator_bootstrap_before_ui_requests.py tests/test_operator_http_fixture_sends_complete_request.py --tb=short` → **0，27 passed，14.59 秒**。同五路径完整加严 mypy：源码已经零 Any，四测试 **111 条**，q014c-baseline.json。下一步只治理实际类型传播与测试边界，保持已有拒绝、重放、origin fence、并发兑换和 HTTP 完整写入断言。


Q014C 验收：

- 仅四测试文件类型修改。HTTP fixture 端口准确包含标准库地址类型，在 fixture 边界校验 IPv4 字符串 host；保留头+body 一次 sendall。动态 type/Mock 改 typed class/callback/counters；唯一有效路由使用关闭 scheduler/memory 的真实 AppContext，替代原无接口 object。JSON 从标准库 Any 改已验证 parse_json_object；WEB_DIR 从真实 owning http_plumbing 导入，未改期望状态/内容。
- 同原四文件实际 **0，27 passed，12.53 秒**，无 skip。完整源码加四测试的加严 Any **111 → 8 → 1 → 0**；最后一条来自 Executor.map 的 Iterable[Any] 将临时输入列表上下文推成 Any，将实际 token 列表先建立为精确 str 列表，无 cast/ignore。scoped Pyrefly/ty/Ruff 均 **0**，q014c-validation.json，前后 hash 一致。
- 依赖扩大 `python -m pytest -q tests/test_http_short_body_never_dispatches.py tests/test_release_control.py::test_browser_session_survives_service_replacement_but_bootstrap_does_not --tb=short` → **1，3 passed / 1 failed，3.37 秒**。三项复用当前 HTTP fixture 的短 body 用例通过；service 替换一例未进入认证，snapshot → runtime_digest → read_runtime 因发布库存失效而失败。只读核对实际 **877** / descriptor **876**，唯一缺项 codey/operations/browser_result_evidence.py（已有 `1292e668`），无过期项。完整命令/栈 q014c-dependencies.json，差异 q014c-runtime-inventory.json。
- 该失败分类为运行包构建/清单状态，Q014C 从未更改生产文件或运行 descriptor；没有把断言、skip 或认证行为改成迁就它。Q078/Q084 已登记正常 publisher 刷新和集成重验；这例不计通过。launcher/runtime.py 只诊断定位到 read_runtime，显式 Any 和全文件治理仍归 Q078，未宣称已完成。没有运行包复制成功，也没有复制工作区/worktree。
- 本批无新生产 bug、无可证明的死代码或相同职责重复；不制造生产修改。生产/配置未变，Q014B 刚通过的三生产类型门继续有效，不机械重跑。本批完整检查/直接必要验证完成，保留上述跨区域集成限制；累计 **19 / 293 批、42 / 975 文件**。
- 下一主批原 Q015 八文件含 844 行 API、HTTP plumbing/server、attachment/document 及 service/control，当前仅 AST/docstring 和调用关系路由，尚未深读/修改。进入前按实际职责拆分，保持全部原文件唯一归属与总范围。总 goal 仍 active。


Q014C 已提交：`1919ded3`（refactor(tests): type operator authentication and HTTP fixtures）。

### Q015：HTTP 基础、静态缓存与 SSE（进行中）

- 原八文件 Q015 按 AST/docstring/实际依赖拆六项：Q015 http_plumbing；Q015B attachments/documents；Q015C api（844 行单文件）；Q015D control_queries/control_service；Q015E service；Q015F server（648 行单文件，接收原 shared server 类型待办）。先基础后消费者，避免将控制执行、包 lease 和 HTTP 发送不同职责放同一批。源清单仍 975 文件唯一归属，批数 **293 → 298**，完成仍 **19 / 298、42 / 975**，没有扩大已完成数。
- http_plumbing.py 与直接 test_http_plumbing.py 已完整阅读；当前检查静态资源/ETag、origin 验证与 SSE 边界，未修改生产。其他依赖测试只定位相关 node，不将全部读取/执行混同全文件治理。候选问题：If-None-Match 按当前精确字符串比较弱 ETag；须先按 HTTP 有效协议建立改前行为复现，不能仅因测试期望修改。


Q015 验收：

- **确定性 bug 1：If-None-Match 没按弱比较。** 服务器发送 W/"opaque" 后，客户端给同 opaque 的强形式 "opaque"，静态 GET 应按 RFC 9110 §13.1.2 返回 304；原代码做全等比较，实际 200。**bug 2：把 opaque tag 内逗号当列表分隔符。** 合法 asset,part.js 的服务器返回 ETag 含逗号，§8.8.3 允许 opaque tag 包含它；原代码 split(",") 后连原样 tag 也无法匹配，实际 200。契约来自公开 HTTP 标准，不来自测试或另一实现。
- 生产修改前第一例 **1 failed，1.21 秒**；随后同一复现加逗号和 origin 基线 **1，2 failed / 1 passed / 5 subtests passed，1.25 秒**。两例均为预期304/实际200的行为失败，无导入或环境失败；q015-baseline.json、q015-etag-before.json 保留实际命令/栈。增加线性扫描，仅在引号外拆列表，比较时忽略合法 W/ 标记；未改变资源内容、ETag 发放、304 头和 cache-control。
- 初始四个已有静态行为基线 **0，4 passed，1.23 秒**。最终两完整文件加严 Any **25 → 2 → 0**，其中最初16条来自本轮 fixture 继承后 BufferedIOBase 读取类型，不冒充全是旧生产债务。源码 send_json 使用具体字典泛型保留直接序列化；SSE 接收只读 Mapping[str,object] 并由原 json.dumps 验证可编码值，非法 object 仍返回False。没有忽略注释或新类型断言。
- 重复收敛：两处绑定地址读取、结构检查、str 转换及异常返回完全一致，抽成唯一 _bound_address；调用方仍各自维护不同 host/origin 集合，未强行合并不同协议规则。提前在原代码验证五种 bind / 无origin / 同origin / 错端口 / 外域/外host。既有测试 fixture 改真实 BaseHTTPRequestHandler/HTTPMessage 最小子类，输出缓冲通过同一个 BytesIO 别名读取；首轮 ty 的可写属性收窄与方法参数名错误已修正。
- 最终 `python -m pytest -q tests/test_http_plumbing.py --tb=short` → **0，8 passed / 15 subtests passed，1.14 秒**，包括弱强、列表、逗号、空头、不同tag、W/*不当通配、未闭引号和静态缓存修改。无 skip。selected server WebAsset/四 origin/四 SSE node + operator_auth HTTP 文件 → **0，30 passed，16.79 秒**，完整明确node命令见 q015-validation-first.json；未跑整个 test_server.py。该大文件全脚本 Any 仍留 Q015F/Q008D，不宣称完成。
- 两完整文件加严 mypy/Pyrefly/ty/Ruff **0**，q015-validation-final.json；生产 mypy **0，737 文件**、Pyrefly **0，原35 suppressed/165 warnings**、ty/精度门 **0**（q015-validation-first.json）。最终只改测试 fixture，生产 hash 与这些调用方/生产门验证时相同，不重复未失效结果。前后 hash 稳定。
- 为证明新 fixture 与 address 重构基线独立成立，临时还原本目录 HEAD 单个 http_plumbing 源，实际运行五既有/绑定基线 node → **0，5 passed / 5 subtests passed，1.24 秒**；finally 精确恢复已验证生产字节，q015-fixture-baseline.json，restored_own_source=true。没有复制工作区/worktree。
- 入口/引用分析未发现可证明能删除的公开函数，__all__ 保留。diff 已核对，只涉及 HTTP 源、直接完整测试与两份记录。本批完成后累计 **20 / 298 批、43 / 975 生产文件**；下一主批 Q015B attachment_routes.py / documents.py。原打包库存失配及其他跨区域待办仍未解决，总 goal 仍 active。


Q015 已提交：`92ef82dd`（fix(http): apply weak comparison to complete conditional entity tags）。

### Q015B：附件与会话文档（进行中）

- 两生产文件已全读；有效职责/范围来自 workspace_documents_and_terminal.md 的服务端会话绑定、工作区包含性、2 MiB/显式状态和研究笔记归属契约，以及 frontend document_content.ts 的 representation 字符串接口。没有修改路径包含性、祖先会话附件继承、图像支持能力或大小预算。
- **bug：representation 容器值触发 hash 异常。** HTTP JSON 能提供任意 JSON 值，合法表示仅 reference/content/image；_file_document 接受 object 并为所有域外值定义 document_representation_invalid，非法值是否可哈希不应改变验证路径。列表/对象却在 set membership 先抛 TypeError，绕过400。新增八输入回归在改生产前实际 **1，4 failed / 4 passed，1.35 秒**，标量拒绝对照通过、四容器出现 unhashable TypeError。独立依据是有限字符串表示的接口要求与 JSON 请求边界，不将生产当唯一正确答案。q015b-baseline.json。
- 原三文件基线（document_preview/attachment_capabilities/input_attachments）**0，39 passed，3.47 秒**；有既有 WinError32 RAM 清理警告，路径指 test_idle_attachment_input_dispatches... 的 .durable.sqlite3.lock，不虚报无警告。首次记录脚本在打印非UTF8系统错误时遭 GBK UnicodeEncodeError，未保存尾部证据，已改UTF8 stdout并逐命令持久化，重新实际运行获得上述完整记录；该脚本输出失败不当成行为复现。
- 增加显式字符串检查后，同三文件加八输入回归 **0，47 passed，4.12 秒**，同一清理警告仍在。两完整生产源码加严 Any **68 → 0**；五完整路径含相关测试原 **192** 条，测试迁移尚未完成，不计整个批次完成。
- documents 采用仅含实际只读能力的 context Protocol，UI rows 在边界验证字符串键/序列，项目 path 验证为持久 UI 契约字符串；不新增 JSON 深度预算或复制整份状态。附件 refs 和 durable 消息行相同的列表/字典验证归唯一 _attachment_rows，保留先验全组结构再读取、原错误文字与 refs 上限4。全量调用方/type gates/diff 尚待完成。


Q015B 验收与提交准备：

- document_preview 整个脚本改为 typed 最小只读 context，包含明确 sessions/projects/KnowledgeStore，保持对同一 project 行的绑定修改；base64、候选路径列表和 read_note 可空返回在边界收窄，无过滤坏行/放宽断言。capabilities 测试的 connector factory 参数/返回明确，API_CONNECTIONS 使用真实 owning catalog 同一个字典，不通过私有重导出传播 Any。
- 测试夹具独立基线：临时使用 HEAD 原 documents/attachment_routes 两源码，`python -m pytest -q tests/test_document_preview.py tests/test_attachment_capabilities.py --tb=short` → **0，20 passed，1.75 秒**；finally 精确恢复并核对 restored_own_source=true，q015b-fixture-baseline.json。没有复制目录/worktree。
- 最终 `python -m pytest -q tests/test_document_preview.py tests/test_attachment_capabilities.py tests/test_document_representation_validation.py tests/test_input_attachments.py --tb=short` → **0，47 passed，4.16 秒**。相同八输入/核心断言已全通过；覆盖文本/图像/reference、binary/大小/路径/symlink拒绝、缺失/歧义、研究归属、捕获图像能力、上传不可变/来源伪造拒绝、历史附件继承/运行交付。没有 skip。Input attachments 只作调用方验证与引用路由，未改动或宣称全脚本 Any 已治理；浏览器 UI、git worktree 场景不在本批执行范围。
- 仍有基线同样的 RAM 清理 WinError32（task idle attachment 的 .durable.sqlite3.lock），已独立路由 Q018 context 生命周期，尚未证明竞态/生产 bug，没有隐藏或为绿门改断言。必要行为用例通过与清理警告分开报告。
- 两源码完整 Any **68 → 0**，加三完整测试 **192 → 0**，加严 mypy/scoped Pyrefly/ty/Ruff 全部 **0**。生产 mypy **0，737 文件**、Pyrefly **0，原35 suppressed/165 warnings**、ty/精度门 **0**；q015b-validation-first.json 持续逐命令写入完整命令/结果，全部完成，changed_during_checks=[]。
- 引用/语义分析：两个生产文件的公开响应/准备函数均有 API/server/控制提交或直接测试用途，不以搜索无外部引用删除私有或公开入口。重复仅合并同为列表+字符串键dict的附件结构校验，正常 request 与 durable fallback 分别保留自己的错误信息、数量上限和 from None，先全部结构验证再读取，未混合存储内容验证与模型能力策略。
- 已检查 diff/暂存路径；按 documents 输入 bug+类型与 attachment routes 类型/行校验收敛拆独立 commits。本批待提交完毕再计完成，仍 **20 / 298、43 / 975**。Q018 清理警告和 Q078 库存失配保留为独立未完事项，不将其误报修复。


Q015B 文档非字符串表示修复与文档类型边界已提交：`c9b7f811`（fix(documents): reject non-string representations before membership lookup）。附件 routes 的独立类型/结构校验收敛单独提交，全部必要验收通过；本批完成后累计 **21 / 298 批、45 / 975 生产文件**。下一主批 Q015C 为 api.py 单个844行请求/提交门面，先建立相关行为/类型基线再改动；Q015D 控制服务、Q015E 生命周期、Q015F server 继续留队。全仓总 goal 仍 active，未完成事项未缩减。


Q015B 附件校验类型收敛已提交：`b433873a`（refactor(attachments): validate typed rows at request and history boundaries）。

### Q015C：HTTP API 请求与提交门面（进行中）

- api.py 844行已全读。现有公开函数分别被 server/control 使用，未凭 __all__ 漏列就删入口。保存 local 配置仍只探一次，changes restore 使用项目 writer lease，审批/附件/显式 entry auth 调用关系已确认；无证据不猜改这些正确路径。完整单源加严 Any **33 条**，主要来自 legacy local/changes/review 返回和 pending question，不是缺参数注解。
- **bug：HTTP resume 缺省 instruction 被改成文字 None。** 公开 ResumeRun DTO 的 instruction: str=""，当前活跃客户端允许省略该字段；省略和显式空默认值必须同义。HTTP 读缺失值 None 后先 str，产生伪造补充任务 "None"，绕过同入口已有的空说明默认续跑分支。该不变量来自可选字段契约，不仅拿另一个实现或当前测试断言当正确答案。
- tests/test_task_input_contract.py 全脚本 Mock/无类型 lambda 改精确七主参数+有类型记录回调，保留源 run 转发和拒绝旧 continue_task；新增省略/空/全空白/显式补充四输入。在生产未修改时 `python -m pytest -q tests/test_task_input_contract.py --tb=short` → **1，1 failed / 7 passed，1.27 秒**，省略实际 calls=[("None","run-1")]，预期默认说明；其他对照通过。q015c-baseline.json。
- 相关五文件冷启动/entry auth/completion output/edit/local protocol 基线 **0，33 passed，15.68 秒**，完整命令同证据。当前仅测试脚本和记录修改，生产修复/类型治理/最终验证尚未执行；不计完成。


Q015C 最终验收：

- **第二个独立确定性 bug：resume 源 ID 隐式转字符串。** 公开 ResumeRun.source_run_id 明确必须是非空 str，非字符串原本被 str(... or "") 强制变成看似合法来源，然后提交；HTTP 结构化输入应保持此类型契约，不让 bool/数字/列表/对象变成 run ID。新增八 JSON 输入在这项生产改动前 **1，4 failed / 4 passed，1.28 秒**，True / 42 / 非空列表 / 非空对象都实际200并调用submit，而应400且不调用；q015c-resume-source-before.json。仅在入口验证 str 后 strip，不新增 run ID 命名或格式限制。空容器/False/None仍原样拒绝，合法 run-1 兼容。
- 原17个最终契约（含首个缺省说明bug、八非法source、新增真实 durable question 阻断基线）**0，17 passed，1.25 秒**；q015c-contract-final.json。首个 omitted instruction + entry auth/completion/edit/local protocol 组先前 **0，41 passed，16.07 秒**；图谱/notes/local/Review HTTP/review source/required_tools 指定组 **0，39 passed，15.10 秒**；ChangesGetTests/RestoreLeaseTests **0，2 passed，1.38 秒**。完整明确路径/node 命令 q015c-validation-first.json、q015c-lease-validation.json，未全量收集仓库。
- source ID 修复后增加明确的 input/output 契约和 durable question：`python -m pytest -q tests/test_task_input_contract.py tests/test_task_input_and_output_contracts.py tests/durable/test_question_authority.py --tb=short` → **0，33 passed，5.73 秒**，包含真实储存的pending question保留同一身份且阻止submit；无skip。sourceID改动不影响已通过的 local/graph/restore 独立路径，不机械重跑它们。
- 完整 API 与 task_input_contract 两文件加严 Any **33 → 13 → 1 → 0**。legacy local/graph/changes 输出先入 object 再检查 string-key dict，响应对检查status+payload；review loader 验证真实 ReviewResult，反射只接受 dataclass 实例，保留原 asdict 转换及异常、不加 JSON/深度/字段白名单。dataclass stdlib 的 class/instance 联合在第一次类型尝试未正确收窄，已用有运行时证据的 TypeGuard/Protocol 描述 Field[object] 元数据；没有 cast/ignore 或把整个 payload 换无约束 object。
- source ID 增加校验导致 Ruff 81 statements，未增加 noqa；将现有 pending-question 的读取及 dict 运行时收窄归独立类型边界 _pending_question，保留 None/409/身份语义。新 question fixture 的 metadata 字面量首次引入1条 Any，已明确定义为 dict[str,object]。q015c-validation-resume-final.json 的 Ruff失败、q015c-validation-final-all.json 的该次mypy失败保留；最终 q015c-contract-final.json 加严 mypy/Pyrefly/ty/Ruff 均 **0**。Pyrefly 的原5个多余bool/str警告单列，不声称零warning。
- 最终生产 mypy **0，737文件**、Pyrefly **0，原35 suppressed/165 warnings**、ty/精度门 **0**，q015c-validation-final-all.json（最终只再明确测试 metadata 类型）；代码/测试检查中hash稳定。旧四用例的typed回调在 HEAD 原API上 **0，4 passed，1.24 秒**，真实 question guard 在同一原API上 **0，1 passed，1.19 秒**；q015c-fixture-baseline.json、q015c-question-baseline.json 均 finally 精确恢复源字节，没有 worktree复制。
- 没有确认可删的公开/动态入口或相同职责实现，不为制造重构移走844行门面的无关功能。scope 中的共享 test_server 等只按指定node验证，未宣称全脚本Any治理；跨模块模型/审查/source authority的 legacy Any 仍在其owning批次，不将本文边界隔离视为这些模块已完成。
- 本批两生产bug的入口条件都保留改前失败和改后关键断言证据，diff和暂存区已核对，仅API、完整契约测试与两记录。本批完成后累计 **22 / 298 批、46 / 975 生产文件**。下一主批 Q015D control_queries.py / control_service.py；原 Q018临时锁清理警告、Q078生成库存等未解决项继续留队。总 goal仍 active。


Q015C 已提交：`cb43afb5`（fix(api): preserve resume field types and the default instruction）。

### Q015D：控制观察与控制服务（进行中）

- control_queries.py / control_service.py 已完整检查；外部控制 implementation 当前文档要求 get_run_result 提供实际运行/候选摘要，record_review 绑定该摘要，摘要函数明确承诺拒绝符号链接与不完整证据。完整两源码加严 Any 初始 **515 条**，q015d-baseline.json。命令侧 execute 已串行 durable/control barrier，wait 用 change_sequence 防丢通知；未凭猜测改锁/收据去重或状态策略。
- **确定性 bug：摘要扫描覆盖失败仍返回标识。** 缺失目录和普通文件被 os.walk 当空树，普通包含目录的链接被静默滤掉，os.walk 默认吞目录 PermissionError。当前有效函数契约是有界且覆盖完整才给候选身份，不能把看不到内容的路径等同空工作区。新增真实临时树/目录链接与定点 scandir PermissionError 回归；改生产前 **1，4 failed / 1 passed，1.22 秒**，四种不完整输入均未抛错而成功返回摘要，有效空目录对照通过。q015d-baseline.json；这是扫描覆盖缺口，无锁环或竞态证据。
- **确定性 bug：回答裁剪标志没计消息分隔符。** 两条 assistant text 各8000字符，渲染加换行合计16001、answer实际只保留16000，但 answer_truncated=False。元数据应描述实际暴露的有界文本是否裁剪，不应仅计正文成员长度。原生 EntryDraft + 真实 AppContext/task 复现，前后边界16000/16002对照保留。改生产前 `python -m pytest -q tests/test_control_evidence_budget.py tests/test_control_run_snapshot_bounds.py --tb=short` → **1，1 failed / 3 passed，1.75 秒**；目标flag断言实际False/应True，q015d-snapshot-before.json。
- 原嵌套 evidence byte budget 基线 **0，1 passed，1.08 秒**；将其SimpleNamespace改真实 AppContext/task 与原生EntryDraft，保持1.8MB嵌套证据、有标记裁剪和原128KiB断言，新 fixture在原生产上仍通过。源尚未修改，不计本批完成；优先完成只读查询再处理命令侧515条中的剩余源，发布库存依赖的测试仍须明确区分环境状态。


Q015D 查询侧独立验收（主批未完成）：

- 候选摘要要求根是实际目录，使用 os.walk onerror 将被吞的目录错误转为 EVIDENCE_UNAVAILABLE；对声明覆盖的普通目录链接明确拒绝，仍保留 IGNORED_DIRS/.tmp- 的原排除范围，不跟随外部链接。未修竞态、未添加缓存或修改已验证文件内容的哈希算法。有效空目录、正常内容修改导致摘要变化、忽略目录不影响摘要、已有文件链接拒绝均保留。
- 回答仅 join 一次，用同一完整渲染文本的长度计算 answer_truncated，再保留原末16000字符，消息顺序、答案文本和16000上限不变；16000/16001/16002 三边界回归保留。
- task_for 使用真实 TaskCheckpoint，查询 context 明确为 AppContext；问题/审批/持久 control review/执行通道在边界验证序列和 string-key dict。evidence 的 string ID/integer 参数与有效公开 command catalogue 类型一致，数量/8192预览/96KiB总预算不变；未引入新JSON深度/裁剪政策。反射记录输出先入object再验证dict，保留原asdict。
- 新增有效 candidate=True 的项目范围/空projection对照，执行输出文本/退出码/完整状态/截断和跨run拒绝测试；嵌套1.8MB evidence 仍满足原128KiB断言，没有减小fixture绕过预算。
- `python -m pytest -q tests/test_control_candidate_digest_coverage.py tests/test_control_evidence_budget.py tests/test_control_run_snapshot_bounds.py --tb=short` → **0，14 passed，2.14 秒**，无skip，q015d-query-validation-final.json。四完整路径加严 mypy/scoped Pyrefly/ty/Ruff均 **0**；单query源码初始 **114 → 1 → 0**，同组初次1条来自asdict三元分支的库Any，已明确对象边界与运行时收窄。
- 查询源码的生产 mypy **0，737文件**、Pyrefly **0，原35 suppressed/165 warnings**、ty/精度门 **0**，q015d-query-validation-first.json；最终只扩展测试，源码未变，hash稳定。ControlService未改，完整加严 Any 当前 **396 条**（原401；task_for精确类型隔离了5条传播）；q015d-service-any.json。两源码初始总515，不将query的零Any虚报为整个控制批次已完成。
- 只读查询的回答修复/类型与候选coverage修复可以独立验收，按两个本地commits处理；先暂存不含digest修复的已验证版本，再恢复当前最终字节提交digest部分。每个中间版本都实际执行明确范围检查，不创建或复制worktree。主批status保持in_progress，完成统计仍 **22 / 298、46 / 975**，查询文件另计已验证部分。
- 其余控制命令/待机/去重/授权预算与本轮未更改的control_conversations/control协议等仍待后续治理；有发布清单依赖的external_control/release集成尚未重跑，不报通过。下一步继续主批control_service.py的396条Any和直接控制服务行为基线，不回到已完成API/provider文件做可选优化。


Q015D 回答裁剪和完整query类型边界已提交：`c2bf9364`（fix(control): count rendered separators in answer truncation）。实际暂存的中间源码保留原digest函数且没有新增未使用helper/import；独立运行 evidence/snapshot两文件 **0，7 passed，1.73 秒**，同源加两完整测试的加严Any/Pyrefly/ty/Ruff均 **0**，q015d-answer-commit-validation.json。随后 finally 精确恢复最终digest修复字节。

最终fixture独立基线：将本目录单个query源临时替换为cb43afb5原版，实际执行三evidence（含新跨run通道校验）、两非失败回答边界、candidate空projection和三原有效digest场景 → **0，9 passed，1.47 秒**，q015d-query-fixture-baseline.json；finally恢复当前最终字节，其sha256与此前14例及完整类型验证时一致。未将四个旧coverage错误或旧16001裁剪错误计为通过，也没有复制worktree。

候选摘要coverage修复已按独立变更准备提交；diff只有根目录存在性、onerror、声明覆盖内目录链接拒绝以及七场景测试。此后主批仍进行中，下一步唯一主线为control_service.py 的396条Any与直接行为基线；control_queries已完成的检查不因可选优化重新打开。全批完成数仍 **22 / 298、46 / 975**，另有本批1个查询源已单独验证。原Q008D/shared server、Q018临时锁清理、Q078发布库存及其他登记事项全部保留，总goal继续active。


Q015D 候选覆盖修复已提交：`50ff98c3`（fix(control): reject incomplete candidate digest coverage）。

Q015D 控制服务最终验收与暂停：

- 上轮有9个实际本地commits及行为证据，属于有效进展。恢复时HEAD=50ff98c3、工作区干净；本轮未完成重构后，用户将范围明确为“这个阶段做完commit好就暂停”。本轮仅完成当前Q015D，不进入Q015E。后来出现的 docs/codey_deep_audit_and_benchmark.md 和 docs/codey_function_level_upgrade_guide.md 为其他工作，保留且不暂存。
- 新增直接ControlService契约脚本使用真实AppContext与原durable owner，关闭scheduler/memory；原源码初始 **0，11 passed，1.89秒**。覆盖创建/入场幂等、内容冲突、历史无被动控制条目、观察超时不取消、暂停代次及原回执、身份/操作员权限、未结算candidate拒绝验收、非法预算不创建会话。没有创建或复制工作区/runtime快照。
- 全文件参数/回调收敛为AppContext、TaskCheckpoint、CommitResult/DurableMutation、ApiRunSelection；legacy配置/收据/队列/问题/health返回先作为unknown读取，再校验记录/序列/实际消费的标量。未加cast/ignore或降低检查强度。保留task旧代次的int转换支持；number边界保留int|float，不把巨大合法JSON整数先转float，既有越界控制错误得以保留。
- _prepare_message 与 _send 的输入ID完全同为conversation_id/request_id同序数组、同前缀/40位fingerprint，无状态副作用，归唯一 _input_identity；创建会话用actor/request、24位的不同策略未混合。未删无法证明不用的公开方法。
- release接口需要非空Path，而AppContext支持ephemeral None；仅在release路由验证home并提供只读的home/instance_id/allowed_roots视图，未改发布模块或启动/切换流程。三个路由均用原接口契约验证转交字段，无实际快照、发布、部署或激活。
- 扩展基线首次 **1 failed / 16 passed，2.06秒**，根因是本轮新测试错误地认为get_upgrade需operator；独立公开Command目录明确其默认readonly、operator=False，而prepare/activate显式operator=True。依此修正测试：代理可读升级状态，代理准备发布仍拒绝；未改生产权限或放宽断言。首次结果保留 q015d-service-fixture-baseline.json。修正后的全部17例在HEAD原control_service.py实际 **0，17 passed，2.00秒**，finally恢复本轮源字节，q015d-service-fixture-baseline-final.json。
- service完整加严Any **396 → 26 → 2 → 0**；两源起始515条现已全部消除。中间错误为本轮类型工作，未归为历史问题：object数值收窄、Path可空release端口、legacy问题返回/显式字典泛型。最终检查前消除两处已是int的重复转换，Pyrefly回到原warning数，不隐藏新增警告。
- 最终 `python -m pytest -q tests/test_control_candidate_digest_coverage.py tests/test_control_evidence_budget.py tests/test_control_run_snapshot_bounds.py tests/test_control_service_contract.py --tb=short` → **0，31 passed，3.46秒**，无skip。主批两源码+四完整测试的加严mypy/Pyrefly/ty/Ruff均 **0**；生产 mypy **0，737文件**、Pyrefly **0，既有35 suppressed/165 warnings**、ty/精度门 **0**。完整实际命令/每批退出码/输出 q015d-final-validation-complete.json，串行检查、hash稳定且提交前核对匹配；之前167warnings的中间结果保留 q015d-final-validation.json。
- Q078发布清单失配仍导致原runtime依赖的external_control/release集成不可用，本轮没有重跑或记作通过；这里验证的是直接控制服务与只读查询及release路由接口。control_conversations/input_queue/durable.control等legacy Any仍归Q019/Q051，已记录具体边界与未完成性质；本批零Any不代表依赖或全仓零Any。
- 两生产文件完整验收完成，累计 **23 / 298批、48 / 975生产文件**，975文件唯一归属未变。Q015E保持未开始；全仓目标尚未完成。按用户指示，在本阶段代码与进度提交完成后暂停，恢复须由用户另行要求。


Q015D 服务类型与输入身份收敛已提交：`c3baf7e2`（refactor(control): validate service boundaries and share input identity）。本次进度提交记录该hash与验收结果；本阶段所有代码已提交。仅记录一个未确认语义问题：grant_control更新expires_at但保留创建时的max_seconds；该展示字段代表原始预算还是最近续期值尚无独立读取方/规范依据，实际到期仍使用expires_at，未猜测更改。此项不阻止现有契约的类型/行为验收，只有新契约或复现证据出现时才重开。

按用户“这个阶段做完commit好就暂停”暂停总goal；本阶段完成，整个仓库仍未完成。当前 **23 / 298批、48 / 975生产文件**，Q015E未开始，所有原已登记限制保持。其他两份未跟踪文档保留。
