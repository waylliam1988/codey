# CodeMode 恢复与引用句柄实施进度

## 目标

保持一份 `ToolSpec.output_schema`、一个 `NodeWorkerRuntime`、一个 kernel receipt/durable ledger 和一个生产浏览器 source provider。模型负责工具选择与语义判断；程序负责结果解包、身份绑定、回执恢复、句柄解析和浏览器会话生命周期。

## 批次状态

| 批次 | 内容 | 状态 |
| --- | --- | --- |
| 1 | `unwrap` 合同测试 | 红测试已提交，生产实现进行中 |
| 2 | schema 驱动 `unwrap` runtime | 未开始 |
| 3 | receipt-only repair 测试 | 未开始 |
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
- 生产修改：尚未开始
- 测试结果：`5 failed, 2 passed`；失败均对应尚未实现的 `unwrap` runtime、catalog 声明和硬编码 prompt 缺口
- KoboldCpp gate：待执行
- commit：待提交

## 运行记录

每批完成后记录相关测试命令、失败根因、修复、ruff/type 检查、KoboldCpp 实机结果和 commit。测试失败必须区分测试合同过时与生产实现错误，不以迁就旧测试为默认修复方式。
