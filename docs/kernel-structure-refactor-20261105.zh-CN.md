# Codey 内核结构整理记录（2026-11-05）

本次直接在主分支完成两项结构整理，刻意没有带入 K-P1。K-P1 只有首轮 prompt token 的局部下降，没有稳定的总请求数或总 token 收益，因此继续留在实验树。

## capability snapshot → typed capability view

新增 `codey/operations/kernel_capabilities.py::KernelCapabilityView`，集中读取一次 kernel 运行需要的 provider 实时能力：native 工具、文本工具格式、request-scoped context 和窗口引用。

durable provider snapshot、API selection、provider 对外属性和兼容字段继续保留。这个改动只整理读取方式，不改变协议选择、权限或恢复流程。

## 四类入口 → typed `KernelRunRequest` builder

新增 `codey/operations/kernel_request_builder.py::KernelRunRequestBuilder`，由 `task_entry`、Project adapter、Research iteration 和 Project audit advisor 统一装配重复的 transport 字段。

各入口的执行依赖和观察依赖仍在调用点显式传入，因此授权、workspace writer、Research ledger、advisor 隔离、durable boundary 和外部 API 差异保持可审计。

## 验证

- 定向回归：**102 passed、6 个子断言通过**；
- Ruff、Mypy、Pyrefly、ty 和相关文件编译检查通过；
- 没有运行全量 pytest；
- 回归中发现的 1 个失败来自测试扫描器把手工脚本内部的低层 `run` helper 误判成 Project adapter 入口，已扩大 AST 扫描器对本地函数的识别范围后通过。

这两项改动属于结构整理，没有宣称模型 token、轮数或准确率收益。后续若出现行为回退，应分别回滚对应提交：`66c0edab`（capability view）或 `1237f63d`（request builder）。
