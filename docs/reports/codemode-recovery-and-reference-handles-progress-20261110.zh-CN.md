# CodeMode 恢复与引用句柄实施进度

## 目标

保持一份 `ToolSpec.output_schema`、一个 `NodeWorkerRuntime`、一个 kernel receipt/durable ledger 和一个生产浏览器 source provider。模型负责工具选择与语义判断；程序负责结果解包、身份绑定、回执恢复、句柄解析和浏览器会话生命周期。

## 批次状态

| 批次 | 内容 | 状态 |
| --- | --- | --- |
| 1 | `unwrap` 合同测试 | 已完成 |
| 2 | schema 驱动 `unwrap` runtime | 已完成 |
| 3 | receipt-only repair 测试 | 已完成 |
| 4 | repair token 与精确回放 | 未开始 |
| 5 | receipt/source/evidence 短句柄测试 | 未开始 |
| 6 | 句柄 resolver | 未开始 |
| 7 | 已有浏览器会话与知乎 403 | 未开始 |

## 约束

- 不新增第二套 CodeMode runtime、receipt ledger、正文缓存或浏览器 provider。
- 不引入新的 `Any`；修改过的生产文件要同时减少动态类型。
- 每批先写确定性测试，再修改生产代码；只运行与改动直接相关的测试和 gate。
- CodeMode repair 只能读取已经 settled 的原始 receipt，不能重新调用 `execute_turn`。
- `r1` 等现有研究句柄不能与 receipt 句柄混用；最终需要有类型前缀的命名空间。

## 第一批记录

- 测试文件：`tests/test_codemode_unwrap_contract.py`、`tests/test_codemode_catalog_result_guidance.py`
- 生产修改：`guest_runner.js` 提供 `unwrap`；catalog 生成 `ToolResult<T>`、`Result<T>` 和 `unwrap` 声明；CodeMode 示例改用 `unwrap`；schema 类型渲染移除 `Any`
- 测试结果：相关 CodeMode/runtime/kernel 测试 `93 passed`
- 静态检查：`mypy`（修改的 schema/catalog）通过，`ruff` 通过，Node 语法检查通过
- KoboldCpp gate：已确认 `http://127.0.0.1:5001/v1` 可用；专用模型行为 gate 待在 repair 批次完成后统一接入
- commit：待提交

## 运行记录

每批完成后记录相关测试命令、失败根因、修复、ruff/type 检查、KoboldCpp 实机结果和 commit。测试失败必须区分测试合同过时与生产实现错误，不以迁就旧测试为默认修复方式。

## 第二批记录

- `guest_runner.js` 提供 `unwrap`，catalog 使用 `ToolSpec.output_schema` 生成 `Result<T>` 声明和示例
- 相关 CodeMode/runtime/kernel 测试已通过；没有增加第二套 runtime 或工具类型表
- commit：`7d10270c feat: add schema-driven unwrap runtime`

## 第三批记录

- 测试文件：`tests/test_codemode_repair_replay.py`
- 红测试结果：`4 failed, 2 passed`；失败对应 `repair_of` 尚未进入协议、失败父回执尚未生成 token、repair 尚未走 receipt-only host
- 测试使用真实 `build_execute_turn_executor`、`execute_turn` 和 `ResearchTools`；source provider 是确定性的脚本源，只用于控制版本和调用次数
- 生产修改：`repair_of` token 解析、原始 settled child receipt 精确回放、能力指纹绑定、一次性 token 使用保护；repair 路径不调用 `execute_turn`
- catalog 校验只绑定实际回放工具的完整能力描述，避免打开来源后新增 `reopen_source` 等无关工具造成误拒绝；工具撤权、参数或输出 schema 改变仍 fail closed
- 测试结果：`tests/test_codemode_repair_replay.py` 7 passed；与 CodeMode 相关回归共 76 passed
- 静态检查：ruff、mypy、`git diff --check` 通过
- KoboldCpp gate：待专用 gate 脚本完成后执行
- commit：待提交
