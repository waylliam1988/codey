# Shell 竞态与 pytest 故障修复进度

## 约束

- 优先运行直接相关测试；不运行完整 pytest、覆盖率或安装包构建测试。
- 区分生产缺陷与过时测试；不为测试放宽生产契约。
- 修改使用明确类型，不新增 Any、类型忽略或防御性冗余。
- 完成后运行 ruff、类型检查，更新本记录并 commit。

## 当前状态

- 开始时工作树干净，HEAD `96d3eef3`。
- 用户随后补充了具体 33 项失败，明确以该清单为范围。已停止按旧日志扩大验证；旧日志日期与失败节点不再作为本轮验收目标。
- 初始 shell remembered 6 passed；shell continuation / approval epoch / command permissions 57 passed，原 CI 单次失败未在独立运行中出现。
- 33 项所涉核心模块初始复现：9 failed / 100 passed / 5 subtests passed；日志 `.e2e-artifacts/current-33-core-before.log`。
- 原有真实浏览器测试独立运行 18 passed，证明不能把完整 CI 的状态污染简单归结为单个 UI 功能必现故障。

## 已确认的根因与修复

### 生产：Zen 目录输出不是严格 JSON

- `ZenModel.to_payload()` 原来调用 `asdict()`，`efforts` 仍是 tuple。
- `model_settings` 对外部数据使用严格 JSON 解码；非空模型缓存进入 settings / discover 路径时抛 `JSON cannot encode tuple`，refresh 被映射为 HTTP 400。
- 在模型输出所属边界构造明确 `JsonObject`，将 efforts 转为 list；未放宽 JSON 解码器。
- 新增空/非空 efforts 的严格 JSON 回归；增加两种协议的真实浏览器回归：预载非空 Zen 缓存、Zen 禁用、Local 发送并完成。
- 相关目录 / 设置 / 偏好回归 68 passed；新增浏览器回归 2 passed。

### 生产：durable 状态无法说明 volatile reservation 的所有权

- 为保护 HTTP 失去 claim 时仍运行的 worker，旧代码只在 WAITING / terminal 状态释放未启动 reservation。
- 重复请求给已 RUNNING 的 checkpoint 创建新 reservation 时，该 reservation 没有 worker 却无法释放；原 `False-False` 用例确定性复现 busy 泄漏。
- 反过来，durable 已停泊或完成也不代表已排队 worker 放弃 reservation，不能仅凭状态释放。
- RunSnapshot 显式记录 worker_owned；HTTP / scheduler 在发布 worker job 前标记所有权，start 与替换保留所有权，拒绝入队归还所有权。
- scheduler 将 reservation 查找/创建与 worker 绑定放在同一 durable control line，避免 HTTP 清理在二者之间抢走 reservation。
- 新增 dispatching / waiting / completed 三种 durable 状态下，已排队 worker 仍能发布且只能发布一次 terminal projection 的回归。

### 测试：交错控制与共享状态污染

- shell HTTP 竞态测试原来在 admit_input 发布后才登记 delayed_run，快 worker 可先越过拦截点。改为 reservation 阶段登记，发生在 durable 发布前；等待也放入 finally 保护范围，失败时释放另一线程。新增 worker 在 admit_input 返回前即抵达拦截点的强制时序，覆盖两种协议与两个完成边界。
- `test_removed_zen_is_not_scheduled_or_imported` 使用 monkeypatch.delitem，撤销时将 Zen 追加到 provider 字典末尾，污染后续顺序断言。改用 patch.dict 保持原有对象与完整顺序。
- boot 顺序测试把重连回调中的 connectEvents 当成 boot 调用。只检查真实 boot 函数，生产授权顺序未修改。
- TaskState 测试仍禁止现有 typed claim_shell_ticket 端口，两个 shell 夹具缺少 TaskRuntimeCoordinator。更新为真实 AppContext / coordinator 接线，并断言 claim 在 spawn gate 内。
- prompt golden 包含解释器路径、Windows 版本和平台。固定 shell discovery 与 prompt host 输入；离线对比证明旧本机 prompt 仅替换解释器路径/版本即得到新哈希 `a8efb12fd24924da7f5dafd2047906236e85e375d8b1760a5d0ac5798458cab2`，未更改生产提示词。
- 连接重置日志来自断开连接的 socketserver 路径；生产 CodeyHTTPServer 已按异常类型忽略常规断开，不通过修改生产来掩盖模型 peer 日志。

## 中间验证

- reservation / shell continuation / provider order / typed port / boot 相关：111 passed / 5 subtests passed。
- 对照 HEAD 四个原生产文件：7 failed / 4 passed。失败为两个严格 JSON 用例、WAITING / completed 两种 queued worker 所有权、重复 admission 的 False-False 泄漏、两个真实浏览器 Send disabled 用例。日志 `.e2e-artifacts/current-33-new-regressions-old-production.log`，脚本已在 finally 中恢复四个工作文件。
- 扩展 shell 交错：10 passed / 20 deselected，包括提前完成与 claim 期间完成两种时序；未通过加 sleep 或放宽超时取得通过。
- `python -m ruff check codey tools tests`：通过。
- `python -m mypy codey --python-version 3.11`：646 files，通过。
- `pyrefly check codey --python-version 3.11`：0 errors，35 suppressed、130 warnings；不将 warnings 描述为零。
- `ty check codey --python-version 3.11`：通过。
- `python -m tools.kernel_type_precision_gate`：通过。
- 新 JSON 单元测试单独 strict mypy：1 file，通过。
- 四个修改生产文件的 AST Any 引用：run_registry / task_runtime / task_submit 均 0 → 0，Zen catalog 6 → 5；迁移的 to_payload 边界无 Any，其余五个是既有目录解析注解，未宣称全仓清零。
- 最终原 shell matrix 与同进程相关回归均完成，结果如下。

## 最终验证

- 用户最初的八文件 shell 命令：**159 passed / 11 skipped**，196.02 秒。新增了四个提前到达边界的交错组合，未新增 skip；JUnit 保持 `.e2e-artifacts/shell-related.xml`，日志 `.e2e-artifacts/current-33-shell-final.log`。
- 33 项失败关联模块及所有权直接影响路径：**324 passed / 469 subtests passed**，122.54 秒，无失败、无跳过、无 warning。JUnit `.e2e-artifacts/current-33-related-final.xml`，日志 `.e2e-artifacts/current-33-related-final.log`。
- 后一组覆盖 zen_model_json_payload、zen_catalog_lifecycle、zen_catalog_free_protocols_and_scoped_identity、current_regressions、model_preferences、done_summary_production_prompts_unchanged、operator_bootstrap_before_ui_requests、task_state、task_submit、task_submit_durable、run_registry、task_runtime_coordinator、connection_status_cold_start、providers、conversation_live_browser、uncommitted_live_browser、durable scheduler / scheduler_claim_release / scheduler_wakeup_races / worker_fencing、architecture。
- 最终两组共 **483 passed / 11 skipped / 469 subtests passed**。不把中间重跑相加来虚增最终测试数。
- Ruff、mypy、Pyrefly、ty、precision gate 与 git diff --check 全部通过；类型检查目标 Python 3.11，行为测试本机 Windows Python 3.12.8。
- 未运行完整 pytest、覆盖率报告、安装包构建或安装包测试；未修改打包配置。

## 交付

- 本轮修复生产缺陷与过时/受污染的测试，新增回归先在旧生产代码失败，再在最终修复通过。
- 无新增 Any、cast、类型忽略、检查豁免或防御性兼容分支；保留 JSON 边界的严格校验。
- 文档随修复一起提交；提交说明：`fix: preserve worker reservations and emit JSON catalog models`。
