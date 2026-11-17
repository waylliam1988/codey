# 正常 App 硬退出与真实 HTTP 恢复审查

- 日期：2026-11-18；关联 0.6.1–0.6.4。
- 状态：冻结旧 smoke 单项诊断，尚未运行；真实 HTTP 矩阵待接口核对后单独冻结。
- 代码基线：`6d844752`；上一轮裁剪修复 `34852638` 保留，不重复其矩阵。
- 模型：本阶段不调用模型；旧 smoke 使用离线 scripted writer/provider。

## 已有证据与实际缺口

`tests/durable/test_prepared_provider_request.py` 有真实 SQLite/os._exit，但 provider 是
fake；`tests/test_ghost_native_refusal_recovery.py` 有真实 loopback HTTP/shared kernel
及 warm/cold 同 run，但不是完整 App 硬退出。普通 App→HTTP 的偏好准入/撤回已在
`tests/test_ghost_language_production.py` 验收，不能据这三组独立证据宣称它们的组合已验。

旧 `tests/manual/completion_operation_resume_smoke.py` 声称正常 headless 硬退出和
同 run 续传，但仍以 positional JSONL adapter 构造现在要求 keyword-only durable 的
operation store，并用相同 run_id 再次提交普通 headless。当前 production 只从 durable
checkpoint/effect 恢复，普通 headless 重复提交应拒绝；静态疑点须先实际诊断归属。

## 冻结诊断 A：旧 smoke

目标：区分过时验证材料与有效生产恢复失败。主要变量只有原样旧 smoke 对当前代码；
对照为代码中的现行 durable owner/重复 ID 契约，不构造兼容生产补丁。

- 入口：`python -B tests/manual/completion_operation_resume_smoke.py --self-test`。
- 样本：原样执行一次；冻结脚本和整个 codey 生产树 SHA256，不修改脚本/生产后复跑
  并替换原结果。无模型、endpoint、采样参数或随机 arm。
- 指标：退出码、首个失败点、是否真实进入 writer/持久恢复/续传、是否有无主子进程。
- 成功：原脚本到达它声称的恢复断言并通过；失败：精确记录接口或运行契约违反。
  参数不匹配或旧结构访问属于脚本失效，不能称为模型阴性或生产丢状态。
- 停止：一次结束即停止；父进程上限 60 秒，子进程由脚本 finally 清理。超时另记
  `INCOMPLETE`，确认真实进程状态后清理，不把观察超时当进程自然退出。
- 检查：诊断后运行全仓 Ruff、完整 codey 的 mypy/Pyrefly/ty/precision gate。
  本阶段不修改 Python，不以旧脚本预存的 Any 代表新代码满足类型要求。

原始 stdout/stderr、检查日志及冻结 metadata 仅留
`.e2e-artifacts/ghost-app-http-crash-recovery-20261118`；仓库留本记录和脱敏指标。
旧脚本使用临时目录，不能在失败后假称保留了独立的崩溃数据库。

## 后续交付与验收

诊断完成后及时提交原结果。若旧脚本失效，按现行 durable owner 和真实恢复入口修正
验证材料，完整清理该文件参数/返回值/回调/容器及动态边界的显式和隐式 Any；不使用
cast、ignore、豁免或放宽检查。保留它验证硬退出、原 run/operation 唯一性、诚实进度、
路径脱敏、已提交状态恢复和终态的能力；旧 JSONL lane 的假设须映射到现行 durable
任务/提交/效果契约，不能只删除断言。

真正缺失的 App×physical HTTP×硬退出组合另行冻结目标、配置、任务、对照、scope、
样本、指标和停止条件，采用正常入口/实际 ApiProvider/SQLite。先复用已验证的受测
Gemma 中文偏好配置作为生产消费者边界；loopback 固定回复只证明接线，不冒充新模型
收益或当前模型服务健康。恢复不能重复不确定发送，不能以新 run 偷换原 run 的恢复，
也不能把正常 headless 的新提交入口改成旧 ID 重放入口。

本轮完成前不标 `PRODUCTION_MERGED`，不新增默认关闭模块或功能开关。若当前生产
本已正确，交付组合验收和修正的验证工具即可，不强行改生产。相关回归、完整静态门、
文档和独立 commit 完成后继续其他 G0 缺口与有新增预测信息的 World Model 消费者。
