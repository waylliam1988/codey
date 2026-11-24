# 代码质量治理进度（2026-11-22）

## 目标及执行约束

目标持续覆盖本仓库自有 Python、TypeScript/JavaScript 源码、工具和测试；排除依赖、第三方 QuickJS、生成的网页资源及构建产物。尚未完成全仓治理，不能据本记录推断仓库没有其他 bug。

当前状态：在 master 的 `3e330232` 恢复总 goal，已完成 Q030D。Q030C 的查询身份修复已验证，研究调用方待跟进。下一批为 Q031。保留历史验收，复核后续提交实际影响的范围。

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


### 2026-11-23 恢复与 Q015E 服务生命周期

- 用户已明确继续；总 goal 保持 active。HEAD=21601416，恢复时工作区干净。暂停期间四个外部提交新增20个生产脚本，按职责追加7批；当前完整范围995文件/305批，唯一归属校验通过。原23已完成批中Q004/Q015C有生产文件实际改动，改为部分完成、待重新核验；21批当前仍完整完成，原历史证据保留。当前完整完成批次覆盖42个生产文件，另有部分批次中6个已验证文件，共48个当前已验证文件；旧48个完整批次覆盖扣除两个变动文件后为46个，其中4个仍属于重新打开的批次，不混计。
- Q015E 唯一主批：service.py 已完整读，publication测试已读；先建立基线，必要复现后修复，完成资源/凭据/回调类型边界治理和限定测试，再本地提交。新增区域只做路由、未冒充深入检查。Q004/Q015C重验排在Q015E之后、Q015F之前。
- test_control_service_startup.py 已为入口/快照副作用分析读过，整文件Any治理未完成，归控制客户端批次；本轮避开复制runtime的用例。file_lock.py 为确认FileLease接口阅读，完整Any核验归原存储锁批次，不在此声称治理完成。


Q015E 最终验收：

- service.py 全文件及两份完整测试已检查。独立依据：service 文档规定state home由OS lease确定唯一所有者；close显式仅在context清理仍活跃时保留所有权。资源已经关闭后，发现登记文件读/删失败不能让无运行资源的服务永久占有writer lease；I/O错误仍须暴露，不得删除无法确认属于本实例的文件。原实现把release放在可抛错发现I/O之后，异常使release永不执行，属于确定性异常清理缺口，无竞态证据。
- 两输入：损坏service.json和本实例登记unlink PermissionError。生产修改前同一回归 **1，2 failed / 5 passed，2.27秒**，均因finished service retained its writer lease断言失败；损坏读取和删除拒绝都已实际走到生产close。最初fixture误用enable_memory参数导致另一个环境/测试失败，已按AppContext真实memory_enabled参数更正；该次保留q015e-close-before.json，只有最终q015e-close-before-final.json作为干净复现证据，未将导入/参数错误当bug。
- 发现I/O放try/finally；ctx完成后无论登记读/删成功与否均释放lease并清None，原StoreCorruption/PermissionError继续上抛，原字节保留。未清完ctx仍抛RuntimeError且保留lease、第二次获取仍失败；匹配登记删除、继任者保留、缺失登记和重复close对照通过。
- 资源字段为真实AppContext/CodeyHTTPServer/ControlService/PublicEventFeed/FileLease可空类型；unsubscribe精确Callable；凭据TypedDict及运行时长度/字符串TypeGuard，保留旧文件附加字段、不重写；event callback明确字典边界转交原public feed，未改变公共事件筛选/编码规则。延续回调统一使用验证活动context的getter；不改正常提交参数。发布锁、8次WinError5重试和失败传播断言保留。没有证据可删的公开入口，也没有强行合并语义不同实现。
- 直接最终：`python -m pytest -q tests/test_service_publication.py tests/test_service_lifecycle.py --tb=short` → **0，20 passed，3.33秒**，无skip。真实临时home启动/凭据/浏览器bootstrap/operator与agent权限/错误实例/激活/排空/关闭均实际运行，没有runtime副本或worktree。凭据非法类型/长度不覆盖文件及额外字段兼容性也通过。
- typed publication fixture用真实ControlService和AppContext替代SimpleNamespace；全部非bug对照在HEAD原service.py实际 **0，18 passed / 2 deselected，3.20秒**，finally精确恢复当前源码字节，q015e-fixture-baseline.json。未将两条bug回归排除结果算为通过；最终版本20项已全部执行通过。
- 依赖：`python -m pytest -q tests/durable/test_services_lifecycle.py tests/test_control_runtime_ownership.py tests/test_control_service_contract.py --tb=short` → **0，25 passed，3.26秒**，无skip/清理警告，q015e-dependencies.json。release复制/发布/切换与基于旧runtime清单的transport集成仍未执行，不声明覆盖。
- 三完整文件加严mypy Any初始两文件 **138条诊断 → 中间10条 → 最终0**；新增测试也已纳入零Any最终检查。mypy严格Any/scoped Pyrefly/ty/Ruff退出均0，生产mypy **0，749文件**、Pyrefly **0，35 suppressed/168 warnings**、ty/精度门0；warnings是当前读数，不套用旧165或声明零警告。完整逐命令/退出码/原始输出/稳定hash见q015e-validation-final.json；初次失败保留q015e-validation-first.json，没有降低门禁。
- 当前 **22 / 305批完整完成，43 / 995生产文件属于完整完成批次**；另有部分批次中6文件已独立验证，共49个当前已验证文件。Q004/Q015C因外部变更仍部分完成，下一步优先重验两个实际改动文件，再进入Q015F server。初始全仓机器门禁、Q008D/shared server、Q018锁清理、Q078库存、Q052 SQLite等未完事项继续留队，总goal active。


Q015E 已提交：`73a12095`（fix(service): release completed ownership when discovery cleanup fails）。工作区提交后干净。

### Q004 外部路径边界变更重新核验（完成）

- 只重新打开实际改动的paths.py；当前完整文件和path_hash的raw模式调用关系已检查：semantic_definition版本验证、runtime edit/raw前提检查使用preserve_newlines=True；其余snapshot/tracker仍用False默认。没有将raw模式强推给所有调用方。
- 当前基线workspace paths+edit fidelity **0，45 passed/2 skipped，2.77秒**，q004-revalidation.json。新增与公开docstring一致的LF/CRLF/CR/混合BOM四个边界，证明True保留原内容、缺省/False仍保持universal-newline语义；不改生产实现，不宣称发现新bug。
- 最终 `python -m pytest -q tests/test_workspace_paths.py tests/test_edit_text_fidelity.py tests/test_followup_semantic_definition.py --tb=short` → **0，49 passed/2 skipped/4 subtests passed，3.01秒**。两skip为Windows无O_NOFOLLOW原测试，不新增skip，不隐藏失败。两完整文件加严Any/Pyrefly/ty/Ruff全0；逐命令退出码/输出见q004-revalidation-final.json。当前未修改生产源，Q015E已通过的749文件生产类型门不因测试新增机械重跑。
- Q004恢复完成；当前 **23 / 305批完整完成、48 / 995生产文件属于完整完成批次**，另有部分批次中2个已独立验证文件，共50个当前已验证文件；不将部分批次的verified混计完整完成。下一唯一主批Q015C重新核验API外部intent推断转交变更，之后Q015F。


Q004 重新核验已提交：`135d8e7a`（test(paths): verify raw newline hashes and edit caller boundaries）。

### Q015C 外部 API 入场 intent 变更重新核验（完成）

- 外部变更仅为run_submit_response末尾7参数元组中的intent：新的auto任务转交entry_auth.inferred_intent，显式intent和resume仍按原值。定位唯一改动与相关TaskSubmitter/EntryAuth契约；未为可选优化重新处理无关API行为。当前原范围基线 **0，26 passed，2.36秒**，q015c-revalidation-baseline.json。
- 六个新增边界直接使用有类型captured EntryAuth：缺省/显式auto采纳hybrid、无推断保留auto、显式project/research不覆盖、resume不改auto；同时断言denied project.write、严格研究/开源要求、无写入要求和源run身份原样转交，防止仅改模式而扩大权限。未修改生产或放宽已有断言，没有把测试新增声称为新的生产bug修复。
- 最终 `python -m pytest -q tests/test_task_input_contract.py tests/test_conversation_entry_auth.py tests/test_edit_api.py tests/test_task_input_and_output_contracts.py::test_api_passes_explicit_contract_and_rejects_invalid_contract tests/durable/test_question_authority.py --tb=short` → **0，32 passed，2.47秒**，无skip。两完整文件严格Any/Pyrefly/ty/Ruff全0；Pyrefly原5 warnings保留，q015c-revalidation-final.json。执行但未修改的共享测试不宣称整文件Any完成，归原owning批次；test_conversation_entry_auth.py旧Mock/无注解在其测试支持队列仍未治理。
- Q015C恢复完成。当前 **24 / 305批完整完成，49 / 995生产文件属于完整完成批次**，另有部分批次中2文件已独立验证，共51个当前已验证文件。两个实际被外部修改的已完成源已重验；生产三个类型门已在当前749文件通过后无生产变更，不机械重复。下一主批Q015F server.py与其测试支持债务。


Q015C 重新核验已提交：`5fff26b7`（test(api): verify inferred intent preserves explicit and resume authority）。

### Q015F HTTP 路由、控制绑定与事件流（进行中）

- server.py 648行已完整检查；tests/test_server.py为6727行跨多生产区域共享脚本，仅定位/执行明确类和node，不假称全脚本治理。原Q008D所欠整脚本Any仍未完成，不能将本批生产零Any等同支持脚本清零。
- 当前server完整强Any基线 **194条**，q015f-any-baseline.json。原认证/短body/显式owner/http plumbing限定基线 **0，30 passed/15 subtests，12.17秒**，q015f-behavior-baseline.json。
- bug一：CodeyHTTPServer先调用父构造绑定socket，再校验前端目录；校验抛ValueError时子类没有关闭已经绑定的监听器。资源构造失败不得留下活跃监听socket是可论证的资源所有权不变量。测试截获真实bound socket并保留引用，构造异常后fileno仍有效；测试finally清理自己的截获资源，避免靠GC掩盖泄漏。
- bug二：/api/run公开对象输入契约与run_submit_response入口明确将非对象返回400 invalid json，但HTTP的fork准备和UI控制先访问body.get；认证后null/true/42/string/空list/非空list均实际500。对象DTO/HTTP入口契约，而非测试断言偏好，要求无效输入在边界拒绝并不入场。修改前 **1，7 failed，4.84秒**，q015f-bugs-before.json：socket断言失败1、同输入500而应400六项。后来将构造测试原样拆至test_server_construction.py以支持独立commit，没有删除失败用例。
- JSON在已注册object POST路由前收窄，bootstrap与control的既有认证/错误响应和未知路由404保留，空body仍映射{}。请求先完成认证/长度/完整body检查，再解码和shape验证，不读取无效payload的context或提交任务。构造泄漏修复暂留独立后续，当前先验收JSON与完整类型边界。
- 真实CodeyHTTPServer资源、Actor、lifecycle、public feed和ContinuationSubmitter取代Any；socket超时字段精确化。ty拒绝把BaseServer可写字段直接窄化（q015f-json-validation-first.json保留），已改为绑定处运行时isinstance验证；没有ignore/cast或降低门禁。_get_state按有效文档验证AppContext，原测试object占位改为真实context，保留身份相等断言并新增缺失context拒绝。四个owner测试在HEAD原server/api上 **0，4 passed，1.59秒**，q015f-ownership-fixture-baseline.json，finally精确恢复源。
- HTTP动态返回沿明确Callable输入契约隔离为object，在使用status/payload前统一验证。为避免重复API现有同语义验证，把原_response_fields/_response_pair原样移到http_response.py；输入序列接受范围、int含bool的既有判定、dict原身份和错误字符串保持一致，API保留原私有别名。新增生产源加入Q015F，完整范围996文件/305批。API因真实共享边界改动再次待重验，不因可选优化重开；当前集中执行必要API/HTTP/服务验证。


Q015F JSON与类型边界独立验收（主批仍进行中）：

- 最终直接HTTP认证/JSON/短body/owner/plumbing **0，39 passed/15 subtests，17.39秒**；新增六错误根输入全部实际400，空body和{}仍task required，未知POST仍404，不触碰bootstrap/control既有拒绝策略。四个owner测试原始生产fixture基线4通过，未按无约束object占位改变正确生产契约。
- 明确server类 `WebAssetTests / LocalProviderApiTests / ResearchGraphApiTests / ResearchServerHelperTests` → **0，31 passed，17.38秒**；API完整输入脚本/服务生命周期/发布/ChangesGetTests/RestoreLeaseTests → **0，45 passed，3.93秒**。SSE五个明确node+execution output/transcript cache两文件 → **0，19 passed，4.13秒**，q015f-shared-support-validation.json；全部无skip，非全量收集。Q015C共享helper影响已验证，恢复已完成。
- server原194条Any诊断 → 首轮连新测试45条 → 最终0；server/API/shared response源码和JSON/owner两完整测试共5文件加严Any/Pyrefly/ty/Ruff全部0。生产mypy **0，750文件**、Pyrefly **0，35 suppressed/168 warnings**、ty/precision0。scoped Pyrefly15 warnings如实保留，不声明新增或既有归属未经比较。逐命令/退出码/稳定hash q015f-json-validation-final.json。
- 共享测试债务重新实际核验：`python -m mypy tests/test_server.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated --cache-dir .e2e-artifacts/code-quality-20261122/mypy-any` → **1，1673诊断**。主要为既有无注解provider/mock/agent脚本、dict泛型和动态server导出；本批未改该6727行脚本，没有以选中node通过宣称它全文件治理完成，也不把当前1673套写成旧1709。该生产支持债务仍属Q008D/Q015F，后续须分生产职责迁移，无新增ignore/skip或强行处理无关业务测试。
- 当前JSON/类型变更可独立验收先提交；socket构造泄漏原复现保留，下一步立即按独立commit修复，不将主批或server文件标记全部问题已完。新http_response源已独立验证。没有已知本轮引入的测试/导入/构建失败；新增构造回归尚未加入本次提交，原构造缺口是已记录后续待修bug。

提交前人工检查补齐event dict的Mapping[object,object]显式边界，避免工具条件推断掩盖内部Any，同时保留非字符串额外键的旧event判定语义；移除本次全局Actor类型导入使其重复的局部导入，整理本次新增imports。受影响SSE五node+服务生命周期 **0，21 passed，4.05秒**；五完整文件严格Any/三类型/Ruff及750文件生产三类型/precision再次全0，35 suppressed/168 warnings不变，q015f-json-final-polish.json。JSON/路由函数没有后续行为修改，39/31/45/19结果按其实际影响保留，不虚构重跑。


Q015F JSON/type/shared response独立成果已提交：`5bc0a43c`（fix(http): validate object request envelopes and typed route boundaries）。

Q015F 构造资源修复与批次移交：

- 独立构造文件在5bc0a43c当前源先实际执行 **1，1 failed，1.66秒**，关键断言仍是校验失败后bound listener未关闭，q015f-construction-before.json。不是导入、参数或环境失败。
- 保留原绑定/验证/操作员认证初始化顺序，在父构造成功之后包住验证及认证初始化；任何BaseException明确server_close后重抛原错误。没有依赖GC、吞异常、隐式重试或改变正常HTTP启动。资源所有权仅覆盖已经成功绑定的父对象，父构造自己的异常清理由标准库继续负责。
- 最终 `python -m pytest -q tests/test_server_construction.py tests/test_service_lifecycle.py tests/test_control_runtime_ownership.py --tb=short` → **0，21 passed，3.11秒**，无skip。相同失败输入现在fileno=-1；真实正常初始化/认证/激活/关闭、初始化前拒绝release以及drain期间保留所有权边界同时通过。两完整文件加严Any/Pyrefly/ty/Ruff全0；生产750文件mypy/Pyrefly/ty/precision再次全0，35 suppressed/168 warnings不变，q015f-construction-final.json，hash稳定。
- Q015F两个生产源和本轮修改的直接测试已完成本身的类型/行为验收；**整个Q015F标记部分完成，拆出共享test_server.py的1673诊断债务**，不把6727行跨provider/review/kernel/research脚本的欠账隐藏在“生产零Any”后面。此脚本本批未修改；保留已验证的独立生产成果，按生产职责分配支持脚本后续治理，不无限停留在server同一实现。
- 当前 **24 / 305批完整完成，49 / 996生产文件属于完整完成批次**；Q015F两个源加其他部分批次两个源均独立验证，累计53个当前已验证生产文件，统计明确区分。原共享测试3个lazy import失败、Q018清理警告、Q078库存等仍保持原处置，未全量pytest。下一主批Q016先依实际职责进一步拆分注册表/投影、tracking、CLI、desktop、headless，优先基础注册表，未缩减总范围。总goal active。


Q015F 构造修复已提交：`7d1b731f`（fix(http): close the listener when service initialization fails）。

### Q016 注册表与运行状态投影（进行中）

- 用户更新总目标文件为 attachments/123ef67f-6428-4a26-83c6-fcfa31cc10c8/goal-objective.md，已实际完整读取；新目标保留全仓分批治理、证据、验证、commit、禁止worktree等原约束，并声明长期维护者可决定设计。继续同一active总goal和队列，不创建新goal或丢失原范围。
- 原Q016八文件实际职责混合，按已读接口拆为Q016注册表/审批/状态投影三文件、Q016B change_tracking、Q016C CLI、Q016D desktop/startup、Q016E headless。八文件逐项唯一归属不变，完整范围996源/309批；优先基础注册表，不同时深入改CLI/desktop/headless。源三文件和两原测试已全读；task_lifecycle_projection.py为投影接口深读，全文件额外Any核验0，归Q027不冒充整个Q027已治理。
- 基线 `python -m pytest -q tests/test_approval_registry.py tests/test_run_registry.py --tb=short` → **0，18 passed，1.55秒**；四源（含task_lifecycle_projection）+两测试强Any **223条/4文件**，另外两文件本来0，q016-baseline.json。
- **确定性bug：教学取消记录复制使等待方误认为Resume。** sibling_probe.handle_control_teach把pending注册后等待其中Event，再读取同一原pending.cancelled；ApprovalRegistry.add_teach却复制字典，cancel_teach只更新副本后唤醒共享Event。旧值False导致实际调用finish_click_capture而非取消捕获并抛ControlTeachCancelled。该预期来自真实调用方等待/取消控制流和Stop不应完成已取消捕获的不变量，不仅来自新断言；非竞态推测。
- 注册表回归先运行 **1，1 failed/9 passed，1.22秒**，原pending.cancelled实际False，q016-teach-before.json。真实AppContext+真实handle_control_teach连续路径、仅点击捕获副作用使用有类型函数替身，原HEAD approval源 **1，1 failed，1.24秒**，实际走finish而触发cancelled teaching attempted to finalize断言，q016-teach-continuation-before-final.json；第一次错误import造成collection error保留为q016-teach-continuation-before.json，不计有效复现，修正为真实providers.controls后才得到行为失败。全部finally精确恢复源字节，没有worktree。
- add_teach保留等待方record身份，shell登记仍复制并钉generation，两种职责不混合；公开pop/snapshot/cancel返回仍复制。中间仅共享record时，真实取消路径已正确抛取消，却剩pending记录/卡片，实际回归 **1，1 failed，1.23秒**，q016-teach-partial-fix.json。因此取消在registry锁内先移除全部本次待办，再将原record标cancelled并唤醒；不让已取消捕获继续作为可恢复UI操作。最终同输入断言取消异常、实际cancel_click_capture、未finish、无pending记录和无pending UI卡片全部通过。
- 教学Event用仅暴露set的运行时Protocol收窄；元数据仍容纳真实request/token对象，只有消费的signal/UI dict边界收窄，不把所有metadata谎报JSON。resume和cancel的signal操作归唯一_wake，None/正常set行为一致，未知不具备set的对象明确报类型错误；不改有效resume语义。两源原重复的signal消费只在输入/副作用一致处共享，未把shell记录复制和teach共享强行抽象。
- RunRegistry单独类型边界：ProviderFailure.to_dict旧Any返回先作object，按真实ProviderFailure标量字段+sanitize_failure_facts单层标量map验证，保留事实False/整数和空facts省略，未引入JSON深度/大小预算或重写diagnostic生产者。完整registry测试参数/容器/回调强类型，原所有断言保留。新增provider failure正/空/None对照和完整typed旧用例，在HEAD原registry实际 **0，11 passed，1.14秒**，q016-registry-fixture-baseline.json，finally精确恢复。
- 主批另一个run_state_projection源本来无Any，无必要业务修改。现有cast未新增：ApprovalStatus字段为已检查TypedDict的str/int，TaskLifecyclePayload来自registry固定run/active/event字段与workflow状态，pending UI来自现有shell/teach构建的JSON事件；它们是已知内部投影的泛型信息恢复，不声称完成其外部builder全文件类型治理。已查看实际两个UI事件生产点；不用加新的无依据cast或额外预算来消除诊断。
- 三直接测试最终 **0，22 passed，1.99秒**，q016-teach-final-first.json。task lifecycle/forget-session+六明确server节点组 **0，15 passed，7.23秒**，q016-projection-callers.json；审批持久失败/关闭/重启+四明确server过期节点 **0，7 passed，9.43秒**，q016-validation-final.json。无skip，未运行全量test_server。运行真实恢复/停止边界，未声称执行浏览器UI或真实点击捕获。
- 完整强Any **223 → 11 → 6 → 0**；额外真实教学回归也0。生产首轮类型因本次ApprovalRegistry精确返回使coordinator两个旧cast冗余而失败（mypy2错，ty2条诊断，Pyrefly170warnings），保留q016-validation-final.json；已经在必需的coordinator边界只删两个冗余cast，整coordinator已读并加入全文件Any检查，不去扩展其余Q024。最终八完整文件严格Any/Pyrefly/ty/Ruff全0；750文件生产mypy/Pyrefly/ty/precision全0（35 suppressed/168warnings），q016-validation-complete.json，所有检查hash稳定。属于本轮引入并解决的检查失败，不归为历史问题。
- 主批三个源已读和验证，独立RunRegistry类型变更与教学取消修复分两commit。先对旧approval/coordinator+新registry的实际中间提交版本做独立限定测试/全生产类型门，保证第一个commit不依赖未提交教学修复；当前结果逐命令q016-registry-commit-validation.json。未确认reserve/replace冲突输入的对外策略，没有据偏好改变注册身份行为或删除仍公开的兼容入口。

RunRegistry实际中间提交版本验收已完成：旧approval/coordinator源码+本次registry和完整typed测试，**11 passed，1.13秒**；两完整路径严格Any/Pyrefly/ty/Ruff、750文件生产三类型/precision均0，35 suppressed/168 warnings。q016-registry-commit-validation.json确认finally精确恢复本轮approval/coordinator。先按四明确路径暂存registry、其完整测试和两记录，不夹带仍待独立提交的教学修复。主批仍进行中，完整完成统计不变24/309、49/996。


Q016 RunRegistry类型/诊断投影已提交：`2a027a51`（refactor(runs): validate provider failure projections and type registry tests）。

Q016主批完成：三生产文件完整检查与约定Any核验完成，教学取消修复将按独立变更提交；修改过的approval/coordinator及两完整审批测试已全文件零Any并完成必要验证。Q024只有因本次精确返回需要的coordinator单源完成，其余五文件仍未治理，明确标为部分完成。当前 **25 / 309批完整完成、52 / 996生产文件属于完整完成批次**；另5个部分批次文件独立验证，累计57个当前已验证生产文件。共有两次有效改前行为失败证据（注册表原flag、真实取消续跑原finish路径），中间残留卡片失败也已定位并修复，不将collection error记作复现。下一唯一主批Q016B：change_tracking.py的task-owned tracker选择；CLI/desktop/headless消费者仍待基础生命周期/context先行，不回到已验收server/registry追加可选优化。总goal仍active。


Q016 教学取消修复已提交：`1a6d017b`（fix(approvals): cancel the waiting teaching record and remove stale actions）。

### Q016B task-owned tracker 资源选择（完成）

- change_tracking.py全42行及test_task_change_ownership.py完整脚本已读，路径归一化、显式scope、无scope扫描真实durable admission、只选择实际manifest、OrderedDict复用/更新/淘汰/禁用旧writer等路径已检查。没有确认新的业务bug或可删公开入口；不为造commit改limit为正的现有MAX_CHANGE_TRACKERS规则，也不引入新的latest-task指针。
- 原相关行为基线 `python -m pytest -q tests/test_task_change_ownership.py tests/test_boundary_hardening_regression.py::SnapshotRequireBaselineTests tests/test_recovery_ownership_regression.py::ChangesGetTests tests/test_recovery_ownership_regression.py::RestoreLeaseTests --tb=short` → **0，14 passed，2.27秒**，q016b-behavior-baseline.json。源码完整强Any16条；源码收窄后原测试单独76条 → 9条 → 0，不将两次不同范围诊断混写为同一次总数。
- state明确唯一真实AppContext调用方，TYPE_CHECKING避免运行时循环；checkpoint.project来自JSON，先保留原falsy跳过，再验证str再Path；无效非空项目仍TypeError，scope已有ValueError不变。缓存key、扫描顺序、文件判断、淘汰动作没有业务修改。整个测试的Path/bool参数、文件行列表/字符串diff明确校验；legacy task_change_tracker返回先作为object，实际isinstance ChangeTracker后使用，没有cast/ignore或筛掉坏行。
- 补充有实际资源意义的LRU/current scope和evicted writer.disable_persistence边界、无缓存ephemeral、恢复跳过无manifest新scope而选有旧baseline。新typed十用例在HEAD原change_tracking.py实际 **0，10 passed，1.65秒**；额外非法project与scope四输入在同原源码 **0，4 passed，1.34秒**，q016b-fixture-baseline.json/q016b-invalid-fixture-baseline.json均finally精确恢复当前字节。没有把这些基线通过包装成bug修改前失败。
- 最终同初始相关组 **0，20 passed，2.37秒**，无skip/cleanup警告；两完整文件加严Any/Pyrefly/ty/Ruff全部0，750文件生产三类型/precision全部0（35 suppressed/168 warnings），逐命令/退出码/稳定hash q016b-validation-final.json。未深入的其余共享回归脚本只执行明确类，不报整文件Any完成；operations/task_changes.py旧Any归原owning批次，本批仅隔离返回边界。
- diff已审查，当前 **26 / 309批完整完成、53 / 996生产文件属于完整完成批次**；另5个部分批次文件独立验证，累计58个当前已验证生产文件。下一步先按实际入口细分Q017并治理durable/lifecycle/workspace lease基础资源，再推进context与CLI/desktop/headless消费者；原Q017 lazy import确定性待办和Q018/Q078等限制继续保留，总goal active。


Q016B 已提交：`8c66a370`（refactor(tracking): type task scope selection and recovery ownership tests）。

### Q017B durable/app/project 资源生命周期（完成）

- Q017按已读实际职责拆为Q017B三个基础资源owner、Q017 consensus/sibling两provider消费者、Q017C workflow HTTP单源；六文件不增不减，已登记Q017 lazy import失败仍保留在原provider批。完整范围996源/311批；先基础资源，再context和启动消费者，未缩减范围。
- durable_services.py/lifecycle.py/workspace_leases.py以及两直接测试整文件已读；基线 **0，6 passed，1.59秒**，三个源本来强Any0，旧services测试3诊断含DurableStore.path接口不成立、SQLite fetchone动态值；另有原arg-type ignore依赖假Session/Scheduler。q017b-baseline.json。改为真实pair并追踪精确close回调，保留原scheduler→session顺序断言，去掉原ignore；SQLite store用真实isinstance收窄，SQL行先入object后断言不传播Any。
- **确定性bug：scheduler启动失败没有关闭已经打开的durable owner。** open已经获取OS数据库lease、打开session并构造owner，再start dispatcher；原start失败直接抛出，没有走有序close。资源构造失败应清理所持资源，且持久事实不可删除，来自本模块显式生命周期owner契约及资源所有权不变量。
- 输入为合法数据库中一条待恢复admission，实际scheduler启动路径仅对真实调度Thread.start注入RuntimeError。错误被pytest按预期捕获后，关键断言session_closed实际False、lease仍持有，**1，1 failed/6 passed，1.74秒**，q017b-startup-before-final.json。原首次空库没有pending工作，不会创建线程，DID NOT RAISE的结果保留q017b-startup-before.json，不计有效复现；补真实待恢复任务后才进入目标路径。不是把线程环境错误本身当bug，失败点是错误后的资源未清理断言。
- 在scheduler.start失败时调用已有services.close，保持scheduler先停、session后关、lease最后释放及原异常重抛。没有删除/重建数据库、恢复默认配置、吞异常或放开第二owner。测试实际验证捕获的失败owner关闭标志/lease清空、再次获取OS lease、重开SQLite及原work任务仍可读；正常open、schema不兼容保留sentinel、第二owner拒绝和关闭重试对照保留。
- 工作区lease源不需要生产改动，新增真实OS锁两例当前源基线 **0，2 passed，1.22秒**：路径别名排他/重复release/跨coordinator重用，以及close release首次故障仍保留owner供重试。durable close首次False同样保持数据库lease，成功后才释放，防止用启动修复破坏已有有序清理。lifecycle源按现有stop→conversation→scheduler→worker/shell→controls/stores顺序和ResourceSlot失败保留检查，不按猜测改并发close或签名兼容策略。
- 最终 `python -m pytest -q tests/durable/test_services_lifecycle.py tests/test_app_lifecycle_coordinator.py tests/test_workspace_lease_coordinator.py --tb=short` → **0，10 passed，2.16秒**，无skip；service_lifecycle/runtime_ownership+两个审批持久边界+RestoreLeaseTests → **0，23 passed，5.89秒**，q017b-validation-final.json。仅声明这些执行范围，没有跑整仓或整test_server。
- 三源+三完整测试加严Any/Pyrefly/ty/Ruff最终全部0，q017b-validation-complete.json。中间Thread类对象暴露的stdlib Any两处已用有类型回调和精确属性路径避免；mypy对调用close后可变字段仍保留先前收窄造成1条unreachable，改用独立断言函数观察真实lease，断言不放宽。原3诊断、过程中2诊断/1诊断与最终0区分，失败日志保留。750文件生产mypy/Pyrefly/ty/precision全0（35 suppressed/168 warnings）；最后只改测试断言函数，三个生产源hash与全生产验证时一致，不机械重跑已通过生产门。
- diff/最终hash已核对。当前 **27 / 311批完整完成，56 / 996生产文件属于完整完成批次**；另5个部分批次文件独立验证，累计61个当前已验证生产文件。下一主批Q018 context.py，优先核对原idle attachment退出锁清理警告；Q017 provider lazy import、Q015F共享测试1673诊断、Q078库存等仍未完，总goal active。


Q017B 已提交：`e3fe9bc4`（fix(durable): close service ownership after scheduler startup fails）。

### Q018 Context 资源装配（进行中）

- context.py当前842行已完整检查（补读终端截断片段），完整强Any基线 **0**，q018-any-baseline.json。不能套用旧context动态字段债务数；前序注册表/服务类型传播和外部提交已经改变实际基线。
- 发现外部branding提交303e7724，位于本轮8c66a370与e3fe9bc4之间；仅生成web/runtime descriptor、图标及设计PNG，无自有生产源变更。design-proposal/codey-logo-icon-green.png和codey-logo-lightning-tail.png为他人未跟踪工作，保留不暂存。生成runtime清单已包含之前缺失browser_result_evidence.py及新增http_response.py；已实际比较当前tracked Python路径集合，不再将旧876/877陈述为当前阻碍。Q078仍未治理，也未运行复制/激活runtime测试。
- **独立确定性发布竞态：** knowledge_store在锁外构造，close的knowledge_for_close或公开setter(None)会关掉_enabled；返回第二次加锁时却只检查winnerNone，仍发布fresh，导致已关闭/禁用的context重新拥有资源。生命周期禁止清理后再接收资源，与原关闭回调关闭_enabled的明确意图构成独立不变量。
- 控制真实KnowledgeStore构造在发布前阻塞（临时state home代替默认home，不触及用户库），实际close/disable后放行；原源 **1，2 failed/2 passed，1.43秒**，两失败均是obsolete construction resurrected a disabled resource；正常缓存和显式replacement胜出对照通过，q018-knowledge-before.json。加第二锁内_enabled复核，沿原loser close路径丢弃fresh，原四输入 **0，4 passed，1.30秒**，context和完整回归脚本严格Any/Pyrefly/ty/Ruff全0，q018-knowledge-first-validation.json。新知识库资源race不混同旧附件退出警告。
- 原附件节点当前实际 **0，1 passed，1.56秒**，但进程退出仍WinError32锁清理warning，q018-attachment-baseline.json；最初父日志打印因GBK编码失败而wrapper退出1，保存的pytest真实退出码0，已核对，之后统一UTF8父/子进程日志，不将打印错误误报为pytest失败。
- 只读状态加受控诊断（ignored q018_fixture_probe.py，原业务断言后）：active run=queued/worker_ownedTrue、input-files=submitted，close=False且scheduler/session未关、database lease持有。测试把submit_browser_task替身固定True却没有真实worker归还reservation，ConversationRuntime.close按其有效ownership契约拒绝关闭。诊断仅归还这个假worker的volatile run，立刻close=True、lease不再持有，且警告消失，q018-attachment-owner-state.json/q018-attachment-owner-probe.json。这是测试stub未履行入场接受后的清理责任，不是生产lease竞态；不改生产清理门禁迁就假worker。
- 保留同一dispatch/metadata/submitted断言，在该test的finally归还stub reservation并关闭输入队列，不伪造task_done或执行结果，不改durable事实。目标节点实际 **0，1 passed，1.58秒**，无原退出warning，q018-attachment-cleanup-after.json。完整test_input_attachments.py以及conftest已为根因读过，完整强Any当前分别167/66条，q018-support-any-baseline.json；尚未完成不得标记这些脚本/Q018全部治理完成。conversation_runtime.py全54行明确close所有权契约已读，旧Any仍归Q019，未将此次定位等同其类型治理。
- 附件整文件类型治理已完成：原167条诊断经明确fixture/回调、真实Session及带spec且isinstance验证的mock、动态记录/序列/文本边界收窄降至0；原业务断言保留，完整知识库与附件测试 **0，23 passed，3.19秒**，q018-input-typed-validation.json。
- conftest改前行为基线：`python -m pytest -q tests/test_context_resource_publication.py tests/test_input_attachments.py tests/test_service_lifecycle.py tests/test_app_lifecycle_coordinator.py tests/test_research_submission_handoff_production.py --tb=short` → **0，58 passed，368.75秒**，无skip，q018-conftest-baseline.json。17个真实浏览器参数用例耗时较长但已正常汇总退出，不将等待过程说成失败。
- 隔离支持基线：`python -m pytest -q tests/test_pytest_cleanup_guard.py tests/test_pytest_home_isolation.py tests/test_pytest_home_isolation_preserves_playwright_cache.py tests/test_pytest_home_isolation_preserves_bun_runtime.py tests/test_pytest_ramdisk.py --tb=short` 首次 **1，8 failed/31 passed/8 warnings**，根因为runner只强制子输出UTF8、嵌套subprocess仍按Windows GBK解码而返回None；未修改测试或生产。父子统一`PYTHONUTF8=1 PYTHONIOENCODING=utf-8`后，同一未修改源码 **0，39 passed，34.30秒**；原失败与环境对照分别保留q018-isolation-before.json/q018-isolation-before-utf8.json。后续本批验证明确使用这两项环境设置，不声称未配置的Windows编码场景已修复。
- conftest追踪列表实际仅接收成功构造的AppContext，改为具体类型；init/close包装器保留真实签名，类型专用导入放TYPE_CHECKING，运行时AppContext导入仍在隔离home建立之后。合并两个语义相同的四次close重试循环，保留次数、异常停止、根目录归属与重入guard；原constructor变量仅作安装标志，全仓引用核对后改bool，原callable仍在闭包。进程级补丁用已有pytest.MonkeyPatch保持寿命，不新增ignore。三个fixture、DNS/metadata/availability回调和pytest hooks补全类型，完整强Any **66→0**。
- 四个完整文件context/resource_publication/input_attachments/conftest的强Any/Pyrefly/ty/Ruff最终均exit0（Pyrefly另有1 warning未显示）；首次ty对旧Windows模块函数直接赋值报1诊断，同样改用MonkeyPatch后通过，未关闭检查。修改后直接生命周期与附件四文件 **0，41 passed，4.73秒**；同五个隔离支持文件 **0，39 passed，35.11秒**，q018-conftest-typed-validation.json。Context额外调用方`tests/durable/test_app_context_store.py tests/test_control_runtime_ownership.py`及coldstart两个knowledge节点 **0，7 passed，2.24秒**。浏览器调用方复核及最终生产门仍在执行，尚未提交/标记本批完整完成。

- 最终补验：`python -m pytest -q tests/durable/test_app_context_store.py tests/test_control_runtime_ownership.py tests/test_coldstart_review_batch.py::ColdstartReviewBatchTests::test_knowledge_read_notes_batches_single_roundtrip tests/test_coldstart_review_batch.py::ColdstartReviewBatchTests::test_knowledge_index_close_fails_cleanly_on_reuse --tb=short` → **0，7 passed，2.24秒**；`python -m pytest -q tests/test_research_submission_handoff_production.py --tb=short` → **0，17 passed，366.44秒**。与前两批41/39项合计本批修改后四批104项通过、0失败/0跳过；未执行其他仓库测试，不把基线重复执行另计唯一覆盖。q018-validation-final.json。
- 最终生产门串行：`python -m mypy codey --python-version 3.11` → **0，750文件**；`pyrefly check codey --python-version 3.11` → **0 errors，35 suppressed/168 warnings未显示**；`ty check codey --python-version 3.11` → **0**；`python -m tools.kernel_type_precision_gate` → **0**。四个变更源/测试hash全过程一致，diff --check通过。实际运行环境仍为Windows/Python3.12，目标类型版本3.11，未声称其他平台实测。
- 当前验收完成，按生产发布竞态与测试fixture职责拆分两次本地提交；第二提交落地后才将Q018标完整完成。Q019 conversation_runtime明确剩余Any位置为__init__:15的emit/durable、admissions:21、prepare:23的ctx/payload/return，保留未修改且不计治理完成。

Q018 生产发布竞态已提交：`9e984dd4`（fix(context): prevent lazy knowledge publication after disable）。测试资源清理和共享fixture整文件类型治理作为独立职责提交，不夹带两张外部PNG。

Q018验收完成：当前 **28 / 311批完整完成，57 / 996生产文件属于完整完成批次**；另5个部分批次文件独立验证，累计62个当前已验证生产文件。context完整强Any0、附件167→0、conftest66→0、新回归0；合并两处同语义close重试，未删除无法证明无用的生产API。下一主批Q017 provider控制流，先复核已有lazy import三失败及其独立导入契约。总goal仍active；Q015F大测试类型债务、Q019等其余范围未完成。

Q018支持提交：`20ab7a7a`（refactor(tests): type lifecycle fixtures and release fake worker ownership）。两提交后工作区仅剩他人两个未跟踪PNG。

### Q017 Consensus / sibling provider 控制流（进行中）

- 两个生产源和test_server_lazy_state.py已完整读。consensus_service模块明确承诺import-light，不得在import时加载浏览器栈；旧实际导入链记录与当前顶层project_audit_advisor导入吻合。先实际复跑当前源码行为失败和完整Any基线，再决定最小延迟导入及类型边界修复。

- 当前源码lazy复现 **1，3 failed/8 passed/4 subtests passed，11.33秒**，三处均实际加载56个浏览器/Playwright模块；非导入异常。两源和完整lazy回归强Any共71条，q017-baseline.json。共识/审计连接、模型占用及共享调用方行为基线 **0，39 passed，12.42秒**，q017-consensus-behavior-before.json。
- 基于真实独立职责将原两个源拆为Q017（consensus_service）与Q017D（sibling_probe）；库存996源不增不减、每源唯一归属，总批次312，不减少总范围。sibling_probe全文件已读但Any尚未治理，request.page、pending metadata及controls.resolve_captured_control动态返回/Locator handler契约仍待所属恢复批；不为本批导入修复顺带重写controls1500行或冒称该源已完成。
- 顶层project_audit_advisor导入移至真正执行project audit的函数内；公开run_project_audit签名/调用路径保留，不更改浏览器底层加载时机或删测试。consensus入口补ConsensusSender/ChatProvider/TaskPolicy，session/page仅作不透明对象转交；旧model snapshot在边界验证字符串键/启用标志/所选模型字符串后继续原capture+ExitStack路径。完整lazy测试仅给空期望列表显式str元素类型，原零浏览器模块断言不变。实际修改后验证进行中。

- 首次候选复验lazy文件 **1，2 failed/8 passed/5 subtests passed，10.26秒**：consensus输入已通过，但server/task_submit仍加载浏览器。新的真实audit-hook栈指向task_submit:27→project_adapter→task_loop→kernel→browser_binding，与已移除的consensus→audit路径不同，q017-task-submit-import-chain.json；原基线相同两个输入已失败，不归为新回归，也不宣称lazy全绿。复用原_heavy_after_import函数、原consensus输入及原[]关键断言独立复核 **0**，q017-consensus-import-after.json。
- 整个consensus源强Any **21→0**，lazy完整脚本 **4→0**，Pyrefly/ty/Ruff定向0。生产ty首次暴露ConsensusTask原object输入与真实Sender要求不一致；已确认两个生产调用方均交入ChatProvider，只将共享callback契约收窄至既有ConsensusSender。task_run.py完整785行已读，强Any基线31，未借机修改其他生命周期逻辑；其反射/可选callback与OperationOutcome类表达式诊断保留Q099部分完成，禁止称该共享文件已全面治理。
- `python -m pytest -q tests/test_consensus.py tests/test_server.py::ConsensusConnectionTests tests/test_model_preferences.py::test_consensus_uses_selected_api_model_and_holds_it_during_advice tests/test_model_preferences.py::test_failed_advisor_connection_releases_its_model_scope tests/test_shared_task_service_consumers.py tests/test_project_audit_advisor_kernel.py tests/test_consensus_audit_split.py --tb=short` → **0，52 passed，15.35秒**。随后精确契约对齐后的`python -m pytest -q tests/test_task_setup_cleanup.py tests/test_task_runtime_coordinator.py tests/test_shared_task_service_consumers.py tests/test_model_preferences.py::test_consensus_discovers_enabled_zen_and_cline_after_api_probes --tb=short` → **0，10 passed，3.34秒**；其中共享consumer1项重复，不把52+10当62个唯一用例。q017-consensus-final-validation.json/q017-consensus-complete-validation.json。
- 最终生产mypy750/Pyrefly/ty/precision全部exit0（35 suppressed/168 warnings仍在），必要protocol调整后的结果在q017-consensus-contract-alignment.json；定向强Any2文件0、Ruff3文件0。首次ty失败与修复后结果明确保留；源hash稳定。此次没有删除公开API或合并非重复业务逻辑。Q017源范围完成，Q017D恢复/教学仍未治理；当前 **29 / 312批完整完成，58 / 996生产文件属于完整完成批次**，另5部分源独立验证，累计63当前已验证生产文件。下一主批从Q027拆出task_submit入口处理剩余cold-start失败。

Q017 已提交：`a8c6dee6`（fix(consensus): defer audit kernel imports and type advisor scope）。

### Q027A Task submit admission / lazy runner（进行中）

- 从Q027拆出598行task_submit.py单独批次，剩余七源保留Q027；996源唯一归属不变，总批次313。已全读task_submit（终端截断尾部另行补读）和test_task_submit.py；TaskSubmissionState与project_adapter.run签名按必要调用契约定位，没有据此宣称这些被依赖文件已完成治理。
- 确定性bug依据task_submit模块明确冷启动契约：agent runner应实际run_task时才加载；顶层project_adapter导入实际走kernel/browser栈。有效修改前结果是Q017候选上的 **2 failed/8 passed/5 subtests passed**，该源当时未改；实际audit栈为独立证据。移除eager alias，保留同名typed agent_run(request)委托真实run，调用方注入seam保持。相同完整lazy文件修改后 **0，9 passed/6 subtests passed，7.41秒**，q027a-lazy-after.json。至此先前3个lazy失败两条根因均已实际修复，不再说整份lazy回归未绿。
- 行为改前`python -m pytest -q tests/test_task_submit.py tests/test_task_submit_durable.py --tb=short` → **0，28 passed，3.37秒**。同两文件强Any基线源9/测试76共85诊断，q027a-baseline.json。源使用具体adapter签名与对象键验证收敛review callback、provider failure callback和三处model selection序列化；worker参数tuple在局部声明元素类型，保持原输入归一化。未改入场、claim、retry、Stop与原初始化异常处理逻辑。
- 测试原稀疏_FakeState不满足真实TaskSubmissionState；改为真实AppContext/运行注册表/durable/scheduler，仍仅覆盖reserve开关及release/approval调用记录；真实context.close前归还仅由本文件接受stub创建的worker reservation，不伪造durable完成。queue替身用ParamSpec保留真实作业/参数类型与延迟执行闭包，保留调用次数、作业身份、错误传播、入场保留和accessor仅调用一次的断言；post-entry用真实deps/state与有类型entry/kick记录，不再动态Mock字段。原method-assign ignore已移除。
- 首次fixture改写自引入1个stub签名漏intent的位置参数错误（27通过/1失败），13条剩余容器诊断及keyword名称override不匹配；按实际submit接口修正，无改业务预期。第二轮28通过、mypy0，但ty不接受固定长度tuple[str]构造任意长度元组；改typed局部tuple，contextmanager改明确Generator后两完整文件强Any/Pyrefly/ty/Ruff最终exit0（Pyrefly2 warnings未显示），q027a-types-final.json。各中间结果保留q027a-first-typed-validation.json/q027a-typed-validation.json，不记为最终通过。

- 最终`python -m pytest -q tests/test_server_lazy_state.py tests/test_task_submit.py tests/test_task_submit_durable.py --tb=short` → **0，37 passed/6 subtests passed，11.14秒**；`python -m pytest -q tests/test_input_attachments.py tests/test_shared_task_service_consumers.py tests/test_headless_real_kernel_lifecycle.py tests/test_server.py::SessionThreadingTests::test_run_task_keeps_selected_provider_through_agent_completion tests/test_server.py::SessionThreadingTests::test_run_task_emits_provider_failure_diagnostic_on_error tests/test_server.py::SessionThreadingTests::test_run_task_uses_second_model_for_approved_review tests/test_server.py::SessionThreadingTests::test_existing_project_uses_read_only_audit_reports_before_writer --tb=short` → **0，26 passed，12.55秒**。两批63项与6子测试均通过，0skip；q027a-validation-final.json。这里只执行test_server明确四节点，没有整文件或全仓pytest。
- 最终生产mypy750、Pyrefly、ty、precision全exit0，35suppressed/168warnings仍披露；强Any两文件0，source hash稳定。AST核对原11个测试方法全部保留；空预期、worker调用次数/身份、retry次数及run身份、原ImportError/ValueError、captured state/getter单次和post-entry kick关键断言都保留；stub签名自引入失败已修复，无skip/放宽预期。diff及暂存前核对完成。
- Q027A完成：**30 / 313批完整完成，59 / 996生产文件属于完整完成批次**；另5部分批次源独立验证，累计64个当前已验证生产文件。原3个lazy失败已由Q017/Q027A共同修复并重跑完整lazy回归；Q008D共享server类型、Q09931Any、Q017D46Any等继续待办。下一主批Q018B knowledge observation，离开已完成的启动导入文件，不继续可选优化；总goal仍active。

Q027A 已提交：`4038ca13`（fix(task-submit): defer agent loading and type admission boundaries）。源码工作区干净，仅保留他人两张未跟踪PNG。

### Q018B Knowledge observation 持久化投影（进行中）

- 下一主批为knowledge_observation.py与实际知识观察测试，先整文件审查及验证基线，不重新打开已验收启动导入代码。

- 全文件确认按committed done decision及原始terminal receipts投影，project事实要求可信final版本/proof与同版本成功check，browser引用固定source revision1并由知识库标记后续版本过时；原内容不直接进入观测事实，existing观测避免重复创建。当前未建立新确定性bug证据，不以偏好改变这些流程，也无可证明死代码/同语义重复业务实现可删除合并。
- 基线`python -m pytest -q tests/test_p1_p2_receipts.py --tb=short` → **0，18 passed，2.30秒**；完整强Any源10/测试1共11条，q018b-baseline.json。source排序lambda在tuple/构造上下文失去参数类型，改明确KnowledgeReference→(str,int)比较键，顺序规则不变；旧provenance在消费url/content_hash前入object并验证字符串键。测试既有字段改为明确JsonObject，调查fixture复用已有InvestigationRecord，所有业务断言保持。
- 中间诊断11→4→1→0；最初局部排序结果注解未消除lambda隐式Any，JsonObject调查变量也不满足已有TypedDict，均按真实类型契约修正，未增ignore/cast。Pyrefly额外发现原passage_refs空列表推断Any，也由完整InvestigationRecord解决；新import的Ruff排序错误已纠正。q018b-first-validation.json/q018b-types-final.json保留中间失败，最终以q018b-validation-final.json为准。
- 最终`python -m pytest -q tests/test_p1_p2_receipts.py --tb=short` → **0，18 passed，2.28秒**；`python -m pytest -q tests/test_knowledge_version_graph.py tests/test_long_term_knowledge.py tests/test_memory_maintenance_runtime.py --tb=short` → **0，13 passed，2.96秒**。31项无skip，覆盖原browser observation的作用域、去私有正文、source修订stale、图与重放/维护调用方；未声称遍历所有生产输入或全仓pytest。
- 两完整文件强Any/Pyrefly/ty/Ruff0；生产mypy750/Pyrefly/ty/precision全0（仍35suppressed/168warnings），hash稳定，diff --check通过。当前 **31 / 313批完整完成，60 / 996生产文件属于完整完成批次**；另5部分源独立验证，累计65当前已验证生产文件。下一主批Q019 conversation/input lifecycle，按实际所有权边界先核对队列再推进。
- 外部新增未跟踪docs/reports/codey-single-agent-implementation-review-20261123.zh-CN.md，保留不暂存、不修改；两张外部设计PNG同样保留。

Q018B 已提交：`ba3bff82`（refactor(knowledge): type observation metadata and receipt fixtures）。

### Q019 Conversation cache / snapshot / deletion（进行中）

- 根据实际接口将八源拆为Q019两基础owner、Q019B两配置/HTTP分支源、Q019C三输入队列/worker源、Q019D一Git workspace subprocess源。完整996源唯一归属保持，总批次316；原四源完整已读，queue/fork等仅文件头定位不算全读。Q019B Any尚未完成，Q019C保留已读conversation_runtime债务；不会创建/复制worktree来验证fork功能。
- 新候选：registry在解锁加载前检查/淘汰容量，返回发布时既不复核并发winner也不再次限流，可能使max_states被突破、过期快照覆盖另一并发调用已保存的新状态。依据bounded provider-window cache的明确职责及成功更新后不得被更早load倒退的不变量；先编写并运行事件控制的行为复现，尚未修改生产代码。

- 基础基线`python -m pytest -q tests/test_conversation_registry.py tests/durable/test_app_context_store.py --tb=short` → **0，8 passed，1.27秒**。两基础源强Any本来0；完整registry测试10条，control配置104/HTTP分支201条共305分别留Q019B，q019-baseline.json，不宣称这些边界已治理。
- **发布竞态已证实：** max_states=1，slow加载在解锁后被Event阻塞，fast先发布，slow返回后原cache实际2项；同session旧load先捕获old快照，另一调用保存committed后旧load返回，当前cache倒退old。两个新节点原源 **1，2 failed，1.29秒**，都是容量或数据关键断言，不是环境/导入失败，q019-cache-race-before.json。发布锁内复核胜出缓存并保持LRU，只有真正发布新项时才淘汰；原7项及两个回归 **0，9 passed，1.16秒**。forget generation、load/save锁外和旧context脱离回调的边界保留。
- 删除原无用evicted tuple拆包及私有淘汰方法无人消费的返回tuple；全仓源码/脚本/文档/配置搜索确认该私有方法无外部调用、动态入口或公开承诺，kernel_parity基线仅记录同一方法名调用符号，方法仍保留。修正文档为实际on_change同步持久化、淘汰仅解绑回调，不再声称淘汰后另存一次快照。未删除构造器兼容参数或公开存储方法。
- **在途旧回调已证实：** 真实AppContext/SQLite中，callback启动后在真正保存前阻塞，真实forget返回空failures且已删除，随后原callback实际重新创建chat；同样会覆盖同ID新context的fresh快照或写回已淘汰context。四输入forget/recreate/evict/unchanged，协调器/绑定修改前 **1，3 failed/1 passed，1.51秒**，q019-snapshot-before.json；先前只读探针亦记录deleted=true→exists=true，q019-stale-save-probe.json。正确行为来自删除及脱离context不得再恢复旧持久事实的不变量，非仅来自测试。
- Registry增加只读当前snapshot身份检查，Coordinator在既有history read lifetime下校验再委托唯一save实现；forget原exclusive lifetime因此不会与检查/写入交叉。AppContext仅将内部callback绑定改到此路径，原显式save_snapshot接口保留；cache锁不覆盖SQLite IO，未吞异常。Q018因这一新有效复现触及绑定重开验证，整文件强Any仍0，资源/附件/存储/删除调用方复验后恢复完成。
- 为验证锁顺序完整读取HistoryLifetime，强Any基线0且未改源码；执行其三个现有跨进程/控制行/物理清理等待测试通过，单文件独立验收登记Q058部分完成，其另两源未治理。完整forget cleanup脚本12条Any中10条来自从未调用的私有_complete_pending；全仓无引用/导入，pytest/unittest不自动收集该名称，无fixture/plugin注册，测试不打包/无公开承诺，故删除该helper及唯一依赖import。其余容器期望及registry helper/callback/容器补具体类型，完整测试最终0，没有放宽原断言。
- 最终`python -m pytest -q tests/test_conversation_snapshot_lifetime.py tests/test_conversation_registry.py tests/test_forget_session_cleanup.py tests/durable/test_app_context_store.py --tb=short` → **0，16 passed，5.30秒**；`python -m pytest -q tests/test_context_resource_publication.py tests/test_input_attachments.py tests/test_history_file_lifetime.py tests/durable/test_conversation_delete_commit.py tests/test_server.py::RunSnapshotTests::test_state_owns_runtime_operation_store_and_forget_deletes_it tests/test_server.py::RunSnapshotTests::test_forgetting_session_clears_only_its_terminal_event tests/test_server.py::SessionThreadingTests::test_conversation_state_is_bounded --tb=short` → **0，40 passed，8.00秒**。两批56项无skip，未运行整test_server/全仓pytest/创建worktree，q019-validation-final.json。
- 七个完整源/测试（Context、registry、coordinator、history_lifetime及三个完整测试）强Any/Pyrefly/ty/Ruff全部0；生产mypy750/Pyrefly/ty/precision全0（35suppressed/168warnings仍在）；hash稳定、diff --check通过。已完成 **32 / 316批，62 / 996生产文件属于完整完成批次**；另6个部分批次源独立验证，累计68当前已验证生产文件。Q019B/C/D、Q09931Any、Q017D46Any等继续排队，后续进入Q019B配置/HTTP边界，禁止工作区复制。

Q019 已提交：`415f84d3`（fix(conversations): fence cache publication and stale snapshot writes）。当前工作区仅外部两个PNG与一份审查报告，保留未暂存。

### Q019B Canonical config / conversation HTTP branches（已完成，提交后暂停）

- 两生产源已在前批完整读，强Any基线control_conversations104/conversation_routes201，尚未治理。开始限定UI配置、会话树与本地分支路径的行为基线，先排除会真正创建worktree的验证路径。

- 用户要求“这一段做完先commit，然后停”；goal当前已paused。代码阶段Q019已由415f84d3完整提交并验收，当前只保存暂停交接记录，不继续Q019B修改或启动新测试。
- Q019B仅完成边界调研：两生产源此前已全读且有104/201条旧Any基线；完整读了tests/test_conversation_tree_routes.py及其durable/control.py配置/收据依赖。新读脚本的fixture/响应容器Any与依赖的latest/receipt/write动态字段尚未完成治理，也未执行该批行为基线，不把阅读记成通过或完成。恢复后从此范围开始，避免真正创建worktree的测试路径。
- 暂停时累计仍为32/316完整批次、62/996完整批次生产文件；另6个部分批次生产文件独立验证，累计68。最后代码验证为Q019两批56 passed及七文件严格Any/三类型工具/Ruff和生产门通过；本次仅Markdown/JSON进度更新，未重复运行pytest或类型门，已校验JSON、316批/996源唯一归属及diff。
- 外部两张PNG与codey-single-agent-implementation-review-20261123.zh-CN.md保留未暂存。总目标未完成；等待用户明确恢复。

- 用户澄清仅授权“把Q019B做完，commit，然后停”，本批重新执行，完成后不进入其他批次；总goal保持paused，不冒称已恢复全仓连续执行。修改前明确15个UI/config/tree/只读capability用例实际 **0，15 passed，87.95秒**，q019b-baseline.json保存完整命令与退出码，未创建worktree。
- 两完整生产源改为AppContext/只读Mapping/具体响应字典；动态配置、UI记录、任务状态与历史输出先入object再验证字段/序列/被消费的字符串整数数值，不用无依据cast或ignore。UI列表复制后写回原state再保存，保留插入、排序、revision、偏好和额外字段语义；fork后台仍唯一拥有真实作业/文件系统操作。
- 将ControlService原两个同语义_fields/_rows校验实现移动到已有utils.object_fields，保留旧私有名import alias及原异常类型/消息，配置和分支路由以及测试复用唯一入口；不新增生产模块/依赖/配置。两个此前完成文件因实际共同实现改动重新验证，完整五文件强Any/Pyrefly/ty/Ruff全0（Pyrefly5warnings未显示），本地树直接5项通过，q019b-first-validation.json。第一轮源诊断305→4，剩余为legacy latest/status条件中Any传播，边界拆为明确object后全部0；目前未建立新确定性bug证据，未据猜测修改业务。

- 完整tree_routes脚本强Any **91→0**（两源104+201→0），合计396条原诊断消除；保留原5个用例及所有关键断言。新增5个无效分支请求（无效ID、空entry、bool/字符串revision、未知action）确认400且head不变；workspace创建/查询/重试/缺失或坏元数据通过有类型_ForkJobs替身验证，未调用真实Git worktree后端；完整脚本 **0，13 passed，3.21秒**，q019b-boundary-validation.json。五完整文件强Any/Pyrefly/ty/Ruff再次全部exit0，未引入cast/ignore/skip或降低断言。
- diff已按生产职责审查；共享校验两函数为原ControlService实现原样迁移，异常类型/消息、行顺序、遇坏行立即失败与结果字典身份保持，旧私有调用符号通过alias保留；新调用点均使用同一入口，不通过过滤坏行消除类型问题。正在执行完整external_control及生命周期投影调用方、fresh-process导入/历史分页与生产类型门，尚未记通过或提交。

- 最终调用方批次：`python -m pytest -q tests/test_external_control.py tests/test_task_lifecycle_projection.py tests/test_fork_capabilities.py::test_capability_route_reports_projectless_and_failed_checks_without_mutating_state --tb=short` → **0，39 passed，222.24秒**；导入/历史批次`python -m pytest -q tests/test_server_lazy_state.py tests/durable/test_history_snapshot_paging.py --tb=short` → **0，17 passed/6 subtests passed，7.87秒**，q019b-validation-final.json。完整external_control测试实际执行，包括控制身份、RPC/MCP本地协议等调用方；未将其原有动态类型宣布已治理。
- 首次生产门全部exit0，但Pyrefly出现169而非原168warnings；`--min-severity warn --output-format json`定位新一项为typed entry_id仍包str的冗余转换，q019b-pyrefly-warnings.json。移除该单一转换后，`python -m pytest -q tests/test_chat_fork_workspaces.py::test_message_fork_keeps_whole_turn_and_origin_without_running_it tests/test_chat_fork_workspaces.py::test_invalid_fork_mode_is_rejected_without_mutating_history tests/test_control_service_contract.py --tb=short` → **0，20 passed，3.86秒**。这三个本地节点只创建小型临时Git仓库，未调用worktree创建；其余实际worktree测试未执行。通过重建旧route哈希确认只改_turn_tip该转换，其他四文件hash相同，因此先前长测的无关路径结果仍有效，未机械重复39项。
- 最终四组互不重复nodeid范围 **13+39+17+20=89 passed，另6 subtests passed，0失败/0skip**。原5个tree测试方法与断言保留，新增8个参数用例，不把修改前基线与重复定向验证额外累计。五完整文件强Any/Pyrefly/ty/Ruff全exit0；生产mypy750/Pyrefly/ty/precision全exit0，Pyrefly恢复原35suppressed/168warnings，定向4项既有冗余query转换warning仍披露。q019b-validation-complete.json保存最终hash与命令；Windows/Python3.12实际运行、类型目标3.11，不声明其他平台实测。
- 最后20项断言通过且pytest exit0，进程退出清理出现一次RAM目录WinError5 warning，指向本地Git fixture的只读object文件；实际文件只读属性已核对，在工作区忽略证据目录中自建只读小文件，独立复现shutil.rmtree→PermissionError WinError5，q019b-git-cleanup-warning.json。自己的小探针已按验证后的绝对路径清理；保留原失败清理目录作为证据。根因属于未修改的共享测试清理对Windows只读Git文件的处理，不是本批生产行为/断言失败；记录到测试基础设施待办，本次不扩展修复，不把目录清理宣称成功。
- Q019B声明范围验收完成：类型与同语义校验职责收敛，没有猜测性bug修复、公开接口删除或真实worktree操作。当前 **33 / 316批完整完成，64 / 996生产文件属于完整完成批次**；另6个部分批次源独立验证，累计70个当前已验证生产文件。durable/control.py及UI存储生产者原Any保留在其所属未完成批次，当前消费者已验证未知数据边界，不借本批完成宣称生产者已治理。
- 用户要求只做完Q019B并commit后停。完成diff/暂存检查后创建本批本地提交，随后保持goal paused，不进入Q019C或其他模块。两张外部PNG及用户审查报告保留未暂存；全仓目标尚未完成。

Q019B 已提交：`bd2c1980`（refactor(conversations): type configuration and branch route boundaries）。

### Q019C Durable input admission / runtime lifecycle（进行中，2026-11-24恢复）

- 用户重新激活总goal，并更新目标为不重复检查此前已检查文件。沿现有未完成队列继续，不重开已验收区域；本批三个生产文件原未完成类型/行为治理。当前HEAD `86d727bc`，已有外部提交保留，仅用户审查报告未跟踪，不暂存；不创建或复制worktree。
- 本批入口为输入HTTP命令、AppContext运行事件准备和durable队列投影，依赖DurableSession任务/收据以及用户问题存储。三个生产文件已全读；直接路由、elapsed、authority及queued revision/steering测试用于行为基线，先执行再修改。DurableUserQuestionStore只作为必要动态边界依赖，原Any保留所属批次，未据阅读宣称完成。

- 原生产源基线：`python -m pytest -q tests/test_conversation_input_routes.py tests/test_conversation_elapsed_time.py tests/durable/test_input_queue_authority.py tests/durable/test_queued_input_revision.py tests/durable/test_steering_consumption.py --tb=short` → **1，1 failed/36 passed，8.14秒**。elapsed脚本全进程替换time.monotonic为两个ticks，真实scheduler并发读走时钟，导致StopIteration；不是生产elapsed计算错误。独立观测探针实际记录scheduler.py:472/_run与:122/_idle_tick穿插两个runtime调用（q019c-clock-probe.json，0/1pass）。只替换runtime模块time引用，保持真实scheduler时钟和原42_750ms断言；未修改生产源时完整elapsed文件 **0，2 passed，1.27秒**，q019c-clock-isolated-baseline.json。
- elapsed完整测试补参数/回调/容器注解，并用具体_Clock替身隔离两个时间值，不使用无类型SimpleNamespace或放宽断言。最终该文件 **0，2 passed，1.29秒**，强Any/Pyrefly/ty/Ruff均0；证据脚本也执行强Any检查。将该独立测试隔离修复先作为支持提交，Q019C生产治理尚待后续提交验收，暂不标完整完成。

Q019C 测试隔离支持已提交：`3e7d9c69`（test(conversations): isolate elapsed clock from scheduler deadlines）。

- **已确认输入边界bug：** HTTP JSON的`input_mode: []`或`{}`在原集合成员判断抛TypeError，未返回受控拒绝。两值都不属于有效steer/follow_up契约（docs/uiux_steering_approval_2026-10-23.md），无效枚举须走现有输入校验拒绝且不能创建持久任务；409来自该HTTP入口既有ValueError→409策略，非仅从测试推导。新回归`python -m pytest -q tests/test_conversation_input_routes.py::test_input_route_rejects_structured_delivery_modes --tb=short`修改目标判断前实际 **1，2 failed，1.35秒**，分别unhashable list/dict，q019c-mode-before.json；目标判断与HEAD原实现相同。先检查str再判断模式，原两输入随完整路由文件实际 **0，8 passed，5.83秒**，合法两模式、未知字符串及持久状态不变均覆盖，q019c-mode-after.json。未吞异常或放宽旧断言。
- 类型治理：三个完整生产源（runtime59、queue175、routes129）及五个完整直接测试（69/11/17/72/67）在强Any配置下总599条原诊断→0。采用具体AppContext/DurableSession/TaskCheckpoint、InputItem TypedDict、类型化回调/fixture和动态对象字段验证；序列化provider selection与question结果先作为object接入，再校验实际消费的字段。动态请求未消费字段保留object并在其负责边界校验，未将旧Any生产者宣称完成。保持请求指纹、queued修改/placement CAS、未知结果和资源所有权等既有语义；未增加cast/ignore/skip/配置豁免。
- 删除证据：bind_input_queue内两个私有闭包仅传给本批具体队列的bind，而bind原本无条件return None、不保存/调用回调；无反射、注册、动态导出或打包入口能访问这两个局部闭包，检查全仓源码/测试/脚本/文档配置引用后移除。保留公开bind_input_queue、queue.bind、dispatch_one及worker属性兼容面；公开API仅搜索不到引用不足以删除。调度继续唯一由DurableTaskScheduler拥有。抽出共同输入模型选择边界及三处相同draining读取，保持非API提供者忽略selection、显式captured selection优先和每个spawn fence处重新读取状态；不新建模块/依赖。
- 最终直接组：`python -m pytest -q tests/test_conversation_input_routes.py tests/test_conversation_elapsed_time.py tests/durable/test_input_queue_authority.py tests/durable/test_queued_input_revision.py tests/durable/test_steering_consumption.py --tb=short` → **0，39 passed，7.68秒**（q019c-direct-complete.json）。最后request_cancel局部变量收窄仅影响remove，整authority文件重跑 **0，3 passed，1.34秒**；elapsed具体时钟替身最后独立重跑 **0，2 passed，1.29秒**，不重复累计。
- 调用方组：`python -m pytest -q tests/test_conversation_questions.py tests/test_conversation_question_http.py tests/test_conversation_entry_auth.py tests/test_input_attachments.py tests/test_control_runtime_ownership.py tests/durable/test_no_legacy_runtime_fallback.py tests/durable/test_question_authority.py tests/durable/test_inbox_placement.py --tb=short` → **0，82 passed，80.37秒**，q019c-callers.json。最后变动限于移除已知str/int身份转换、模式类型检查及remove局部类型收窄；无关路径结果保持，受影响直接路由/取消范围已复跑。
- 调度与导入组：`python -m pytest -q tests/durable/test_scheduler_claim_release.py tests/durable/test_scheduler_local_reconcile.py tests/test_task_submit_durable.py tests/test_server_lazy_state.py --tb=short` → **0，35 passed/6 subtests passed，11.70秒**，q019c-scheduler-callers.json。
- 真实打包浏览器组：`python -m pytest -q tests/test_conversation_live_browser.py::test_real_steer_is_consumed_by_the_current_task_without_a_second_run tests/test_conversation_live_browser.py::test_real_stream_and_queued_input_keep_one_answer_and_survive_reload tests/test_conversation_live_browser.py::test_real_question_card_resumes_the_native_run_and_opens_read_details --tb=short` → **0，6 passed，46.07秒**，q019c-browser-callers.json，两协议真实HTTP/SSE/本地脚本provider/Chromium覆盖消费一次、排队续接与问题恢复，未调用真实外部模型、Git worktree或重构建发布资产。四组范围互不重复，累计 **162 passed，另6subtests passed，0失败/0skip**；诊断探针/基线/中间复跑不另计唯一覆盖，其余全仓测试未执行。
- 最终八文件强Any/Pyrefly/ty/Ruff均exit0，最后elapsed改动也单独重跑四工具。中间两个类型工具未能跨request.get保留字符串收窄，改为命名run_id变量后通过，未用断言伪造类型。Pyrefly曾由新具体类型暴露冗余str/int转换，已移除，定向0warnings。最终生产`python -m mypy codey --python-version 3.11` **0/756文件**、`pyrefly check codey --python-version 3.11` **0/35suppressed/165warnings**、`ty check codey --python-version 3.11` **0**，precision gate亦通过；Windows/Python3.12实际执行，类型目标3.11，其他平台未实测。
- 恢复期间外部提交新增6个生产源，仅读模块头/import作归属清单：browser_linked_record→Q084、model_handoff→Q101、project_delivery→Q090、project_instructions→Q088、python_edit_assistance→Q091、primary_sources→Q165，均仍未开始。生产总范围996→1002、批数仍316，路径检查无漏列Python或失踪源，未收窄范围制造完成率。
- Q019C声明范围验收完成，待创建主提交。当前 **34/316批完整完成，67/1002生产文件属于完整完成批次**；另6个部分批次源独立验证，累计73。其余依赖原Any和全仓范围未完成，用户报告未跟踪文件保留。下一批Q019D仅检查fork作业所有权及可模拟的进程边界，严格不复制或创建worktree；总goal保持active。

Q019C 主提交已完成：`a79cbd7d`（fix(inputs): reject structured modes and type durable queue boundaries）。

### Q019D Fork workspace jobs（进行中）

- 完整阅读单一生产源fork_workspaces.py及test_fork_capabilities.py，文档依据uiux_message_fork_2026-10-23.md；实际创建worktree的原测试不执行，只使用纯状态/模拟Git边界和只读Git诊断。完整两文件强Any基线118条诊断，尚未修改生产源。
- 待复现候选：start在锁外读取shutdown_requested，阻塞后AppContext可完成close并报告True，随后旧start仍可能发布worker。关闭成功后不得接纳新资源是生命周期不变量；使用事件控制真实AppContext关闭和真实start，只替换Git/注册副作用/_run为无操作探针，先实际运行失败再决定修复。

- **关闭发布竞态已确认：** `python -m pytest -q tests/test_fork_workspace_jobs.py::test_start_cannot_publish_a_worker_after_context_close --tb=short`（首轮运行该文件时仅此节点）→ **1，1 failed，1.31秒**，AppContext.close真实返回True后，旧start仍返回pending并启动已替身化的worker，没有应有的shutting-down拒绝，q019d-shutdown-before.json。预期来自现行start已拒绝shutdown及关闭成功不得再接纳后台资源的不变量，而不是测试偏好。锁内增加closing门禁，close同锁设置closing并获取固定worker快照，释放锁后沿原2秒总预算等待；防止发布交叉且不阻塞worker的最终持久化。原输入与关键断言修改后通过，另验证活跃worker时close=False、释放后True、迟到start拒绝和重复请求仅一worker。
- 行为基线：修改前完整capabilities **0，11 passed，1.80秒**；中断收据与模拟快照两个节点 **0，2 passed，1.25秒**。模拟Git验证独立临时index、快照命令、相对子项目、受管目标；目标越界和已有无关文件两边界均保持原文件不变。未实际创建或复制Git worktree，真实worktree恢复/checkout等原用例未运行，不声称该场景实测。
- 两原完整文件强Any配置诊断 **fork74/capability tests44，共118→0**；新增完整jobs测试及必要AppContext整文件均强Any0。具体AppContext和JSON字段运行时验证替代Any/cast，保留未知额外元数据、收据状态与Git命令顺序。只在AppContext补TYPE_CHECKING导入及惰性服务的None类型槽位两行，避免动态赋值/无依据断言；实际服务仍按实例绑定，独立两context测试验证不共享owner。原Q018仅因该实际类型所有权变动执行定向构造/关闭复验，没有重新扫描其余已完成文件。没有删除未证明无用的公开API或改动Git快照算法。
- 直接组`python -m pytest -q tests/test_fork_capabilities.py tests/test_fork_workspace_jobs.py --tb=short` → **0，18 passed，4.81秒**，q019d-direct-complete.json。既有capability测试保留断言，只补注解和响应字段校验；7个新参数用例涵盖关闭竞态/retention、状态恢复、模拟快照、实例所有权及目标保护。
- 调用方组`python -m pytest -q tests/test_conversation_tree_routes.py tests/test_conversation_input_routes.py tests/test_context_resource_publication.py tests/test_service_lifecycle.py tests/test_app_lifecycle_coordinator.py tests/test_control_runtime_ownership.py --tb=short` → **0，47 passed，10.99秒**，q019d-callers.json。与直接组互不重复，累计 **65 passed，0失败/0skip**；基线、复跑不另计唯一覆盖，不与Q019C重叠节点相加伪称全仓唯一用例总数。
- 四完整源/测试强Any/Pyrefly/ty/Ruff全exit0，ignored证据runner也强Any0；生产mypy **0/756源**、Pyrefly **0/35suppressed/165warnings**、ty **0**、precision **0**，q019d-production-*.json及q019d-precision.json。实际Windows/Python3.12、类型目标3.11，未验证其他平台。中途Ruff指出动态setattr与constructor语句预算，已通过明确惰性字段槽位解决，未加ignore或放宽阈值。
- capability基线及直接组退出时均有同类RAM清理WinError5，指向其小型Git空提交创建的只读object；所有断言及退出码0。该根因已由Q019B独立只读文件探针证实，当前文件只有类型/字段收窄改动，不新增此清理路径。保留为未修复的共享测试清理待办与原目录证据，不声称清理成功或扩展本批修复。
- Q019D声明范围验收完成，待创建本地提交。当前 **35/316批完整完成，68/1002生产文件属于完整完成批次**；另6个部分批次源独立验证，累计74。工作区用户审查报告保留未跟踪，不暂存。下一批Q020（history_routes.py/tool_history.py）沿未完成队列推进；总goal保持active，全仓尚未完成。

Q019D 已提交：`5fa495a9`（fix(forks): fence workspace job publication during shutdown）。

### Q020 History validation / tool presentation（进行中）

- 恢复读取最新goal和工作区，上一轮三个实际提交属于progress；HEAD仍5fa495a9，用户审查报告保留未跟踪。沿未开始队列进入两个生产源，不重新检查已完成生产区域。两个文件已完整读；接口为history run版本验证及display-only工具/推理记录，不把展示消息送入模型上下文。参照uiux_implementation_2026-10-20与uiux_tool_stream_2026-11-06的身份/顺序契约，必要依赖只看调用签名，不扩展重写。
- 修改前直接三文件history_routes/tool_display_history/reasoning_cold_history实际 **0，28 passed，2.35秒**；reasoning_display额外提供事件/原生回复生产者基线 **0，15 passed，1.47秒**。四个完整测试已阅读，强Any配置112条诊断；两个生产源1条（empty-chat去重dict.fromkeys默认值引入Any），tool_history本身0。尚未发现符合准入的新生产bug，不据猜测改变业务。
- 当前做类型边界收窄：有类型的占位值保留去重顺序语义；四测试保留原断言并验证动态行/JSON/可空事件，将MagicMock响应改为有类型字节流替身，移除对另一个无类型测试脚本私有helper的依赖。尚待修改后行为、类型和相关调用方验证，不标完成。

- 类型探针实际确认dict.fromkeys(runs)、显式None和参数化dict三种形式仍得到dict[str,Any|None]，不是database_id（其类型str）；最终只将未消费的membership值改True，键集合和首次顺序不变，不引入cast或ignore。tool_history全文件本来严格Any0，无需生产修改，保留公开入口与工具/推理identity语义，没有猜测性bug修复或无证据删除。
- 四完整测试的强Any配置原诊断history1/tool15/cold33/reasoning63，共112，加生产1→最终六文件0；Pyrefly/ty/Ruff均exit0且无定向warning。BytesIO响应提供明确headers/read/context manager能力，同一请求字节经JSON字段校验后记录，原thinking wire、预算、原生回复及冷历史关键断言保留；可空事件与model回复显式收窄，原直接DurableSession测试补finally关闭资源。没有修改生产提供者的既有Any，也不将其视为本批已治理。
- 第一轮修改后1failed/42passed是我在测试解码边界把合法bytes交给只接受str的decoded_object导致，非新生产bug；按原SSE的UTF8 JSON契约补解码，不改断言。最终`python -m pytest -q tests/test_history_routes.py tests/test_tool_display_history.py tests/test_reasoning_cold_history.py tests/test_reasoning_display.py --tb=short` → **0，43 passed，3.14秒**，q020-direct-second.json。
- 共享存储/冷启动调用方：`python -m pytest -q tests/durable/test_display_index.py tests/durable/test_history_snapshot_paging.py tests/test_server_lazy_state.py --tb=short` → **0，35 passed/6 subtests passed，18.27秒**，q020-callers.json。浏览器调用方`python -m pytest -q tests/test_ui_edit.py::test_edit_uses_target_chat_ownership_when_another_chat_is_primary --tb=short` → **0，2 passed，9.37秒**，q020-browser.json。三组范围互不重复，总 **80 passed，另6subtests passed，0失败/0skip**；基线和中间复跑不重复累计。其余UI/workflow/全仓测试未执行，不声称全部通过；没有复制或创建worktree，没有重构建/发布资产或调用外部模型。
- 生产Pyrefly **0/35suppressed/165warnings**、ty **0**、precision **0**；mypy仍在原活跃进程中，尚无最终汇总，不提前记通过。完成该门和最终hash/diff检查后才验收提交本批。

- 最终同一mypy进程已正常汇总 **0/756源**，q020-production-mypy.json，未重启门禁。六文件全部强Any/Pyrefly/ty/Ruff通过，源码在直接/调用方/浏览器及生产门之间hash一致，diff已审查。无实际生产bug修复、公开API删除或重复生产实现合并；唯一生产修改只是空聊天验证的类型精度，tool_history检查后无需修改。测试替身职责限于当前脚本，没有删改其他无类型helper的API。
- Q020声明范围验收完成，待本地提交。当前 **36/316批完整完成，70/1002生产文件属于完整完成批次**；另6个部分批次源独立验证，累计76。用户审查报告保留未跟踪；总goal保持active、全仓仍未完成。下一批Q021从事件投影依赖边界划分，先查看模块头和实际依赖，不重开已完成区域。

Q020 已提交：`edf0c69e`（refactor(history): type validation and display regression boundaries）。

### Q021 Bounded event payload codecs（进行中）

- 依据实际依赖拆原Q021五源为：Q021 event_payloads.py（551行独立codec）、Q021B event_bus.py（内存订阅/重放）、Q021C public_event_feed/durable_event_bridge/durable_events（公开投递与durable游标）。全部1002源保持唯一归属，批数316→318，完成数仍36不变。仅完整检查当前codec；bridge44/replay22行先前模块头读取即全文件，只记已读未验证，feed/bus头用于依赖归属，不重开已完成区域。
- 修改前codec严格Any配置21条诊断；直接五测试 **0，25 passed，4.42秒**，q021-codec-baseline.json。五完整测试中browser timing原0，其余四文件126条；SSE共享fixture因无类型capture_events阻止消费者精度，作为必要支持扩入当前范围，原27条诊断。原MagicMock/SimpleNamespace替身、函数/泛型及动态行消费均在本批治理，不改生产HTTP owner API。
- 未建立畸形receipt引用/非法final_delivery的正确处理契约：目前某些非法值会TypeError，字符串引用会被逐字符处理；调用方当前有效收据提供数组，尚无独立要求证明应省略、拒绝还是其他处理。保留原行为并记待确认，不按猜测增加成功返回或吞异常；继续可独立成立的类型治理与重复职责收敛。
- 四段引用处理均是按字段文本上限clip、删除空串，再由调用方保留proof2/path4/ref4或完整reason_codes；收敛为唯一_receipt_texts，保留各字段上限、顺序与TypeError错误类别。初始25项基线之外，补充对原commit edf0c69e的内存模块加载（只git show单文件，无worktree/源码复制/工作文件切换），新两参数序列用例旧版本 **0，2 passed，1.08秒**，当前实现同输入 **0，2 passed，1.13秒**，q021-receipt-original/current.json。此为重构等价基线，不作为生产bug的修改前失败证据。
- 浏览器动态字段用真实dict检查与Mapping[object,object]视图，只在消费处收窄具体整数/文本；不假定未知键和值类型。共享SSE fixture用有类型Handler/AppContext子类、真实owner检查和未绑定socket的超时状态替代SimpleNamespace，不监听端口；仍执行真实Handler._sse，patch按作用域恢复，断连/订阅关闭断言保留。原fake run_status返回{}落后于公开AppContext.run_status()->str契约，改idle字符串，仅修改夹具，不迁就错误夹具改变生产。相关消费者回归待执行后才宣称通过。

- 最终兼容性补验包括Python原本支持的仅__getitem__序列，使用运行时协议验证索引接口后沿iter调用，不缩小原合法序列集合；仍保持字符串逐字符等未确认旧行为。三个list/tuple/indexed参数旧commit **0，3 passed，1.08秒**，当前 **0，3 passed，1.14秒**，q021-receipt-original-final/current-final.json。初始两个参数基线被此完整三参数范围替代，不重复累计或假称旧实现失败。
- 八个完整源/测试（codec、browser timing、tool details、receipt bool、headless final/status、SSE helper、新序列基线）强Any/Pyrefly/ty/Ruff全部exit0，定向0warnings；原21+126+27=174条诊断归零，browser timing原0、新序列原0。两个ignored证据脚本也在同一强Any命令实际通过，共10file。过程中修正fixture只读property覆盖可写durable字段，以及ty对条件tuple字典的协变限制；通过真实字段/命名类型解决，未加cast/ignore或降级工具。
- 最终直接命令`python -m pytest -q tests/test_browser_timing_events.py tests/test_conversation_tool_details.py tests/test_event_receipt_verification_requires_exact_boolean.py tests/test_headless_final_summary_preserves_structured_answers.py tests/test_headless_tool_status_requires_exact_boolean.py tests/test_event_payload_receipt_sequences.py --tb=short` → **0，28 passed，4.91秒**，q021-codec-complete.json。最后仅命名replayed tuple收窄后SSE节点两参数亦 **0，2 passed，1.25秒**，不重复累计。
- 共享fixture及版本调用方`python -m pytest -q tests/durable/test_commit_events.py tests/durable/test_database_cursor.py tests/test_task_lifecycle_projection.py --tb=short` → **0，27 passed，3.34秒**，q021-fixture-callers.json；随后仅增加index-only receipt兼容分支及命名tuple，fixture源码/现有list/tuple路径不变，最终三种序列基线已验证，未机械重复无关游标测试。UI/headless语义调用方`python -m pytest -q tests/test_tool_result_ui_headless_semantic_parity.py tests/test_project_and_machine_outputs_preserve_final_delivery.py tests/test_headless_missing_tool_name_stays_missing.py --tb=short` → **0，9 passed，3.09秒**，q021-semantic-callers.json。最终三组互不重复 **64 passed，0失败/0skip**，不额外累计基线/旧模块探针/复跑。其他headless全文件与浏览器、事件总线、公有feed全部范围未执行，不声称全仓通过；无worktree、外部模型或资产发布。
- 最终生产门mypy **0/756file**、Pyrefly **0/35suppressed/165warnings**、ty **0**、precision **0**，q021-production-*.json/q021-precision.json；最终源码hash和diff已核对。没有确认新的生产bug，不将类型重构包装成bug修复；只合并已确认同语义的四处receipt文本处理，不删除公开入口。public_event_feed的_encode/encode/publish/stream/_write既有Any仍归Q021C，仅因共享fixture读取必要订阅/stream边界，未修改或宣称已治理。
- Q021声明范围验收完成，待本地提交。当前 **37/318批完整完成，71/1002生产文件属于完整完成批次**；另6个部分批次源独立验证，累计77。新增拆分只改变批次边界，不减少源范围；用户审查报告仍未跟踪且不暂存。下一批Q021B event_bus.py，Q021C及不明确畸形收据策略继续列队；总goal保持active。

Q021 已提交：`c0e2b48e`（refactor(events): type payload boundaries and share receipt text projection）。

### Q021B Event bus ownership / replay（进行中）

- 恢复目标与实际工作区，上一轮两个commit属于progress；HEAD仍c0e2b48e，仅用户审查报告未跟踪。当前219行event_bus源已完整读，直接scope/cursor测试已全读；文档project_structure.zh-CN.md明确总线负责重放与运行身份，公共接口为emit/sequence/observe/subscribe及带cutoff的有序重放。强Any配置生产基线69条诊断。
- **观察者重入死锁已确认：** emit持普通Lock调用外部observer，observer有限路径调用sequence/退订/订阅/emit会再次申请同一锁。公开只读游标和返回的退订句柄没有禁止回调内调用的约束；有限同步观察者不得因调用这类本地公开接口自锁，发布/重放游标必须单调。新子进程回归`python -m pytest -q tests/test_event_bus_observers.py --tb=short`修改前实际 **1，4 failed/1 passed，6.89秒**，四个都entered=true/finished=false/errors=[]，不是导入/环境失败；父子正常汇总退出，旧死锁daemon只存在于隔离探针进程，未留在pytest进程。跨线程顺序对照原本通过。另两个直接文件`python -m pytest -q tests/test_sse_cursor_reconnect_boundaries.py tests/test_task_state.py --tb=short` → **0，17 passed，3.11秒**。
- 修复将该私有锁改可重入锁，固定每次发布的observer/subscriber快照，并以FIFO发布通知处理重入emit；保留其他线程等待同步发布、原observer→subscriber顺序及异常日志，不用后台worker或吞异常。避免仅换RLock导致嵌套游标2先于1、退订跳过邻居或新订阅拿到cutoff之前事件。相同新五节点修改后 **0，5 passed，2.73秒**。当前正在完成类型边界及直接支持文件治理，尚未验收提交。

- 新增观察者异常边界：重入emit之后抛ValueError仍记录原异常日志、继续投递其余观察者和嵌套事件，后续第三次emit正常；与原跨线程同步对照一起验证FIFO状态不会被异常污染。没有把BaseException吞掉或新增后台线程。私有锁无仓内外部使用约定或直接依赖（定向引用检查）；公开观察者与队列入口全部保留，不删除兼容API。尚无可独立证明无用的生产入口，不做无依据删除。
- 完整event_bus源强Any配置 **69→0**，两个原完整直接测试在源类型已收窄后的基线31条→0；新完整回归脚本0，共4文件消除100条已记录旧诊断。以最小只读RunScope协议表达两个身份字段、Mapping入场与具体消息/队列/观察者类型约束数据，未增加cast/ignore或配置豁免。tests/test_task_state保留所有原行为断言：SimpleNamespace/object替身改真实AppContext/ProviderRegistry和满足ChatProvider的fixture，状态调用以类型化录制器保存相同connecting/running断言，自修复runner返回真实AdapterRepairResult；未为迁就错误mock改生产API。源/测试四文件强Any/Pyrefly/ty/Ruff均exit0，证据runner另强Any0。
- 直接组`python -m pytest -q tests/test_event_bus_observers.py tests/test_sse_cursor_reconnect_boundaries.py tests/test_task_state.py --tb=short` → **0，23 passed，5.09秒**，q021b-direct-first.json。最后仅测试中命名容器消除Any的task_state文件复跑 **0，13 passed，2.66秒**，不重复累计。溢出/重连组`python -m pytest -q tests/test_coldstart_hardening.py::EventBusOverflowTests tests/stress/test_sse_reconnect_duplicates.py --tb=short` → **0，7 passed，1.81秒**，q021b-replay-overflow.json；只执行指定coldstart类，不把整文件阅读/治理或其余测试记为完成。
- 桥接/版本调用方`python -m pytest -q tests/test_durable_event_bridge.py tests/test_task_lifecycle_projection.py --tb=short` → **0，9 passed，2.05秒**，q021b-bridge-callers.json。直接挂载源码的真实HTTP/SSE/Chromium/脚本provider组`python -m pytest -q tests/test_conversation_live_browser.py::test_real_stream_and_queued_input_keep_one_answer_and_survive_reload --tb=short` → **0，2 passed，16.02秒**，q021b-browser-callers.json，两协议排队输入和刷新各只保留一条答案。上述四组互不重复，合计 **41 passed，0失败/0skip**；不将基线或复跑重复累计，不声明全仓或其他浏览器/完整external_control文件已通过。
- 另有实际失败，单独披露：`python -m pytest -q tests/test_external_control.py::test_visible_creation_and_original_receipt_are_idempotent tests/test_external_control.py::test_wait_timeout_is_observation_not_cancellation tests/test_external_control.py::test_rpc_is_a_shared_service_client_with_unicode_framing --tb=short` → **1，3 setup errors，1.86秒，0测试断言执行**，q021b-public-callers.json。根因为release_helpers启动前read_runtime拒绝失配的生成清单：缺新路径52（6既有Python源＋46新asset名），引用已不存在旧asset46；不是EventBus回归。只读package_files对照在未导入event_bus的独立进程已复现差异，当前批无新增生产路径且未改runtime.json，q021b-runtime-inventory.json。三节点均在复制步骤前报错，未实际复制源码/创建worktree；识别该fixture后不再启用它或为其重生成清单，避免触及用户禁止的源码快照路径。以上3errors保留，不以skip/xfail或修改测试消失，发布清单治理与无需复制的验证归Q078；源码直接HTTP调用方验证作为本批公开投递覆盖，不混称3节点通过。
- 最终生产门mypy **0/756file**、Pyrefly **0/35suppressed/165warnings**、ty **0**、precision **0**，q021b-production-*.json/q021b-precision.json。Windows/Python3.12执行，目标类型3.11；新回归修改前4个实际行为失败、修改后同输入通过，最终源码hash稳定、diff已核对，无外部模型、无资产发布或worktree/源码复制操作。
- Q021B源码声明范围验收完成，3个额外打包集成setup错误按独立清单原因留作Q078限制，待创建本地提交。当前 **38/318批完整完成，72/1002生产文件属于完整完成批次**；另6个部分批次源独立验证，累计78。用户审查报告保留未跟踪不暂存。下一批Q021C公有feed、durable桥接与固定游标重放；总goal保持active，尚未完成全仓目标。

Q021B 已提交：`50a9b50b`（fix(events): serialize reentrant observer delivery without deadlocking）。

### Q021C Durable bridge / fixed-cutoff replay / public SSE（部分完成，保留支持事项）

- 用户重新启用总goal后继续本批，保留Q019B既有提交及其他已完成批次，不重开无变更区域。三生产源已完整阅读；public_event_feed严格Any基线63条，bridge/replay原0，q021c-source-any-before.json。未修改生产代码；用户未跟踪审查报告保留，不复制源码或worktree。
- 安全基线：`python -m pytest -q tests/test_durable_event_bridge.py tests/test_external_control.py::test_feed_encodes_once_and_isolates_slow_consumers tests/test_external_control.py::test_feed_filters_before_queueing_and_limits_bytes tests/test_external_control.py::test_internal_events_never_escape_the_public_feed tests/test_external_control.py::test_reply_stream_state_is_preserved_by_public_encoding tests/test_external_control.py::test_public_terminal_preserves_elapsed_time_and_research_navigation tests/test_external_control.py::test_large_research_details_are_an_explicit_bounded_preview --tb=short` → **0，8 passed，1.33秒**，q021c-baseline.json。明确节点不请求service fixture；原3项打包清单setup错误仍归Q078，未重试快照路径。固定游标重放及HTTP消费者尚未验证。

- **延期投递竞态已确认并独立验收：** publisher观察其他线程持协调锁后、append之前被可控Event暂停；持锁操作释放后调用最后一次flush，旧实现读空并返回，再append使已提交事件无人发布。AppContext以该bridge作为durable外部commit listener，task_lifecycle的finish/stop/expiry明确在协调锁外flush；“延期投递不能丢在最后一次drain之后”是桥接所有权不变量，不由测试自定。`python -m pytest -q tests/test_durable_event_bridge.py::test_flush_cannot_miss_a_publisher_deferring_behind_another_operation --tb=short` 修改前实际 **1，1 failed，1.22秒**，断言实际发布0而非1；q021c-bridge-before.json。不是线程/导入错误，两个future正常完成。
- 新私有短锁把locked检查/append与flush取空/提取/清空串行化；flush统一按coordination→deferred顺序取锁，publish检查不申请coordination锁，实际projection在两锁外，不新增worker、轮询或生产等待。相同交错修复后通过；补充立即发布、noop、持锁延期、回调验证两锁可立即取得、回调产生的下一次commit继续drain，完整bridge脚本5节点最终通过，包含在q021c-direct-complete.json的38项中。完整bridge源/脚本严格Any、Pyrefly、ty、Ruff均0，未删除公开入口或添加ignore。
- bridge调用方明确批次`python -m pytest -q tests/test_task_lifecycle_coordinator.py tests/test_task_submit_durable.py tests/test_shell_approval_lifecycle.py tests/test_conversation_lifecycle.py --tb=short` → **0，49 passed，36.45秒**，q021c-bridge-callers.json，未执行worktree/snapshot入口。桥接修复与后续feed修复拆为独立本地提交；其余feed/重放治理仍在进行，不把本项提交当整批完成。
- 调查期间HEAD外部推进到13b05d6c、4eef1a78，四个新增自有源按模块头/导入归入Q124(request_metrics)、Q132(task_prompt)、Q181(task_diff)、Q226(ecmascript)，未深入检查或修改；总范围1002→1006、批数318不变。API(Q015C)和server(Q015F)被外部修改，撤销这两源旧验收有效性并记录必须复验，不撤回外部代码。Q015C重新转部分完成；其余已验收范围不重开。外部同时更新runtime清单/资产，Q021B旧清单失配不再作为当前清单的直接证明；Q078仍需无需复制的验证，禁止启用会复制源码的service fixture。

Q021C 桥接竞态已独立提交：`0c51f863`（fix(events): synchronize durable deferral with the final drain）。

- **非法游标损失信号缺口：** `publish`在既有TypeError/ValueError处理区外转换commit_seq，非整数字符串、非空list/dict导致异常直接外抛，订阅仍保持打开；后续事件推进游标时无法知道丢了提交。独立依据为external-control-transports-and-updates第5.1/5.2节：公共编码失败必须可观察并重新同步，丢失不能仅把后续commit游标前移。已有同方法的序列化失败策略就是关闭订阅，此修复将游标转换纳入相同边界，不改为默认成功值或吞掉损失信号。
- 在本批类型重构之前的`50a9b50b:codey/app/public_event_feed.py` Git blob内存加载版本，三个原输入/原断言实际 **1，3 failed，1.19秒**（ValueError/TypeError），q021c-invalid-cursor-original.json；未复制源码树或切换工作文件。当前相同三输入 **0，3 passed，1.20秒**，q021c-invalid-cursor-after.json。为了保持提交独立，这三个节点原样迁到test_public_event_feed_invalid_cursor.py（仅显式命名event字典以隔离旧源Any上下文），随后以该独立脚本和最小原转换实现补验；不改变断言或预期。
- 独立脚本对旧Git源再验 **1，3 failed，1.21秒**，q021c-invalid-cursor-independent-original.json。最小暂存变更仅把原int转换/缓存键两行移入try，未包含后续缓存/类型工作；实际加载index blob，三个回归＋原六个纯feed节点 **0，9 passed，1.31秒**，q021c-invalid-cursor-index-fixed.json。初次PowerShell文本管道的补丁换行未应用，index仍是旧实现，探针如实记录3failed/6passed；已用二进制patch输入纠正后才取得此9passed，不将失败探针写成通过。

Q021C 非法游标损失信号已独立提交：`c0d60512`（fix(events): signal subscriber loss on invalid commit cursors）。

- **默认游标缓存串帧：** 公开encode(event, sequence=0)对两个会话/不同文本复用同一("event",0)正文，第二个返回的session元数据与正文不一致；随后private_diagnostic也直接命中旧缓存而不经过公开事件准入。默认0不具备唯一不可变事件身份，违反正文必须属于当前事件/作用域的不变量及文档5.1的视图隔离契约。新两个节点修改前 **1，2 failed，1.22秒**（实际first!=second；PrivateEvent未抛），q021c-cache-behavior-before.json；相同输入修改后 **0，2 passed，1.16秒**，q021c-cache-after.json。初稿曾用完整dict等值误漏有效schema_version，先把断言限定为独立契约中的session/text字段后，才取得此有效行为失败，未拿初稿失败作为串帧证据。
- 只有正的event/commit身份参与缓存，无身份正文仍投影、准入、编码和校验单帧上限，但不读写或驱逐共享缓存。有效身份继续正文对象复用，event与commit键空间分离，字节及1000项上限保留。两个同语义缓存键构造归唯一_cache_key，三个同语义游标转换归_commit_cursor；整数转换在边界运行时收窄，不按非法值返回0。HTTP owner用真实BaseHTTPRequestHandler/AppContext，connection从object检查为实际socket后消费，TYPE_CHECKING避免导入环；不加cast/ignore/依赖或关闭检查。
- 三源完整严格Any为feed **63→0**，bridge/replay原0；已全读的commit_events/database_cursor两脚本原43诊断→0。最初支持审计47条含新feed测试4条旧生产签名造成的上下文Any，不将其重复计作旧测试债务。fixture参数先入object验证，projection行入共享校验，commit listener使用具体CommitResult/DurableMutation并证明record存在，失败写入替身参数完整标注；原用例/关键断言保留。新两个回归脚本及bridge整文件0；最终三源＋五测试强Any/Pyrefly/ty/Ruff全部exit0，三个ignored复现/记录脚本也实际强Any0（共11file），q021c-final-local-*.json。durable_events源无需修改；新两页大小(1/512)对照证实重放使用初始cutoff，不被持续到达的新提交拖住或越界。
- 最终直接组`python -m pytest -q tests/test_public_event_feed.py tests/test_public_event_feed_invalid_cursor.py tests/test_durable_event_bridge.py tests/durable/test_commit_events.py tests/durable/test_database_cursor.py --tb=short` → **0，38 passed，3.45秒**，q021c-direct-split-complete.json。feed/历史/投影调用方`python -m pytest -q tests/test_external_control.py::test_feed_encodes_once_and_isolates_slow_consumers tests/test_external_control.py::test_feed_filters_before_queueing_and_limits_bytes tests/test_external_control.py::test_internal_events_never_escape_the_public_feed tests/test_external_control.py::test_reply_stream_state_is_preserved_by_public_encoding tests/test_external_control.py::test_public_terminal_preserves_elapsed_time_and_research_navigation tests/test_external_control.py::test_large_research_details_are_an_explicit_bounded_preview tests/test_conversation_tool_details.py tests/test_history_routes.py tests/test_task_lifecycle_projection.py --tb=short` → **0，31 passed，3.31秒**，q021c-feed-callers.json。加上述49项bridge调用方，非浏览器互不重复范围共 **118 passed，0失败/0skip**；不另累计基线/中间复跑。feed调用方时bridge仅尚未将等价嵌套with合为多context语法，之后bridge直接组和49调用方已用最终源码验证；feed自身hash全程相同。
- **浏览器与支持脚本未完成，禁止用重跑覆盖失败：** 同节点`tests/test_conversation_live_browser.py::test_real_stream_and_queued_input_keep_one_answer_and_survive_reload`的三次双协议运行均 **1，1 failed/1 passed**（30.26/25.89/25.48秒，q021c-browser-callers/observed/final-current.json），completions在首次发送前button disabled，未执行其生成断言。断言前截图/DOM证实UI显示live-fixture Unavailable；同次认证HTTP model_settings却列出该模型且preferences.local启用，证据q021c-browser-disabled-state.json/png。当前单独completions **0，1 passed，8.17秒**；旧两源Git blob内存对照单节点 **0，1 passed，8.15秒**、双协议 **0，2 passed，14.32秒**；显式预加载相同两模块的当前源码双协议 **0，2 passed，15.36秒**（q021c-browser-current-single/original/original-pair/preloaded-current.json）。预加载改变初始化条件，此结果不等于常规入口稳定通过，不据此宣称与源码变更无关或根因已修复；保留为初始化/模型目录/页面状态交互待定位，不按猜测改生产或放宽断言。已两轮调查但未证实根因，停止追加同条件重跑，拆出后续事项。
- 为定位该失败读了browser脚本的peer/seed/open_live/send与目标节点；整文件严格Any实际 **463条**，主要缺失contextmanager/HTTP handler/fixture参数和返回、JSON字段与回调容器，q021c-diagnostic-any.json。该原文件未修改，不宣称已治理；ignored诊断脚本自身唯一容器诊断已用具体dict字段修正并实际检查0。前端models/provider_ui仅定位必要目录消费函数，未作全文件检查/修改或宣称其类型治理完成。浏览器支持类型债务和常规入口不稳定均保留Q021C待办，因此本批仍是**部分完成**，不能标整批验收。
- 最终生产门已在外部提交稳定后串行补验：`python -m mypy codey --python-version 3.11` **0/760file**、`pyrefly check codey --python-version 3.11` **0/35suppressed/166warnings**、`ty check codey --python-version 3.11` **0**、`python -m tools.kernel_type_precision_gate` **0**，q021c-final-production-*.json/q021c-final-precision.json，记录起止HEAD及三源hash；没有把166warnings说成0。额外`--min-severity warn --output-format json`诊断命令实际exit1（warning级门），记录q021c-production-warnings.json；本批三源没有warning，未改无关生产源去压总warning数。Windows/Python3.12实际运行，类型目标3.11，不代表其他平台实测。
- 待缓存/类型提交落地后，Q021C三个生产源的定向验证成果保留，整批因上述两支持事项仍部分完成。外部API改动使Q015C重新待验，当前 **37/318完整批次、71/1006完整批次生产源**，另8个部分批次生产源有独立验证，累计79；不通过缩小范围/隐藏失效结果维持旧完成率。下一主批优先复验实际被外部改动的Q015C及Q015F必要HTTP边界，再进Q022，不无限停留本批；总goal继续active，未完成全仓任务。用户审查报告不暂存；无worktree/源码树复制、外部模型生成或自行构建/发布资产。

Q021C 缓存/类型成果已提交：`54940c4b`（fix(events): isolate uncursored frames and type public feed boundaries）；整批仍部分完成。

### Q015C 外部task-diff HTTP接口变更复验（已完成）

- 重开依据仅为13b05d6c/4eef1a78实际新增run_changes_response及GET路由绑定，未重新泛读已完成API/server。任务差异前端调用明确提供session_id/run_id；已有operator会话契约及按历史run归属读取契约有效。新增真实本地HTTP基线覆盖未认证401、缺标识400、两个已冻结run各自正文、跨会话/未知run/别名404，不修改生产代码、原断言、授权或调度行为，不使用复制式service fixture。
- 必要依赖task_diff.py新增61行源现已完整读（原先仅文件头），tests/test_task_diff.py只读了首个历史身份用例来确认公开fixture用法；完整类型基线与直接行为尚待运行。该依赖生产治理归Q181，其他ledger实现只定位公开open/append签名，不作全文件完成声明。Q015F仅复验新增路由及整个文件类型，原共享server测试1673Any债务继续保留。

- 基线`python -m pytest -q tests/test_run_changes_http.py tests/test_task_diff.py tests/test_task_input_contract.py tests/test_task_input_and_output_contracts.py --tb=short` → **0，48 passed，10.53秒**，q015c-recheck-baseline.json。生产无行为失败，不按猜测修复新接口。三完整生产源＋两直接脚本＋证据runner强Any基线只有task_diff测试1项attr-defined：通过changes间接引用未显式导出的_read_text_or_none，不是Any/生产功能错误。其余五文件实际0。随后读完95行task_diff脚本，将保存原reader的引用改为其原定义处公开read_text_or_none；原changes调用点monkeypatch、side_effect以及全部行为断言保留，未改变生产重导出或放宽测试。
- 最终直接`python -m pytest -q tests/test_run_changes_http.py tests/test_task_diff.py --tb=short` → **0，10 passed，4.75秒**，q015c-recheck-direct.json。原48基线里另38项既有input/output合同的脚本/生产源码未变，必要修改过的reader用例已在此10项复跑，不机械重复38项。HTTP/owner/审核调用方`python -m pytest -q tests/test_server_boundaries.py tests/test_control_runtime_ownership.py tests/test_operator_auth_blocks_unauthenticated_http.py tests/test_review_submission_rejects_coerced_source_ids.py --tb=short` → **0，33 passed，15.67秒**，q015c-recheck-callers.json。本次互不重复范围48+33=**81 passed、0失败/0skip**，直接10是其中复跑，不再次累加；其他后台、发布、UI与全仓范围未执行。
- API、server、task_diff三完整源与新HTTP/原task_diff两完整脚本强Any/Pyrefly/ty/Ruff均exit0（Pyrefly披露15个既有warning）；证据runner强Any亦0，共6file，q015c-recheck-local-*.json。task_diff两个函数在API读取、task_entry/project_completion_flow落盘各有真实入口，不删除；保存/读取职责不同，没有强行抽象。task_diff源已读且独立类型/行为验证，归Q181的其他ledger/details/projection源未读，不把整个Q181标完成。保留完整生产树上一批的mypy760/Pyrefly35suppressed166warnings/ty/precision通过结果：此后无生产文件修改，当前仅测试引用/新增HTTP基线和记录，不重复同一生产门。
- 本次无生产代码修改、无新确认bug、无忽略/断言放宽；恢复Q015C已完成和API源有效验证，恢复Q015F的server源有效验证，但Q015F仍因既有共享server测试类型债务部分完成。Q021C浏览器启动/463Any仍未解决。当前 **38/318完整批次、72/1006完整批次生产源**，另10个部分批次生产源独立验证，累计82；source/task_diff仅1源贡献独立验证，不伪称整个runs区域完成。准备提交新增HTTP验证和reader测试引用修正，用户审查报告保留不暂存；下一主批Q022 knowledge/memory，先按职责拆分大型区再进未检查文件，总goal仍active。

Q015C HTTP复验及测试类型引用已提交：a71eaa40（test(api): verify authenticated historical task diff reads）。后续主批Q022尚未开始；本次仅补记提交与实测行数，不新增测试执行或完成声明。

### Q022 Knowledge index rebuild admission（已完成）

- 原8源按实际职责拆为Q022 indexer（1源）、Q022B memory装配/来源生命周期（1源）、Q022C已结算事实投影（4源）、Q022D管理路由/反馈（2源），总321批、1006自有源归属唯一，未减少范围。仅indexer68行及其完整直接测试深入检查；memory_verification头42行就是完整文件，记Q022C已读但未验证，其余仅模块头/导入定位。不重开已完成批次，不复制源码树/worktree。
- 原source/直接脚本强Any实际0，q022-any-before.json。基线`python -m pytest -q tests/test_app_background_workers.py tests/test_architecture.py::ArchitectureBoundaryTests::test_knowledge_indexer_owns_single_flight_state --tb=short` → **0，3 passed/2 subtests passed，1.19秒**，q022-baseline-corrected.json；首次误写class名的命令exit4/no tests ran（执行0项，工具未给出收集数，q022-baseline.json），不是行为复现或通过证据。
- **启动失败留下虚假单worker所有权：** Thread.start抛RuntimeError（Python公开启动失败异常）发生在running=true之后，当前实现既不撤销标志又在状态锁外启动；单次失败后下次schedule只会设pending，同时失败窗口内的另一schedule也会返回却无人执行重建。独立依据为single-flight所有权不变量：只有实际被接纳且仍活跃的worker才拥有running标志；失败启动必须把失败报告给原调用方，不能让随后/并发正常返回的请求绑定到不存在的worker。AppContext恢复研究修改通过该公开schedule入口安排重建，真实provider只读取已有_knowledge_store字段，不把假设的lazy getter异常当本批bug。
- 新完整脚本`python -m pytest -q tests/test_knowledge_indexer_startup.py --tb=short`修改前实际 **1，2 failed，1.27秒**，q022-startup-before.json：启动异常已按原合同被捕获后running仍true；可控start暂停/失败、并发调度正常返回且重建次数为0。所有辅助线程均已join终止，无导入/环境/线程未回收错误；未实际耗尽系统线程来制造失败。尚未修改生产代码，下面仅修复该已复现准入状态问题。

- 修复把现有状态登记及Thread.start放到同一准入锁中；仅RuntimeError启动失败时撤销running并重新抛出原异常，其他调度者只能在启动成功/失败状态确定后合并或接纳。不增加worker、重试、后台队列或吞异常，原rebuild在工作线程执行、pending合并及重建错误记录保持。相同两个回归与旧直接脚本修改后 **0，4 passed，1.82秒**，q022-startup-after.json。追加store为None不启动线程的边界；原有重入schedule/pending及rebuild抛异常的断言全部保留。
- 最终直接命令`python -m pytest -q tests/test_knowledge_indexer_startup.py tests/test_app_background_workers.py tests/test_architecture.py::ArchitectureBoundaryTests::test_knowledge_indexer_owns_single_flight_state --tb=short` → **0，6 passed/2 subtests passed，1.87秒**，q022-final-direct.json。恢复修改调用方`python -m pytest -q tests/test_server.py::RunSnapshotTests::test_restore_research_changes_schedules_rebuild_in_background tests/test_server.py::RunSnapshotTests::test_restore_research_changes_coalesces_background_rebuilds tests/test_server.py::RunSnapshotTests::test_restore_research_changes_rebuild_error_does_not_change_restore_result tests/test_server.py::ResearchServerHelperTests::test_research_restore_response_preserves_status_mapping --tb=short` → **0，4 passed，3.46秒**，q022-restore-callers.json。两组互不重复共 **10 passed，另2 subtests passed，0失败/0skip**，不额外累计基线/中间复跑。不将整个architecture/server脚本说成已运行或已类型治理；既有server脚本债务仍归Q015F。
- 源与原直接脚本强Any原0；新回归最初有4条Thread类构造器Any间接传播，在测试只消费/替换start的边界使用具体_ThreadStarter协议（显式Thread参数，位置参数合同）后归0，不用cast/ignore或宽泛object隐藏接口。Pyrefly最初指出命名参数差异、Ruff指出两个with可合并，均已修正；完整三个源/测试严格Any/Pyrefly/ty/Ruff均exit0，ignored证据runner强Any也0（共4file），q022-final-local-*.json。原测试无需修改；无可证明可删的公开类/字段，无同语义生产实现需要合并，未做无关重构。
- 生产门按要求串行执行：mypy **0/760file**、Pyrefly **0/35suppressed/166warnings**、ty **0**、kernel precision **0**，完整命令/退出码/源码hash与起止HEAD在q022-production-*.json/q022-precision.json。当前单源hash在有效复现后的相关验证与门禁间保持一致；Windows/Python3.12实际运行，类型目标3.11，不声明其他平台实测。工作区仅本批源码/新回归/两进度文件及用户未跟踪审查报告，后者不暂存。
- Q022声明范围验收完成，待本地提交。累计 **39/321完整批次、73/1006完整批次生产源**；另10个部分批次生产源独立验证，累计83。Q022B/C/D未完成，Q021C浏览器启动/463Any仍保留，不用该批通过掩盖其他失败；下一主批Q022B memory_setup.py装配与来源生命周期，总goal继续active。

Q022 已提交：`2d23e891`（fix(knowledge): roll back failed rebuild worker admission）。

### Q022B Memory composition / source lifetime（已完成，无需修改）

- memory_setup.py完整143行已读，直接依据memory-boundary及memory-completion当前有效文档：具体Ghost导入只在装配点，禁用/缺实现用Null，缺少后端内部依赖不得冒充可选未安装；经历从真实终态commit和submission最终answer读取，消息身份/摘要仍在当前分支才可用，失败投影不回滚终态。源与现有工作区无外部修改；先做全文件Any及明确测试文件基线，不预判生产或测试正确性。

- 直接范围`python -m pytest -q tests/test_memory_boundary.py tests/test_memory_integrity.py --tb=short` → **0，43 passed，12.58秒**，q022b-baseline.json；原脚本仅按入口/节点名定位与执行，未修改或宣称全文件类型治理。覆盖现有Ghost缺失/禁用、替代后端、实际答案、消息身份/摘要、分支撤回与终态取消边界。维护调用方`python -m pytest -q tests/test_memory_maintenance_runtime.py --tb=short` → **0，2 passed，1.62秒**，q022b-maintenance-callers.json。两个明确范围共45passed、0失败/0skip，不运行全仓或复制式service夹具。
- 完整装配源与证据runner强Any均0；源Pyrefly/ty/Ruff exit0，q022b-source-*.json。独立进程`python -c "import sys; import codey.app.memory_setup; assert 'codey.ghost.backend' not in sys.modules; print('composition import leaves concrete backend unloaded')"`实际exit0，q022b-fresh-import.json；这是一次非pytest导入验证，不计入45测试。所有验证源码hash一致。此后无生产修改，沿用Q022刚执行的生产mypy760/Pyrefly35suppressed166warnings/ty/precision通过记录，不机械重复。
- 公开入口build_memory/source_available/message_source_ref/observe_terminal分别由Context、管理路由、MemoryService和idle维护消费，具体后端动态导入是明确装配约定，不能按“静态导入少”删掉；消息文本摘要校验与普通记录/管理来源的校验语义不同，未为表面重复强建抽象。无确定新bug证据，无死代码删除、生产重构或类型修改需要执行。该单源声明范围完成，累计 **40/321完整批次、74/1006完整批次生产源**，另10部分源独立验证，累计84；不代表整个memory区域无bug。无代码变更不创建空commit，记录随下一次实质变更提交；下一Q022C四源，其他旧失败/债务保留，总goal active。

### Q022C Committed memory fact projections（已完成）

- 范围固定为memory_behaviors/memory_candidates/memory_verification/memory_observation四源（52/135/42/91行）。verification此前全读，不重复扫描；当前补足其余三个完整文件，先建立候选/回执/终态语义基线及完整Any审计，再处理有证据的问题。

- 四源现已完整阅读，生产代码尚未修改。基线`python -m pytest -q tests/test_memory_completion.py tests/test_memory_feedback.py tests/test_memory_learning_targets.py --tb=short` → **0，37 passed，19.77秒**，q022c-baseline.json；正常提取/确认/纠正/重建/删除调用方`python -m pytest -q tests/test_memory_adaptation_flow.py --tb=short` → **0，5 passed，11.49秒**，q022c-adaptation-callers.json。两个明确范围共42passed、0失败/0skip，不把重复命名/已阅读计作执行，不包含其他memory脚本。这里只定位/执行现有脚本，不宣称这些脚本已完成整文件Any治理。
- 四源与证据runner完整严格Any实际0（5file），四源Pyrefly/ty/Ruff均exit0，q022c-source-*.json；不为制造提交修改已合规类型。候选原话/区间、实际接受并交付的响应、关闭确认排除及“工具成功不等于正式验证”的有效契约来自memory-boundary/completion文档，不以当前断言或另一实现作为唯一正确性依据。当前没有新的有效失败复现，不能宣称已修复bug或整批已完成。
- 下一项核对：accepted_done_decision与accepted_response_ref用途不同，前者读取证明、后者还要求已完成任务/响应并排除close_reply；校验条件也不完全相同，不能因代码相似直接合并。已定位KernelContext.advance的task-only工件发布（owner_effect_id=None）及decision读取合同，并查到真实RuntimeOperationState/checkpoint/工件注册公开签名；这些依赖仅为契约/fixture边界定位，未宣称其整文件治理完成。下一步应以独立的task/effect完整归属不变量建立真实checkpoint＋artifact最小输入，确认是否存在错误接受或无效工件读取；此前不改生产代码。若无法确定正确契约/建立有效失败，保留待确认并按两轮规则推进Q022D，不能长期卡在几段相似实现。
- 当前仍 **40/321完整批次、74/1006完整批次生产源**，另10个部分源独立验证；Q022C只标进行中，不把42通过和零Any自动当整批验收。无运行中的测试或门禁进程待观察，实际HEAD为2d23e891；工作区仅两份本轮进度记录与用户审查报告（未跟踪），记录随下次实质修改提交。总goal active，未暂停或缩小总体范围。

- **task决策工件的effect归属被遗漏：** KernelContext.advance发布kernel_decision的公开持久化边界固定为(owner_task_id=run, owner_effect_id=None)，因此task级决策完整所有权是该二元组，不能仅因task相同就把某个effect的字节提升为task决策。这一独立归属不变量及生产写入合同是预期依据，另一个reader的校验仅作旁证。实际accepted_done_decision只比task/kind/摘要，接受同task但带effect归属的工件，并经accepted_completion暴露为已接受结果。
- 新回归使用真实Memory/SQLite DurableSession、EffectRecord、ManagedOutputStore JSON工件、RuntimeOperationState序列化和任务时间线，无mock替代目标解码/归属验证。首稿缺少schema要求的driver，四项均在checkpoint校验处失败（q022c-ownership-before.json），不是bug证据；在夹具补足writer driver后，合法task-only对照两后端均通过。`python -m pytest -q tests/test_memory_decision_ownership.py --tb=short`修改前实际 **1，4 failed/2 passed，1.67秒**，q022c-ownership-behavior-before.json：两个reader×两个后端实际未拒绝effect-owned同内容工件，均DID NOT RAISE ValueError。目标路径已实际读取并接受，非导入/环境/语法或夹具错误。尚未修改生产代码，接下来只补完整所有权检查，不合并用途不同的读取入口。

- 仅在accepted_done_decision原归属检查增加owner_effect_id is None要求；沿用原ValueError及消息、原摘要/版本/解码校验顺序，合法输入的结果不变。相同六节点修改后 **0，6 passed，1.60秒**，q022c-ownership-after.json。不改checkpoint、工件生产方、目标测试预期或数据库约束，不把恢复失败转成默认成功。accepted_response_ref的终态/响应/关闭确认条件与本入口语义不同，未强行合并；其他公开投影入口各有实际调用方，不按“外部引用少”删除。
- 最终直接`python -m pytest -q tests/test_memory_decision_ownership.py tests/test_memory_completion.py tests/test_memory_feedback.py tests/test_memory_learning_targets.py --tb=short` → **0，43 passed，20.04秒**，q022c-direct.json。调用方`python -m pytest -q tests/test_memory_adaptation_flow.py tests/test_memory_maintenance_runtime.py tests/test_memory_boundary.py::test_terminal_projection_cancellation_cannot_change_committed_result --tb=short` → **0，9 passed，12.77秒**，q022c-callers.json。两范围共52passed、0失败/0skip；不累加修改前基线/中间重复节点。Q022B的必要终态/维护消费者因真实依赖变更重跑，装配源码未改、验证结论保留；未重开其无关分支。新增回归完整文件已强Any/Pyrefly/ty/Ruff验证0，原脚本只是明确范围的行为验证，不宣称其全文件类型治理。
- 四完整生产源＋新回归强Any/Pyrefly/ty/Ruff全部exit0（runner也强Any0，共6file），q022c-local-*.json；无cast/ignore/检查降级/依赖。生产门串行：mypy **0/760file**、Pyrefly **0/35suppressed/166warnings**、ty **0**、kernel precision **0**，q022c-production-*.json/q022c-precision.json。修复后源码hash与起止HEAD稳定，Windows/Python3.12执行、类型目标3.11；不声称其他平台实测。
- 必要契约/夹具依赖另作全文件Any基线，不能隐去未治理来源：`python -m mypy codey/operations/kernel_context.py codey/operations/kernel_contracts.py codey/durable/checkpoint.py codey/runtime/core/operation_payload.py --python-version 3.11 --follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-decorated` → **1，21 diagnostics/4 files**，q022c-dependency-any-baseline.json。两operations源0；checkpoint.py13条（69/91-172的dict isinstance后无泛型及**字典传播），operation_payload.py8条（47/60/82/280/393/396的set/zip/字典/布尔传播）。四依赖均未修改，新增目标检查与这些局部诊断无数据/类型传播关系；普通生产三类型门仍通过。残余源分别明确留Q056/Q192，必要边界已用具体公开类型约束当前回归，不扩展为两大运行时文件重写，未宣称这些依赖已完成治理。若后续检查其完整模块，须连同行为基线和原精度门一起完成，不只改为宽泛object。
- Q022C四源声明范围验收完成，待本地提交。累计 **41/321完整批次、78/1006完整批次生产源**，另10部分源独立验证，累计88；21条依赖Any及Q021C浏览器/463Any未解决，不能以此批完成冒称全仓完成。仅提交memory_candidates、新回归及两份进度；同时保存Q022B无需修改的检查记录，不夹带用户未跟踪报告。下一主批Q022D管理路由/显式反馈两源，goal保持active。

Q022C 已提交：`87d0873f`（fix(memory): require task-only ownership for completion decisions）。

### Q022D Memory management / explicit feedback（已完成）

- 两源memory_routes.py364行、memory_learning.py76行已完整读。入口/api/ghost/action由app_api.ghost_action_response唯一转发；管理action为明确字符串命令，ValueError按既有客户端错误返回400，后端/投影不可用才是503。候选确认、跨作用域、原话摘要与request_id内容不可改写的合同仍来自memory-boundary/completion文档；不把现有测试自动当正确答案。先运行管理/反馈基线及全文件Any，再复现复杂JSON action值可能错误进入503的路径。
- 已确认summary中items/exposures两个列表过滤与现有_scoped_values完全同语义：同一state key读取、dict条件、scope tuple成员判定、顺序、行对象身份、异常及无额外副作用一致，可在行为基线后收敛。与输入类型bug分成独立提交，暂不同时修改。

- 基线`python -m pytest -q tests/test_memory_lifecycle.py tests/test_memory_learning_targets.py tests/test_memory_adaptation_flow.py --tb=short` → **0，34 passed，13.33秒**，q022d-baseline.json；两完整源与runner严格Any均0（3file），q022d-source-any-before.json。原脚本仅路由/节点名定位并执行，未改动或声称全文件类型治理。
- **非法action错误报告为后端不可用：** /api/ghost/action的action是单一字符串命令；缺失/未知/错误类型均属客户端请求错误，现有DTO参数校验及HTTP400客户端错误合同有效，503用于服务/后端暂不可用，不能仅因JSON容器不可hash就误判后端故障。数组/对象payload经正常JSON解码后直接参与set成员判断，在访问任何后端前TypeError，进入服务错误503分支。使用真实AppContext(memory_enabled=False)和已存在会话，新脚本`python -m pytest -q tests/test_memory_action_input.py --tb=short`修改前实际 **1，4 failed/5 passed，1.69秒**，q022d-action-before.json；[]、非空list、{}、非空dict实际503而非400；null/bool/int/float及未知字符串对照原本400。不是测试/环境或生产后端错误，未增加mock异常或重写预期；下面只在公开入口使用已有require_json_string校验action。

- 第一项修复只将memory_action公开入口的action读取改为require_json_string；在命令分派与后端访问之前完成类型校验，仍由既有ValueError分支返回400，真实服务错误503分支不动。不对私有下游重复校验或把未知数据按str强转。相同9节点 **0，9 passed，1.60秒**，q022d-action-after.json；所有拒绝路径断言无Core commit推进。
- 直接`python -m pytest -q tests/test_memory_action_input.py tests/test_memory_lifecycle.py tests/test_memory_learning_targets.py --tb=short` → **0，38 passed，3.39秒**，q022d-action-direct.json；反馈/适应调用方`python -m pytest -q tests/test_memory_feedback.py tests/test_memory_adaptation_flow.py --tb=short` → **0，15 passed，15.87秒**，q022d-action-callers.json。两范围共53passed、0失败/0skip，基线和复跑不另累加。两完整生产源＋新完整脚本严格Any/Pyrefly/ty/Ruff全部0，runner强Any也0（4file），q022d-action-local-*.json。
- 生产门串行mypy **0/760file**、Pyrefly **0/35suppressed/166warnings**、ty **0**、precision **0**，q022d-action-production-*.json/q022d-action-precision.json，源码hash/HEAD一致；第一项具备独立提交条件。后续summary重复职责收敛仍未执行，本批整体保持进行中，不以此53项通过提前结项。用户报告不暂存，未修改既有测试预期或复制工作区。

Q022D action类型修复已提交：`bc7ff75b`（fix(memory): validate action tags before command dispatch）。

- **summary范围参数与后端故障混淆：** selected_scope的有效DTO仅user/session/project，后两者要求对应标识，错误selector/缺标识属于请求错误（HTTP400），不是“memory unavailable”（503）。原summary把这一步和后端读取放在同一个except Exception中，未知/空scope_kind、project缺project、session缺session_id都返回503。新脚本`python -m pytest -q tests/test_memory_summary_input.py --tb=short`修改前实际 **1，4 failed/2 passed，1.66秒**，q022d-summary-before.json；真实健康Ghost实例的合法user查询200，以及合法查询下明确注入的export_state ValueError仍503，两对照均通过。四失败均是503!=400，非夹具/导入问题，source仍未改。预期来自scope DTO约束与客户端/服务错误分类，不是照抄另一reader；后端ValueError不能一起改成400。

- 在原外层服务错误保护内，单独捕获selected_scope的ValueError为400，后端ValueError、其他scope解析错误仍按原合同503。第一版直接拆成两外层try虽通过初始6项，却会漏捕scope解析OSError；追加保留原服务错误语义的对照实际 **1，1 failed，1.34秒**（q022d-summary-scope-control-before.json），这是本轮未提交初稿引入的回归，不冒充原仓库bug。改为嵌套边界后完整7项 **0，7 passed，1.54秒**，q022d-summary-after-controls.json。没有放宽断言/吞异常或把后端故障改成功。
- 直接`python -m pytest -q tests/test_memory_summary_input.py tests/test_memory_action_input.py tests/test_memory_lifecycle.py tests/test_memory_learning_targets.py --tb=short` → **0，45 passed，4.31秒**，q022d-summary-direct.json；调用方`python -m pytest -q tests/test_memory_adaptation_flow.py tests/test_memory_boundary.py::test_review_routes_and_source_deletion --tb=short` → **0，6 passed，11.73秒**，q022d-summary-callers.json。当前51项互不重复，前一修复53项与之有交集，不机械相加为唯一覆盖。两完整源＋两完整新测试严格Any/Pyrefly/ty/Ruff0，runner强Any亦0，共5file，q022d-summary-local-*.json。生产mypy760/Pyrefly35suppressed166warnings/ty/precision串行均exit0，q022d-summary-production-*.json/q022d-summary-precision.json；第二修复具备独立提交条件，待summary过滤收敛后才整批结项。

Q022D summary范围校验修复已提交：`458c9730`（fix(memory): classify summary scope validation as client errors）。

- 重构前补充公共summary范围基线：以真实MemoryItem/MemoryExposure合同生成完整DTO，仅通过有类型export_state夹具给出混合session/foreign-project/user的快照，验证两个字段只保留当前session＋user且原相对顺序不变。当前旧循环实现完整summary脚本 **0，8 passed，1.70秒**，q022d-filter-baseline.json；这不是bug的修改前失败，而是行为保持基线。随后只将items/exposures的相同过滤交给现有_scoped_values，删除两处被替代的循环及其局部临时变量，无新抽象/公开API变更；原行对象不复制、字段读取位置与过滤次序不变。
- 重构后`python -m pytest -q tests/test_memory_summary_input.py tests/test_memory_lifecycle.py::test_summary_of_parent_controls_excludes_child_denials tests/test_memory_learning_targets.py::test_management_calibration_does_not_include_another_project tests/test_memory_boundary.py::test_review_routes_and_source_deletion --tb=short` → **0，11 passed，2.09秒**，q022d-filter-after.json。这覆盖所有被修改summary行为的直接回归与scope/校准/管理消费者；action与feedback函数未在此重构改变，沿用明确未受影响的此前成功结果，不机械重跑整个已通过集合。
- 本批各阶段实际执行过且验收仍有效的不重复节点并集为 **62 passed、0失败/0skip**（action9、summary最终8、lifecycle24、learning_targets5、feedback10、adaptation5、review-route1），不是把53＋51＋11相加；新summary过滤测试已在重构前后执行。两完整源＋两完整新脚本严格Any/Pyrefly/ty/Ruff全部0，runner强Any0（5file），q022d-filter-local-*.json。未全面阅读/修改的原脚本仅计行为执行，不宣称其整文件Any完成。
- 最终生产门在真实重构后串行补验：mypy **0/760file**、Pyrefly **0/35suppressed/166warnings**、ty **0**、kernel precision **0**，q022d-final-production-*.json/q022d-final-precision.json，记录当前源码hash和起止HEAD，无测试或门禁进程遗留。前后两确定性bug均已保存有效失败→相同输入通过证据；类型原0的源未做强行改动，memory_learning仅检查/验证后保留。源导入/语法由真实pytest与三类型门验证，未触及前端/打包入口、发布资产或复制源码树。
- Q022D两源声明范围验收完成，待独立过滤收敛提交。累计 **42/321完整批次、80/1006完整批次生产源**；另10个部分源独立验证，累计90。21条运行时依赖Any和Q021C浏览器/463Any仍未解决；总goal active，全仓未完成。下一主批Q023仅memory_maintenance.py的原scheduler维护适配与游标生命周期；用户审查报告始终不暂存。

Q022D 过滤收敛已提交：`39a5535c`（refactor(memory): share summary scope filtering）。

### Q023 Scheduler maintenance / history recovery（进行中）

- 用户重新启用总goal，从真实HEAD 39a5535c和下一未开始批次继续；仅用户未跟踪审查报告保留，不触碰或暂存。Q019B原提交已核验，无新增证据，不重新检查已完成源。
- 当前完整阅读memory_maintenance.py及test_memory_maintenance_runtime.py，范围是原scheduler的idle维护、两类恢复游标与失败投影重试；先执行原两项行为基线及整文件Any，再复现删除任务后append-only历史是否阻塞仍存活任务恢复。正确性依据为Core允许删除会话而commit历史保留，不得让已删除来源阻断其他会话的可选投影。未修改生产代码，尚未认定bug。

- 原2节点基线0/2passed（q023-baseline.json）；完整源/现有测试/runner强Any基线1，源35-36两条Mapping条件传播，测试与runner0。真实MemoryDurableStore与SQLiteDurableStore删除已结算old-run会话后，commit前缀仍保留，再结算kept-run，维护在查询old-run时KeyError，两后端相同2failed/exit1（q023-deleted-task-before-valid.json）。前置删除/保留历史断言已实际通过；不依赖mock或错误测试预期。初稿额外调用不存在的MemoryService.close导致teardown AttributeError，已修正夹具后重新取得仅目标行为失败的证据，初稿不作正式复现依据。
- 违反memory-boundary第51-54行的Core补读恢复和成功消费推进游标合同：已删除任务没有可恢复来源，应完成该历史条目消费而非永远阻塞其他会话。下一步仅识别task查询的缺失KeyError，不隐藏存储失败或投影返回False；补齐有效任务、已删除任务、重复维护和失败重试边界。

- 修复仅处理历史task查询的缺失KeyError，将其视为已无来源的完成消费；其他错误照常抛出，投影False仍中止当前扫描而不跨过失败位置。_recover_knowledge仅补具体tuple[object,...]/Mapping[object,object]视图，三个所消费字段仍逐一判断，不按object掩盖动态字段约束。真实memory/SQLite相同输入 **0，2 passed，1.74秒**（q023-deleted-task-after.json），存活task经历恢复、历史游标到末尾、已删除task仍不存在，重复维护没有重复item。
- 补充维护内投影失败不越过cursor、第二次可恢复；OSError不是缺失任务；知识恢复两种Ghost开关下False重试/只消费一次。直接完整脚本 **0，8 passed，2.93秒**（q023-direct-final.json）；知识fixture初稿依赖临时AppContext自动建knowledge而为None，属隔离配置问题，两失败不是生产bug，显式创建真实KnowledgeStore后8通过。随后仅把测试原函数引用从非公开重导出改为其原定义模块以满足mypy，受影响两节点在下面runtime批再次实际通过，无业务改动或预期放宽。
- 关联memory/deletion：`python -m pytest -q tests/test_memory_boundary.py tests/test_memory_completion.py tests/durable/test_conversation_delete_commit.py --tb=short` → **0，62 passed，21.85秒**（q023-callers-memory.json）；scheduler/knowledge/cleanup：`python -m pytest -q tests/durable/test_scheduler.py tests/test_knowledge_version_graph.py tests/test_forget_session_cleanup.py tests/test_memory_maintenance_runtime.py::test_knowledge_recovery_retries_completion_then_consumes_it_once --tb=short` → **0，31 passed，7.89秒**（q023-callers-runtime.json）。去重8+62+31-2=**99 passed、0失败/0skip**；基线和复现不额外相加，未运行全量pytest，其余模块未覆盖。
- 完整源/完整maintenance脚本/忽略runner强Any **0/3file**（q023-local-any-complete.json）；源/测试Pyrefly0、ty0、Ruff0（q023-local-{pyrefly,ty,ruff}.json）。生产门按要求串行执行：`python -m mypy codey --python-version 3.11` **0/760file**、`pyrefly check codey --python-version 3.11` **0/35suppressed/166warnings**、`ty check codey --python-version 3.11` **0**、`python -m tools.kernel_type_precision_gate` **0**（q023-production-*.json/q023-precision.json）。所有成功证据核对源码前后hash、最终工作区hash和起止HEAD一致；Windows/Python3.12实跑、类型目标3.11，不宣称其他平台实测。
- MemoryMaintenance是AppContext唯一注册的原scheduler idle callback，_recover_knowledge由本类调用；两观察器分别维护持久memory游标与实例knowledge游标、消费不同事实且失败语义不同，不强行合并，不删除有契约入口。无需新依赖/配置/公开API变更，既有两个原测试断言全部保留。旧checkpoint/operation_payload Any、Q021C浏览器/463Any及其他未完成事项继续保留原队列。
- Q023单源声明范围验收完成，准备独立本地提交。累计 **43/321完整批次、81/1006完整批次生产源**，另10部分源独立验证，累计91；全仓未完成、总goal active。下一主批Q024先按真实shell/审批职责核对是否需分区，不重开已完成源。用户未跟踪审查报告始终不暂存，无worktree/源码树复制或远端修改。

Q023 已提交：`dcdacde9`（fix(memory): advance recovery past deleted task history）。

### Q024 Canonical memory commands（进行中）

- 原Q024的memory_commands不属于shell审批，按真实职责拆为Q024命令持久化1源、Q024B审批事实/重建3源、Q024C审批worker1源、Q024D执行票据/进程1源；完整1006源仍唯一归属，总324批，未缩小范围。原Q016已验收coordinator记录迁到Q024B，不重新全读；state此前输出部分已读但未完成，durable/workflow/service只接口头定位，不能记已治理。
- memory_commands完整57行已读，检查锁内request_id幂等/内容不可改写、先验证再提交、canonical occurred_at复用及scope选择，类型入口为AppContext/JsonObject/EntryDraft，先执行实际调用方及源整文件Any。暂未发现符合准入的独立bug或可证明可删公开接口。

- Q024调用方`python -m pytest -q tests/test_memory_lifecycle.py tests/test_memory_summary_input.py tests/test_memory_feedback.py tests/test_memory_adaptation_flow.py --tb=short` → **0，47 passed，17.85秒**（q024-baseline.json）。完整源＋忽略runner严格Any **0/2file**；源Pyrefly/ty/Ruff全exit0（q024-local-*.json），命令/最终源hash/起止HEAD均记录。原始脚本只执行未全读，不宣称其Any治理。
- Q024完成且无需源码修改，不为生成commit而改动；5个公开函数都有实际路由/feedback调用，scope去重与JSON标量验证职责已由现有工具唯一承担，未证明可删或可合并的新实现。自Q023后生产源码未变，沿用未失效的三生产门/precision证据，不机械重跑。累计 **44/324完整批次、82/1006完整批次生产源**，另10部分源独立验证，累计92。记录随下一实质修改提交，总goal active。

### Q024B Durable shell approval facts / reconstruction（进行中）

- 范围为durable/state两未完成源，coordinator此前Q016已治理，保留其验收；只在实际共享接口改变时扩大复验。先完整读取事实提交和重建、执行审批基线及Any审计，再对独立合同允许的问题建立有效复现。

- 两未完成源durable127行/state414行已全读；40个durable审批/权限基线 **0/40passed/16.31秒**（q024b-baseline.json）。整文件强Any源5条，均在state:141 model_selection映射泛型、178-180 deferred_tool_calls的Any迭代；durable/runner0（q024b-any-before.json）。
- **事实可套用另一审批身份：** 独立合同为ApprovalRecord文档的command artifact/digest不可变、shell_approval_lifecycle第39-41行的approval绑定session/source run/已验证工件。commit_fact只检fact自己的task属于session，却把相同approval_id原记录的身份复用后推进状态；另一真实task/另一session或变更command_ref/digest/cwd均不被拒绝，请求重试还被误当幂等成功。新真实memory/SQLite脚本`python -m pytest -q tests/test_shell_approval_fact_identity.py --tb=short`修改前 **1，20failed/2passed，2.59秒**（q024b-identity-before.json），所有失败均DID NOT RAISE CommitConflict，合法同身份幂等及approved转换对照通过。原生产未修改；下一步在查到existing后先核对完整不可变身份，再执行状态/重试逻辑。

- 修复existing record的task/session/command_ref/command_digest/规范化cwd身份核对，位于幂等及状态转换之前，差异立即CommitConflict；不改Core状态机或反向改变正确测试。相同完整22节点 **0，22 passed，2.38秒**（q024b-identity-after.json），两个合法幂等/approved控制通过，所有拒绝路径Core commit/audit原样保留。
- state类型收敛先入object、Iterable[object]、dict[object,object]及Mapping[object,object]再按使用字段判断；保留字典行对象/顺序/只取前8、falsey缺省为空及非可迭代输入TypeError。API选择仍由ApiRunSelection.from_payload校验。expiry原无依据JsonObject cast删除，使用现有decode_json_object在公开JSON事件返回边界校验，不改有效DTO值；unused cast import随之删除，无新抽象。coordinator未编辑，因直接调用state/durable发生真实依赖变化，整文件类型与相关claim/expiry测试重新验证，未随意重新治理已完成源。
- 首次直接72通过后，空deferred缺省从[]换成()以消除唯一空容器Any，原测试源码hash已不等于当前，未把它作最终结果；最终`python -m pytest -q tests/test_shell_approval_fact_identity.py tests/durable/test_approval_records.py tests/test_command_permissions.py tests/test_approval_registry.py --tb=short`重新执行 → **0，72 passed，17.86秒**（q024b-direct-final.json）。claim/Stop/recovery `python -m pytest -q tests/test_shell_approval_coordinator.py tests/test_approval_authorization_recovery.py tests/test_shell_approval_effect_recovery.py tests/test_shell_approval_epoch.py --tb=short` → **0，21 passed，20.34秒**；workflow/HTTP restart/native identity `python -m pytest -q tests/test_shell_approval_lifecycle.py tests/test_shell_approval_http_restart.py tests/test_shell_approval_native_identity.py --tb=short` → **0，36 passed，39.44秒**。三组去重 **72+21+36=129 passed、0失败/0skip**；原基线与复现不额外累计。
- 整个durable/state/未改coordinator＋完整新回归＋忽略runner强Any **0/5file**（q024b-local-any-final.json）；源/新测试Pyrefly0、ty0、Ruff0（q024b-local-*.json）。Pyrefly显式warn检查exit1仅报告durable:50已有冗余str转换，不伪称warn命令通过或无warning，不为此做无关格式变更。生产mypy **0/760file**、Pyrefly **0/35suppressed/166warnings**、ty **0**、precision **0**串行通过（q024b-production-*.json/q024b-precision.json）；最终源hash/检查起止HEAD稳定，pytest支持脚本其余仅执行，未宣称全文件类型完成。
- 必要依赖整文件Any基线exit1共19条：shell_approval_records.py17（18/44/49显式Any，19/46/50-53/70-71/84/100编码反射与JSON传播）属Q200；api_selection.py2（66-67的to_payload显式/返回传播）属Q189。q024b-dependency-any.json保存命令/原始位置；两依赖均未修改，消费边界已使用具体ShellApprovalFact/ApiRunSelection和object校验，依赖未宣称已治理或计入完成生产源。
- 事实记录与execution atomic claim的状态副作用不同，未强行抽象；所有内部入口由coordinator、workflow和restart使用，未删除公开或可能外部入口。Q024B三源声明范围完成，准备与无改动Q024记录一并提交。累计 **45/324完整批次、85/1006完整批次生产源**；coordinator从部分源迁入完整批，部分源独立验证由10降为9，累计94，不重复计数。Q024C/Q024D及其他范围未完成，总goal active；下一主批审批worker/结果续接。用户审查报告未暂存，无源码树/worktree复制、远端或发布操作。

Q024B 已提交：`201d521e`（fix(approvals): reject facts with conflicting immutable identity）。

### Q024C Approval worker / result recovery（已完成，提交后暂停）

- workflow完整617行已读，范围HTTP决定、唯一worker接纳、结果持久化/投影与恢复；ShellService仅必要执行接口定位，实际进程/票据留Q024D。先运行三脚本基线与整文件Any，检查Thread.start失败是否留下虚假worker所有权。显式回调参数当前未调用但为公开兼容入口保留，不能因无仓内调用直接删；会用ParamSpec约束其任意调用签名而不引入Any。

- 原三脚本基线 **0，29passed，31.20秒**（q024c-baseline.json）；workflow整文件强Any **1，69 diagnostics**（q024c-any-before.json），包括Submit省略签名、6处显式Any、动态getattr/task调用与pending传播。
- **Thread.start失败留下虚假worker：** _schedule先注册_workers再start；标准RuntimeError启动拒绝后无回滚，同approval后续恢复被当作已有worker，quiescent/close永久错误，已提交approved决定却无人执行。独立依据为线程start失败不得拥有执行名额、批准工作必须可由原durable恢复入口重新接纳且不得重复执行的single-flight不变量；Python线程公开启动失败契约及shell lifecycle恢复所有权有效。新真实AppContext/真实审批工件、仅executor替身与一次Thread.start失败注入，从accept与start恢复两个入口实际 **1，2failed/1passed，24.38秒**（q024c-worker-before.json），失败均quiescent=False，正常活跃worker防重复对照通过，非导入/夹具失败。

- 相同worker回归修复后 **0，3passed，4.37秒**（q024c-worker-after.json），已提交approved决定保留、失败名额释放、wait立即空闲、start可恢复且最终只执行一次；同一活跃worker的并发恢复仍不重复。
- workflow69条Any已清零：复用PendingApproval、公开响应具体object字段、ParamSpec保留任意兼容回调参数及str/None返回约束、直接使用AppContext声明的durable/control接口；原cast改为字符串键字典运行时守卫或具体TaskCheckpoint返回。scope/结果/receipt缺失与继续阻断语义保留，原三脚本及新worker脚本直接32passed，权限/epoch/worker34passed，receipt/native34passed（q024c-direct/callers-*.json）。后续完整查看必要共享fixture，发现5条registry动态lookup Any，需在同批治理并使相关setup验证重新有效，前述结果不提前当最终helper结果。
- 完整tests/support/shell_approval.py已读，基线强Any **1，5条（24-27/34）**（q024c-support-any.json）。上下文合同为AppContext，改用其已声明run_registry.api_selection_for并为UI事件作object键Mapping视图，保持字段提取/缺省与请求/工件生成。初稿视图局部名fields与下方命令fields冲突，被三checker拒绝，已改名event_fields；这是自己的未提交类型初稿问题，不称原生产bug。一次checker调用返回session后误启动其他checker，最终重验改用串行等待全部结束再启动下一项，披露该过程偏差，不将先前失败/重叠说成已通过。

- helper后最终direct原发生 **1，31passed/1failed**（q024c-final-direct.json），关闭用例等待全局stop_flag超时；其余5个调用方文件 **0，65passed，42.10秒**（q024c-final-callers.json）。未凭重跑覆盖失败。单节点无活跃run采样1passed/global_set=true；受控地在Stop前用公开RunRegistry.reserve接纳一个有效run，在201d521e旧workflow＋旧helper的内存Git blob对照与当前实现均 **1，1failed**，同一580行，global_set=false但该run.stop_event=true（q024c-shutdown-original/current-controlled.json）。这是事件身份/夹具同步问题，不把受控接纳冒充自然失败当时的精确时序；自然失败没有保存run快照。首次trace的相对-m命令无效，未执行测试，不作故障证据。无磁盘源码副本或worktree。
- 独立依据：RunRegistry.stop_flag是没有run时的admission fallback，每个已接纳run有独立stop_event；UI全局busy也覆盖实际run和后台approval。原测试开启scheduler却依赖“没有其他run”的全局事件/空闲前提，不满足自身声明的receipt与resume间隙。用户明确要求本段修完再提交暂停，因此继续处理此测试隔离与整文件类型义务，不进入Q024D。
- 完整lifecycle脚本强Any基线 **1，204类型诊断**，另最初trace脚本3条，合计207（q024c-lifecycle-any-baseline.json）。测试参数/返回、执行器、response/observation容器、native callback完整签名、Optional状态/事实和动态JSON字段均补类型与运行时守卫；同一成功ShellResult字典由一个有类型fixture工厂生成，原Mock调用断言与字段值保留。旧24个测试函数顺序与集合相同，静态assert计数99→106，仅说明未删测试、不是执行证据。
- 关闭用例首进程用受支持start_scheduler=False，显式断言无active run且原任务PENDING，把已结算receipt之后的durable wakeup留到真实重启进程执行；原全局stop_flag、close完成、已结算receipt、恢复started、只执行一次等断言全保留。该节点实际 **0，1passed，2.72秒**（q024c-shutdown-isolated.json）。期间完整类型脚本复验出现busy用例 **1，27passed/1failed，30.28秒**（q024c-closure-direct.json）：worker.wait已确认worker为空，而全局busy仍被允许自动接纳的Core run占用；同样将仅验证后台approval busy的用例设为start_scheduler=False，并在前后断言无其他active run，busy true/false断言不改。两隔离节点＋worker回归 **0，5passed，6.91秒**（q024c-isolation-after.json）。没有修改正确生产Stop/busy逻辑、加超时、skip或放宽断言。
- 最终完整直接组`python -m pytest -q tests/test_shell_approval_worker_admission.py tests/test_shell_approval_lifecycle.py tests/test_shell_approval_http_restart.py tests/test_shell_approval_text_result_reconstruction.py --tb=short` → **0，32passed，33.97秒**（q024c-accepted-direct-final.json）。既有未再受影响的调用方组`python -m pytest -q tests/test_command_permissions.py tests/test_shell_approval_epoch.py tests/durable/test_approval_records.py tests/test_shell_approval_effect_recovery.py tests/test_shell_approval_native_identity.py --tb=short`为 **0，65passed，42.10秒**；helper当时已是最终版本，后续只改lifecycle脚本，该组结果未失效。当前9文件不重复 **97passed、0失败/0skip**，不是累计所有前后重跑。原两个失败保留记录，已通过有独立前提依据的隔离修正处置，不以偶然单跑通过结项。
- 最终串行强Any覆盖完整workflow/worker回归/lifecycle/helper及两个忽略runner **0/6file**；四个仓库源/测试Pyrefly0（仅1条原有冗余str warning）、ty0、六文件Ruff0（q024c-pause-local-*.json）。workflow69、helper5、lifecycle204诊断均已清零；中间变量重名、callback可空选项签名和import排序均为本轮初稿问题，已工具检出并修正，不冒充生产bug。最后一次验证哈希与当前四文件一致，无生产修改发生在原生产门之后，因此保留实际mypy760/Pyrefly35suppressed166warnings/ty/precision exit0证据，不机械重跑未失效的生产门。
- 先提交生产worker修复及其独立回归，随后将已验证的共享fixture/lifecycle治理独立提交并结项暂停；第一次提交记录不声称后一个测试提交已发生。没有可证明可删的公开兼容Submit入口；start的started分支虽然最终continue，但含durable读取/异常效果，未按表面冗余强删。用户审查报告始终不暂存；总目标尚未完成。

Q024C 生产修复已提交：`82cfb5e6`（fix(approvals): release worker admission after rejected starts）。

- 本次第二提交收录已完整验证的helper与lifecycle脚本治理，源与独立worker回归保持第一提交内容不变；没有暂停在失败状态。两个测试原先混入自动Core调度造成的全局停止/空闲假设均通过显式夹具隔离处置，原关闭和busy断言保留，没有为了迁就测试改正确生产行为。lifecycle完整24函数均已读并类型治理，无遗留204诊断；状态/事实Optional、JSON动态字段和mock结果工厂均有具体类型或运行时守卫。原完整4字段成功字典只收敛为一处有类型工厂，不用实际生产输出重写预期。
- 暂停前最后直接32passed与未失效调用方65passed去重 **97passed、0失败/0skip**，所有应执行的局部验证已完成。最后串行六文件强Any、Pyrefly、ty、Ruff均exit0，源hash和本次入库测试/helper hash与证据一致；原生产三类型门/precision实际通过且自后仅测试脚本修改，因此保持有效。不运行全量pytest、不扩大到未检查shell进程实现、不重复已完成无变化生产批次。
- Q024C声明范围结项；累计 **46/324完整批次、86/1006完整批次生产文件**，另9部分批次源独立验证，累计95，不把这9个算完整批次。两个正确性bug（此前Q024B事实身份与本批worker接纳）各有有效原失败→相同输入通过；本次两隔离问题是测试fixture问题，另列处理，不伪称为生产竞态修复。其余全仓范围与原Q021C浏览器/Any、Q189/Q200依赖Any等历史事项尚未完成，未标总goal完成。
- 用户已要求“修完，提交后，暂停一下”；第二提交后goal设为paused并停止，不进入Q024D。第二提交hash避免自引用，在最终聊天报告和Git历史提供，下一次进度更新再补入commits数组。用户未跟踪审查报告未编辑/暂存/提交；不push、不改远端、不复制worktree或源码树。


### Q024D 审批执行票据、输出与续接

- 恢复基准为 master `3e330232`，工作区及暂存区干净。Q024C 两笔提交为 `82cfb5e6` 与 `be6cb67d`。对比当前 Git 清单，纳入后置记忆抽取 Q023B、项目检查交接 Q090 与 Messages codec Q122，共 1009 个生产文件、40 个区域、325 批。新增 changes drawer 测试归验证支持。Q015C、Q015F、Q018B、Q023、Q181 仅因已有验证文件实际改变而登记重验，保留历史证据。新增 Q023B 与这些重验排在当前断点后的回补队列，Q024D 后先继续 Q025。
- 完整检查 `codey/app/shell_service.py` 的票据入场、进程生命周期、结构化结果和续接。直接三脚本基线 `python -m pytest -q tests/test_shell_contract.py tests/test_shell_approval_epoch.py tests/test_shell_continuation_reports_denial_and_reconciles_ui.py --tb=short` 得 32 passed，退出 0。运行目录为 `D:\codey`，Windows Python 3.12.8，测试使用现有 RAM 盘。
- 输出观测的独立契约见 `docs/conversation_ui_next_implementation_spec_2026-10-16.md` D2/D3。快照是可选展示通道，进程执行及最终结果独立。真实已授权票据、无害 echo 和被注入的存储初始化 OSError 执行后原先返回 `spawn_error`。真实 SQLite 和过期 generation 输入原先返回 stopped，实际记录却长期为 running。仅执行 `python -m pytest -q tests/test_shell_execution_output_lifecycle.py --tb=short`，退出 1、2 failed，失败分别命中上述状态断言。
- 隔离观察初始化失败，保持真实进程执行。过期和 Stop 共用既有取消清理路径，将已建立的快照置为 unknown。相同回归实际退出 0、2 passed。最终加入健康存储对照，验证同一执行身份、输出内容和 settled 状态。保持初始化在 spawn gate 外，避免慢输出存储占用 Stop 的启动锁。
- 补齐结果序列化和可选上下文字段类型，以现有 JSON 解码和对象键检查替换无校验断言。合并重复的续接结果文本构造，保留尾行投影及原始执行收据。移除只在必经 wait 成功后检查不可达 None 的分支。保留导出的旧执行和结果入口。定向严格 mypy 由源文件 49 条诊断降至 0，源、新回归和共享执行夹具共 3 文件。初稿 Pyrefly 发现分支字典泛型推断问题，明确 JsonObject 后通过。定向 Pyrefly 0 errors、2 warnings 未展示，ty 和 Ruff 均退出 0。
- 最终直接范围 `python -m pytest -q tests/test_shell_execution_output_lifecycle.py tests/test_shell_contract.py tests/test_shell_approval_epoch.py tests/test_shell_continuation_reports_denial_and_reconciles_ui.py tests/test_server.py::ApprovedShellTests tests/test_server_lazy_state.py --tb=short` 得 53 passed、6 subtests passed，退出 0。调用方 `python -m pytest -q tests/test_execution_output.py tests/test_shell_approval_native_identity.py tests/test_shell_analysis_authorization.py tests/test_shell_approval_lifecycle.py tests/test_command_permissions.py --tb=short` 得 79 passed、2 skipped，退出 0。本次两个最终范围共有 132 项通过、2 项跳过，另有 6 个通过的子例。跳过理由未在此次命令输出。
- 生产 mypy、Pyrefly、ty 均以 `--python-version 3.11` 在 `codey` 执行，退出 0。mypy 763 文件，Pyrefly 35 suppressed、166 warnings 未展示，ty 无诊断。 `python -m tools.kernel_type_precision_gate` 退出 0。原始日志位于忽略的 `.e2e-artifacts/code-quality-20261122/q024d-*.log`。
- 本次先提交可独立验收的输出生命周期和文件类型修复。Q024D 继续核对旧公开续接文本的启动失败描述，并完成已深入读取的续接测试类型检查。上述两组测试的其他脚本用于执行验证或入口定位。尚未执行其余全仓队列。

- 输出生命周期修复已提交 `5a116e71`。续接的独立依据是 `_execute_shell_ticket` 将尚未创建进程的错误标为 spawn_error，且 shell 生命周期要求透传真实执行结果。公开构造器输入 `status=spawn_error, exit_code=None, error=interpreter missing` 原先声称 “approved and ran”。原样运行续接测试文件，实际 1 failed、2 passed，退出 1。新增明确的启动失败文案后，相同回归连同拒绝、停止、ApprovedShellTests 和两条续接计划调用方共 14 passed，退出 0；命令为 `python -m pytest -q tests/test_shell_continuation_reports_denial_and_reconciles_ui.py tests/test_server.py::ApprovedShellTests tests/test_server.py::RunSnapshotTests::test_shell_approval_continuation_plan_uses_active_run_and_pending_fallbacks tests/test_server.py::RunSnapshotTests::test_shell_approval_continuation_plan_falls_back_to_pending_provider --tb=short`。
- 续接脚本补齐两个原测试签名与 JSON 对象校验，保留对脚本化答复原摘要的断言。源和续接测试严格 mypy/Pyrefly/ty/Ruff 退出 0，Pyrefly 有 2 条未展示警告。前一提交的全生产门与独立执行、输出及 native 验证已执行通过，最终文案变化的直接调用方已重验。全批定向去重 135 项通过、2 项跳过，另有 6 个通过的子例。
- Q024D 结项。累计 44/325 批完成，完成批覆盖 84/1009 个生产文件，另有 7 个部分批次文件独立验证。部分批次 13，阻塞批次 0。 旧入口仍为公开导出，保留。下一主批 Q025 负责问题查询、答复和取消 HTTP 路由。


### Q025 问题查询、回答和取消的 HTTP 路由

- Q024D 续接修复已提交 `1653d49f`。本批完整检查 `codey/app/question_routes.py`。独立契约见问题卡调用方 `frontend/src/user_questions.ts` 的答复与取消请求，以及任务控制中只有明确答复或取消才能改变问题和原任务的状态。正常发送的答复请求省略 action。API 维持历史显式 answer 兼容。
- 在真实认证 HTTP 路由和 SQLite 中，用同一有效答案分别提交未知字符串、数组、显式 null 三种动作。原先都返回 202，GET 随后读到 answered、revision 2。 `python -m pytest -q tests/test_question_route_actions.py --tb=short` 实际退出 1，3 failed、3 passed。后者为省略 action、显式 answer 和 cancel 的成功对照。
- 在调用 durable 决策前拒绝未知动作。相同三种输入均返回 400，问题保持 pending、revision 1。补齐 HTTP 边界的 AppContext、字典和行校验。保留原本的无效回答 409 及当前问题返回，并增加无效答案和 GET 列表的回归对照。底层问题存储的历史动态类型仍归 Q064，本批仅通过有类型的 HTTP 边界消费。
- 在 `D:\codey` 执行 `python -m pytest -q tests/test_question_route_actions.py tests/test_conversation_questions.py tests/durable/test_question_authority.py --tb=short`，15 passed。 `python -m pytest -q tests/test_conversation_question_http.py --tb=short` 修改前后均 10 passed，后者覆盖双协议、两任务模式及重启恢复。两组去重 25 passed，0 skip，均退出 0。在 `D:\codey\frontend` 执行 `npm run test -- src/user_questions.test.ts`，3 passed，退出 0。调用方测试只记执行范围。
- 源和新回归定向 mypy（强 Any 三标志）从初稿 39 条诊断到 0，Pyrefly、ty 和 Ruff 均退出 0。全生产 mypy、Pyrefly、ty 分别用 `--python-version 3.11` 检查 `codey`，以及 `python -m tools.kernel_type_precision_gate`，均退出 0。mypy 763 文件，Pyrefly 35 suppressed、165 warnings 未展示。命令结果位于忽略的 `.e2e-artifacts/code-quality-20261122/q025-*.log`。
- Q025 结项。累计 45/325 批完成，完成批覆盖 85/1009 个生产文件。Q026 接续审查服务的输入、模型调用与结果交付。


### Q026 审查执行和快照保护

- Q025 已提交 `be12856b`。完整检查 `codey/app/review_service.py`，范围包括模型选择、原回复和格式修复、持久快照及事件。独立依据为审查快照只对实际文件内容有效，格式修复复用同一次审查输入。已有服务契约要求格式重试与最终审查结论均由快照保护。
- 新回归在首个回复中把实际受审文件从 `updated = False` 改为 `updated = True`，返回无效 JSON。旧实现发送两次，最后才标 stale。相同测试在工作目录执行原模块的 Git blob，实际 1 failed、1 passed，失败为 `('stale', 2)` 与预期 `('stale', 1)` 不同。未变化的对照正常发送两次且获批准。让普通解析失败和输出截断共用发送前的取消、快照检查，保留原诊断。相同回归修改后 2 passed。严格类型初稿发现测试假提供器的 send 签名与 ApiProvider 不兼容，替换为满足 ChatProvider 端口的窄 fixture；前述原模块对照也使用最终 fixture。
- 用 ChatProvider、ReviewInput、ReviewSnapshot、TaskCheckpoint 等既有类型替换动态字段。两条 API 路径共用检查点读取和允许模型提取，原选择、租约、检查点写入和打开顺序分别保留。复用、结果持久化和调用方依赖通过现有对象守卫。保留公开入口与带不同语义的发送路径。
- 在 `D:\codey` 运行 `python -m pytest -q tests/test_api_reviewer_uses_distinct_model_and_identity.py tests/test_review_actual_model_and_policy_identity.py tests/test_review_consumer_and_provider_failure_boundaries.py tests/test_review_http_rejections_preserve_known_failure_without_replay.py tests/test_review_contract_owner_and_empty_rejection.py tests/test_review_contract_integration.py --tb=short`，基线 47 passed。修改后同命令加入 `tests/test_review_format_repair_snapshot.py` 得 49 passed。两个范围退出 0。
- 调用方 `python -m pytest -q tests/test_headless_review_uses_selected_provider.py tests/test_project_auto_local_review_lifecycle.py tests/test_standalone_api_review_keeps_explicit_model.py tests/test_server_lazy_state.py --tb=short` 得 18 passed、4 failed、6 subtests passed，退出 1。用 `git show be12856b:codey/app/review_service.py` 在内存替换模块并复跑原四个节点，同样 4 failed，断言和计数相同。额外观测一个桌面路由节点，Writer 4 次发送、计数 0，Reviewer 0 次、无审查事件。生产 Writer 可走 `activate_durable_window`，现有 fixture 只计 `new_chat/reset_window`。桌面 Reviewer 未接纳的具体原因待在 Q027/Q090 跟进，Writer 窗口入口归 Q088。保留四个现有断言，并把其未完成验证和测试脚本类型审计纳入后续。
- 源文件强 Any 基线 151 条，最终源与新回归的 mypy 强 Any、Pyrefly、ty 和 Ruff 均退出 0。首次 Ruff 识别新增局部导入排序，修正后通过。生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty、`python -m tools.kernel_type_precision_gate` 均退出 0。原始记录在忽略的 `.e2e-artifacts/code-quality-20261122/q026-*.log`。本批 67 个通过的定向测试、4 个未解决的原调用方失败，另 6 个通过子例。
- 本批部分完成，提交已独立验证的修复。累计 45/325 批完成，完成批覆盖 85/1009 个生产文件。另有 14 个部分批次、其中 8 个文件已有独立验证。Q027A 已完成并沿用验收，接续 Q027 的任务生命周期与运行适配。


### Q027 任务协调、终态事件和服务装配

- Q026 已提交 `c78d63ca`。本批查阅了 runtime_adapters、submission_types、task_abort、task_lifecycle、task_lifecycle_projection、task_runtime 和 task_services 的全部文件，按公开入场、停止、终态发布和依赖装配分配验证。此前通过验收的 Q027A 保持历史覆盖。
- 终态快照的版本应对应实际完成的 run。独立依据是 RunRegistry.finish 用入场 run ID 作为结束事件身份，前端及持久化回放要求同一身份关联一次提交版本。真实 SQLite 中完成 task 至 revision 3，保留另一待办 other-task 的 revision 0。向 `finish_run("task", event)` 提供缺失 run_id、另一任务 ID、另一任务已写入版本三种事件。相同的 `python -m pytest -q tests/test_task_lifecycle_projection.py::test_terminal_version_uses_the_finished_run_id --tb=short` 在修改前实际 3 failed、退出 1，结束事件分别得到缺失版本或 `0/pending`。
- 在版本读取前绑定 run ID，移除其他身份带来的版本和状态字段，保留同身份预先写入的历史版本。相同三个输入均通过，已有确定版本的回放与当前状态变化也继续通过。其余变更将动态 abort 能力经 callable 检查后调用，故障采集入口按既有 CaptureProviderFailure 端口传递。
- 工作目录 `D:\codey`。基线 `python -m pytest -q tests/test_task_runtime_coordinator.py tests/test_task_lifecycle_projection.py tests/test_task_admission_races.py tests/test_task_submission_stores.py --tb=short` 为 23 passed，修改后同命令 26 passed。调用方 `python -m pytest -q tests/test_task_input_contract.py tests/test_task_guidance_reaches_production_entries.py tests/test_task_entry_verification.py tests/test_shell_approval_lifecycle.py tests/durable/test_suspension.py tests/test_server.py::RunSnapshotTests::test_finish_non_approval_expires_same_run_shell_approval --tb=short` 为 62 passed。最终两组 88 项通过，0 失败、0 跳过，退出 0。未重新执行 Q026 的待回补项目生命周期用例。
- 七源强 Any 基线 4 条诊断，修复后七源和深入检查的投影测试共 8 文件，强 Any、Pyrefly、ty、Ruff 均退出 0。配套调用方测试按执行范围记录。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 和 kernel_type_precision_gate 实际退出 0。详细运行记录位于忽略的 `.e2e-artifacts/code-quality-20261122/q027-*.log`。
- Q027 完成。累计 46/325 批，完成批覆盖 92/1009 个生产文件。部分批次 14 个，其中 8 个文件独立通过。下一批 Q028 为 automation 包入口。


### Q028 automation 包入口

- Q027 已提交 `88929a3a`。完整检查 `codey/automation/__init__.py`，其唯一内容是模块文档。包标记用于 setuptools 普通包发现，browser 和 browser_worker 由调用方按需导入。无修复或删除依据，保留源码。
- 在 `D:\codey` 用 `python -c` 启动新进程，断言 `setuptools.find_packages(include=["codey*"])` 包含 `codey.automation`，导入后 `sys.modules` 未加载 automation 子模块或 Playwright。退出 0。对唯一源执行严格 mypy（强 Any 三标志）、Pyrefly、ty 和 Ruff，全部退出 0；日志 `.e2e-artifacts/code-quality-20261122/q028-*.log`。已有 lazy 行为验收沿用，未新增或复跑 pytest。
- Q028 无需修改，记录随下一次代码提交入库。累计 47/325 批，完成批覆盖 93/1009 个生产文件。接续 Q029 的浏览器绑定、worker 进程和 IPC。


### Q029 浏览器 worker 的命令与二进制传输契约

- 原 Q029 按独立职责拆为传输契约 Q029、CDP 启动和绑定 Q029B、进程协调和 BrowserSession Q029C。详情读取移至原 DOM 观察 Q030。总数更新为 327 批，1009 个生产文件均保留原覆盖范围。
- 完整检查 browser_ipc 与 browser_contracts。Python 二进制流 `write()` 返回本次实际写入长度，调用方线程锁只保证包不交错。用无外部副作用的内存短写流分别收 1、6、0 字节，原写法丢失帧内容，读端报 EOFError 或非法头，0 字节时写端还宣告成功。完整写入对照通过。改为推进剩余内存视图直至 frame 完成，无法推进时抛出 I/O 错误。
- 命令期限的契约为可表示成有限正浮点数，解码器用 ValueError 报无效期限。输入一个超出浮点范围的整数时，原 `math.isfinite` 泄漏 OverflowError。显式转换期限并将该边界映射为现有 ValueError。类型合法的数值仍走原有限性和正数校验。
- 在 `D:\codey` 先定向验证饱和队列和输出 schema 两个节点，2 passed。回归最初合放 `tests/test_browser_ipc_contract.py`，`python -m pytest -q tests/test_browser_ipc_contract.py --tb=short` 实际 4 failed、1 passed。修复后按提交职责拆为 `tests/test_browser_ipc_contract.py` 和 `tests/test_browser_command_contract.py`，相同断言及两个基线节点合计 7 passed，退出 0。另运行 `python -m pytest -q tests/test_browser_process.py::test_worker_real_browser_wait_cancel_and_binary_capture --tb=short`，真实 worker 定向集成 1 passed、退出 0。CDP 重连和 worker 其他生命周期测试分别归 Q029B/Q029C。
- IPC 固定结构头的七条强 Any 诊断降至 0。两个源、两个回归的定向强 Any、Pyrefly、ty、Ruff 均退出 0。生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 及 kernel_type_precision_gate 均退出 0。日志在忽略的 `.e2e-artifacts/code-quality-20261122/q029-*.log`。两处修复可独立交付，将按帧写入和期限校验分开提交。
- 帧短写修复已提交 `20c4b519`。期限校验作为独立修复提交。累计 48/327 批完成，完成批覆盖 95/1009 个生产文件。下一批 Q029B 为 CDP 启动、端点和重连绑定。


### Q029B CDP 启动与资源绑定

- Q029 的期限校验已提交 `4d6414d1`。完整检查 `browser.py` 和 `browser_binding.py`，包括发现、启动、连接、本地标签与进程所有权，以及记录身份的恢复。独立依据见 `docs/browser-process-reconnect.zh-CN.md` 的资源清理和借用页合同。新建页在 Session 成功返回前仍由连接操作所有，异常时由该操作清理。
- 用专用真实本地 CDP，预置一个用户页。令新页导航无效 URL，或在实际出现第二个 target 后触发取消。对同一最终 fixture 使用 `git show 4d6414d1:codey/automation/browser.py` 的内存版本运行 `tests/test_browser_attach_cleanup.py`，实际 2 failed、退出 1，均额外残留一个 target，原用户页面内容仍在。修复先关新建页再断开 Playwright，并由同一错误清理所有权处理取消。相同两案通过。初稿诊断区分 Playwright 不支持同线程嵌套 sync 与目标泄漏。后续改为顺序连接，并按真实目标出现触发取消，消除 Playwright 类反射带来的类型诊断。
- 两源的显式和隐式 Any 清零。将标准库响应标为二进制流，在 JSON 和 CDP 响应处做对象收窄。删去两个无副作用的自赋值。保持现有读取和恢复路径的错误策略。
- 在 `D:\codey` 运行 `python -m pytest -q tests/test_browser.py tests/test_browser_process_reconnect.py tests/test_research_browser_cdp_lifecycle.py --tb=short`，前后各 70 passed，退出 0。最终 `python -m pytest -q tests/test_browser_attach_cleanup.py --tb=short` 2 passed，退出 0，定向去重 72 项。无跳过。工作进程其他生命周期用例留 Q029C。
- 源初始强 Any 11 条诊断。最终两源和新测试的严格 mypy、Pyrefly、ty、Ruff 均退出 0。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 与 kernel_type_precision_gate 已通过，之后只更新新测试。日志 `.e2e-artifacts/code-quality-20261122/q029b-*.log`。
- Q029B 结项。累计 49/327 批，完成批覆盖 97/1009 个生产文件。接续 Q029C 的 worker 和 BrowserSession。


### Q029C worker 进程与会话

- Q029B 已提交 `fc021bbd`。完整检查 `browser_process.py`、`browser_session.py`、`browser_worker_main.py` 的恢复、初始化、控制帧、结果和终止所有权。
- 子进程创建成功但 init 写失败时，调用尚未派发，客户端须能安全重试且必须收回半启动子进程。用真实 Popen 和一次测试注入的 init 写 OSError 执行 `python -m pytest -q tests/test_browser_worker_startup_rollback.py --tb=short`。修改前返回 not_delivered，但子进程 `poll()` 仍为 None，1 failed、退出 1。修改后相同输入 1 passed，并核实后续有效调用由另一进程完成。提前回收启动异常中的子进程和路由线程池，已完成握手的正常关闭仍由现有路径负责。删去关闭路径的重复赋值。
- 子进程入口验证标准流有二进制缓冲层或原始二进制流，清除三处 stdio Any。首次 TextIOWrapper 直接注解违反存根签名并超过 Ruff 语句上限，已改成独立的实际类型守卫。IPC 端口允许 Python raw I/O 的 None 返回，读端已有 EOF 处理，写端沿用无进展错误。Q029 仅重验这一受影响的帧边界。
- 在 `D:\codey` 执行 `python -m pytest -q tests/test_browser_process.py tests/test_task_browser_resources.py --tb=short` 得基线 8 passed。最终在同命令加入 `tests/test_browser_ipc_contract.py tests/test_browser_worker_startup_rollback.py` 得 13 passed，`python -m pytest -q tests/test_browser_process_reconnect.py --tb=short` 得 15 passed，两组去重 28 项（含 Q029 的 4 项变更后重验）。以 `PYTHONUNBUFFERED=1` 单独重验启动回滚与真实 worker 取消、截图共 2 项，属于重复节点和不同启动模式。各组无跳过，均退出 0。
- 最终强 Any、Pyrefly、ty、Ruff 覆盖三源、新测试和受影响的 IPC 源及原测试共 6 个文件，退出 0。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty、kernel_type_precision_gate 均通过。日志 `.e2e-artifacts/code-quality-20261122/q029c-*.log`。
- Q029C 完成。累计 50/327 批，完成批覆盖 100/1009 个生产文件。Q030 接续 DOM、框架、查询和页面观察。


### Q030 文档、框架与节点引用身份

- Q029C 已提交 `b452600e`。原 Q030 按文档和元素引用、快照、查询和观察、详情读取拆为 Q030 至 Q030D。所有 9 个文件保留明确归属，队列更新为 330 批。当前完整检查 browser_document、browser_refs、browser_frames。
- Chromium 运行时对象组在观察操作结束后必须释放。用真实 Chromium，分别观察一个及一批节点，在删除临时标记时注入 asyncio.CancelledError。原程序成功调用 DOM.describeNode，随后传播取消，但无 Runtime.releaseObjectGroup 请求。 `python -m pytest -q tests/test_browser_frame_group_cleanup.py --tb=short` 实际 2 failed，退出 1。最早以 Playwright Error 注入的对照原本通过，取消错误的回归才是证据。
- 将同一节点清理过程收敛到一处，在 finally 中释放对象组，原取消继续传播。相同两项随后通过。共用已解码的文档身份比较，在需要比较变化时避免再次解码，保留缺失身份和历史可选字段规则。为 Playwright 的 CDP 和 evaluate 入口采用明确可等待返回对象，返回动态 JSON 前做运行时解码。
- 在 `D:\codey` 执行 `python -m pytest -q tests/test_browser_async_frames.py tests/test_browser_element_registry.py tests/test_browser_navigation_handoff.py --tb=short`，基线 46 passed。最终加入 `tests/test_browser_frame_group_cleanup.py`，48 passed，无失败或跳过，退出 0。测试覆盖实际框架、节点引用、导航恢复。快照及详情后续有各自批次。
- 对三源及回归执行定向严格 mypy（禁 Any 三标志）、Pyrefly、ty、Ruff，均退出 0。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 和 kernel_type_precision_gate 均通过。日志 `.e2e-artifacts/code-quality-20261122/q030-*.log`。
- Q030 完成。累计 51/330 批，完成批覆盖 103/1009 个生产文件。接续 Q030B 的 DOM 快照、控件语义和版本差异。


### Q030B DOM 快照与控件状态

- Q030 已提交 `5160ff98`。完整检查 `browser_snapshot.py` 的 DOM 和 AX 合并、控件作用域、句柄与快照差异。依据是 ARIA `aria-disabled` 对可聚焦后代的禁用语义，以及该快照对控件可用性的合同。输入为真实 Chromium 页面的禁用 group、内部按钮 Queue 及外部普通按钮 Send。
- `python -m pytest -q tests/test_browser_snapshot_availability.py --tb=short` 修改前 1 failed、退出 1。Queue 的 disabled 实际为 False。让 DOM 及 AX 补充节点保留 Chromium 已观察到的禁用值。相同回归随后 1 passed，Queue 为禁用，Send 为可用。未改变操作授权。
- 在 `D:\codey` 执行 `python -m pytest -q tests/test_browser_observation_regressions.py tests/test_browser_async_runtime.py tests/test_browser_native_actions.py tests/test_browser_readable_references.py --tb=short`，基线 57 passed。最终加 `tests/test_browser_snapshot_availability.py`，58 passed。补查 `tests/test_browser_navigation_continuation.py::test_observed_disabled_state_matches_native_fieldset_and_legend_semantics`，4 个参数均通过。最终 62 passed，无跳过，退出 0。
- 为快照 JS 参数声明现有 JsonValue 容器，清除唯一强 Any 诊断。两文件定向 mypy、Pyrefly、ty、Ruff 均退出 0。Pyrefly 定向有 1 条未展示警告。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 和 precision 均通过。日志 `.e2e-artifacts/code-quality-20261122/q030b-*.log`。
- Q030B 完成。累计 52/330 批，完成批覆盖 104/1009 个生产文件。下一批 Q030C 为控件观察、记录区域和查询绑定。


### Q030C 控件观察、记录和查询绑定

- Q030B 已提交 `44b91b66`。完整检查 browser_observation、browser_query、browser_query_async，核对同步和异步调用、历史方案、导出和工具脚本。render_controls 由 A/B 工具调用。同步观察和绑定虽无新的仓内业务调用，历史方案仍引用，保留这些入口。
- 同一个已绑定的真实区域才可确认查询更新。用缺失、空和整数三个区域身份输入 `changed_query_region`，其余字段均满足可见、已结束、内容变化。 `python -m pytest -q tests/test_browser_query_region_identity.py --tb=short` 修改前 3 failed、1 passed（合法同身份对照）。三个无效键都返回了新正文。要求非空字符串且前后相同后，原测试 4 passed。用明确的 Awaitable 接收 Playwright 的区域关系值并校验字符串，清除唯一强 Any 诊断。
- 在 `D:\codey` 运行 `python -m pytest -q tests/test_browser_query_binding.py tests/test_browser_shadow_query_regressions.py tests/test_research_dynamic_queries.py --tb=short`，基线 25 passed、3 failed、退出 1。修改后加 `tests/test_browser_query_region_identity.py`，29 passed、同 3 failed、退出 1。真实节点、查询和 shadow 组通过。三失败都是研究 provider 替身的断言，修改前后同一位置与原因。
- 两项普通查询测例（文本、native）在正文已到达后拒绝出现任何 Execute this next step 提示。提示属于 `operations/browser_task_handoff.py` 的调用方选择合同。慢查询用例拒绝首个未确认读取的审计里有 query_click。现有失败结果用该字段连接原动作，且只有成功、完整的 query_region 才构成确认。完成门禁也读取失败动作的关联。记录至 Q084，结合实际提示内容和操作结果复核意图与测试合同。未调整原断言。
- 三源和新回归的定向 mypy 强 Any、Pyrefly、ty、Ruff 全部退出 0。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 与 kernel_type_precision_gate 均退出 0。日志 `.e2e-artifacts/code-quality-20261122/q030c-*.log`。
- Q030C 部分完成，提交独立通过的修复。完成 52/330 批，覆盖 104/1009 个生产文件。部分批次 15，其中 11 个文件独立验证。Q030D 接续页面展开与读取。


### Q030D 原生详情展开观察

- Q030C 已提交 `0cceb56f`。完整检查 `browser_disclosure.py` 和 `browser_detail.py`。HTML 的详情摘要是第一个 summary 元素子节点。当前页面可动态在其前插入普通元素，Chromium 仍用原摘要。
- 真实 worker 页面先有 Shipping address 和 Empty details 两个原生详情，随后在两个摘要前分别插入一个隐藏 span。 `python -m pytest -q tests/test_browser_native_disclosure.py --tb=short` 修改前 1 failed、退出 1，观察列表为空。按第一个直接 summary 子元素识别，正文判定排除同一节点。按原选择读取 Dock 27，空详情不进入选择，页面保持折叠。相同用例修改后 1 passed、退出 0。收敛两处相同的任务 URL 去除模式。
- 在 `D:\codey` 执行 `python -m pytest -q tests/test_browser_detail_production.py tests/test_browser_linked_record.py --tb=short`，基线与最终均 17 passed，退出 0。最终合计 18 项通过，无跳过。两源及回归定向严格 mypy、Pyrefly、ty 和 Ruff 均退出 0。全生产 mypy 763 文件、Pyrefly 0 errors（35 suppressed、165 warnings 未展示）、ty 和 kernel_type_precision_gate 均通过。
- Q030D 完成。累计 53/330 批，完成批覆盖 106/1009 个生产文件。接续 Q031。
