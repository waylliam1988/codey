# 请求预算与 token 统计

本文描述当前实现。请求前的上下文计数与请求后的 API 用量是两个独立事实，不能相加。
实现复用现有 API runtime、codec、连接注册和 RunTrace，不增加厂商框架、独立统计存储或迁移器。

## 数据含义与归属

| 数据或职责 | 唯一归属 | 含义 |
| --- | --- | --- |
| 已接纳的连接、模型、协议、计数方式和预算来源 | `runtime/core/api_selection.py` | 冻结运行选择，不保存凭据，不枚举厂商计数器 |
| `ContextBudget` | `providers/token_accounting.py` | 窗口、输出预留、安全余量、裁剪目标及容量来源 |
| `RequestContextCount` | 同上 | 本次最终请求的输入计数；来源为 tokenizer、estimated 或 unknown |
| `ReportedUsage` | 同上 | 服务端报告的输入、输出及缓存/推理明细；缺失字段为 None |
| 编码、合法消息组和裁剪边界 | `api_chat.py` / `api_responses.py` | 保持工具调用与结果配对，不自行估算或决定预算 |
| 最终计数、裁剪、准入和请求生命周期 | `api_provider.py` | 计数后发送同一份 payload，回答解码前记录 usage |
| KoboldCpp 能力与完整模板计数 | `local_tokens.py` | Local 连接解释自己的服务端能力 |
| 原始 usage 字段解释 | `local_usage.py` / `zen/usage.py` | 各连接只解释自己支持的两种协议 |
| 原始事件交付 | `api_transport.py` | JSON/SSE 事件先交观察器，再处理回答，无厂商字段映射 |
| 规范化记录与已知量汇总 | `runs/trace.py` | 按物理请求身份去重；历史读取不导入连接包 |

缓存输入、缓存写入与推理输出都是对应总量的子项，不能再次加入总量。
零是服务端明确返回的零；unknown 不能被伪造为零。字段类型、非负整数及明细边界严格校验。

## 预算与准入

本次有效预算只使用已选模型与连接配置，不再读取 API 静态 capability 的 32K 窗口。
Local 的初始 32K 选项是可修改的连接配置，不是探测出的模型容量。
Zen 目录必须明确提供 context 和 output；缺失时不生成伪默认值，也不接纳该模型。

```text
输入计数 + 输出预留 + 安全余量 <= 窗口
input_limit = window_tokens - output_tokens - safety_tokens
```

现有配置字段 `context_reserve_tokens` 表示输出预留与安全余量之和。
运行时一次性解析为不可变预算，输出上限不得偷偷扩大总预留。
Zen 的输出上限同时受目录 output 和本次预留限制。

流程是：准备合法候选历史 → 编码完整请求 → 添加连接所需参数 → 计数 → 必要时按完整交换裁剪
→ 重新编码并计数 → 发送刚刚计过的 payload。工具声明、思考参数和模板参数都在计数之前加入。
裁剪不能拆开调用/结果组或删除当前交换；失败不提交候选历史。计数期间的取消和超时也会阻止生成。
API 不再使用网页字符累计值触发 rollover；明确继续任务、切换会话等真实生命周期动作仍保留。

## 三类计数依据

| 连接 | 请求前计数 | 请求后用量 |
| --- | --- | --- |
| 网页 | ASCII 约四字符一个 token，非 ASCII 约一字符一个；明确为估算 | 不制造 API usage |
| 确认支持完整模板的 KoboldCpp Chat | `/api/extra/tokencount` 接收完整生成 payload，来源为 tokenizer | 解析服务端返回的 usage，缺失则未知 |
| 其他 Local 兼容连接 | 接纳时明确选择 estimated | 按所选 Chat / Responses 协议解析 usage |
| Zen | 完整请求的显式估算 | Zen 包按实际协议解析服务端 usage |

估算没有统一误差保证。语言、代码、特殊 token、隐藏网页提示、模板与工具格式都会影响误差；
本轮没有用真实服务做精度标定，也不把估算包装成精确计数。

KoboldCpp 必须确认标准 `/v1` 地址、Chat 协议、Jinja 模板及有效运行窗口。
有效窗口取配置与运行窗口的较小值。计数前核对加载模型和窗口，模型更换或窗口缩小时要求重新选择。
已确认的 tokenizer 失败就阻止生成，不降级为估算；关闭 Jinja 或无法确认容量也明确拒绝接纳。
计数与生成是两个 HTTP 操作：相同 payload 不代表能原子锁住外部服务在两次请求间的配置变化。

## usage 生命周期与显示

每次实际生成尝试创建新的 exchange ID，包括工具往返、结束交付和 Zen 内部闭合请求。
一个请求的 usage collector 接收原始事件，保留最新快照，不能把累计 SSE 帧重复相加。
Chat 流式请求在计数前设置 include_usage，保留末尾仅有 usage 的事件；Responses 读取 response 事件中的 usage。
回答解码失败仍保留此前收到的用量；断流、不确定结果、无用量或非法用量明确记录相应状态，不自动重发。
绑定在请求开始时冻结，迟到观察不能被记到之后绑定的运行。

RunTrace 保留最多 64 条请求明细、最后一次请求和所有已观察请求的汇总；超限明确标记 truncated。
汇总分别保存已知输入、已知输出及不完整请求数，不受明细条数截断影响。
这是现有本地运行观察记录，不是供应商账单：观察落盘失败不能重发生成，也不能保证崩溃前所有观察均已落盘。

现有 Run details 显示独立的 `API usage` 和 `Request context` 行。
不完整汇总标为 Known 并说明缺失请求数；估算计数带 `~`。
`Last prepared request` 只表示上次准备发送的请求，不冒称下一请求或实时上下文占用。
不增加常驻统计面板、进度条或供应商品牌样式。

## 移除 Zen 与未来接入

删除 Zen 注册和 `providers/zen/` 后，核心、Local 及规范化历史仍可运行。
Zen 的认证身份、目录、访问资格、内部闭合及 usage 字段映射全部随包移除；专用测试、gate 与文档随之清理。
移除探针使 Zen 不可导入，执行 Local 两种协议及工具往返，并验证规范化 usage。

未来 DeepSeek API 使用独立连接身份（网页 deepseek 保留原义），复用 runtime、codec、记录和显示。
只补该连接实际需要的认证、目录、计数能力与 usage 解释；没有准确计数能力就明确 estimated，
没有 usage 就保持 unknown。未来连接不导入 Zen，也不提前实现未使用的厂商抽象。

行为测试索引见[tests/README.md](../tests/README.md)，实际测红、回归和全量结果见[测试报告](../TEST_REPORT.md)。
