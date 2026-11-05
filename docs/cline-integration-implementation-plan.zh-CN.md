# Cline 可移除连接实施方案

状态：2026-11-05 实施基线，主体已实现并通过相关回归和实机验证。
本文保留最初的逐字段设计，当前接口以源码与[模型管理说明](model-management.md)为准；
实际交付、与设计的差异及未验证项见[验收报告](reports/cline-integration-validation-20261105.zh-CN.md)。
实施与提交进度见[进度记录](cline-integration-progress.zh-CN.md)。

## 1. 产品约定与已验证边界

- 输入框保持现状：`[模型名称 ▾] [思考等级 ▾]`，不增加 `Cline ·` 前缀。
- 设置页的模型来源显示 Cline；同名模型只在下拉列表中用来源辅助区分。
- 不向提示词追加“你是 Codey”“欢迎使用 Codey”或 Cline CLI 的代理系统提示词。
- 沿用 Codey 的任务、工具、权限、审批、上下文和思考内容展示流程。
- 自写 HTTP 请求，不执行 Cline CLI，不依赖 `@cline/sdk`。
- 初版目标为当前桌面 Codey 的本机后端，一个操作系统用户使用一个 Cline 登录账号；不把本机单用户服务直接当成云端多用户服务。

已经实测：WorkOS 设备授权、Cline 注册换取令牌、刷新令牌、读取免费目录、非流式普通对话；Solar Mini 4 连续两轮 HTTP 200，首轮返回 `usage.cost=0`。

已定位根因：Cline 凭证适配层 `formatClineApiKey()` 为推理密钥添加 `workos:`。同一令牌和请求，不加前缀 401，加前缀 200。

推理请求：`Authorization: Bearer workos:<Cline accessToken>`。注册和刷新请求的 JSON 字段仍使用原始 token。

实测使用了 Cline CLI 兼容请求头。这不证明省略这些请求头或换成任意客户端标识也能成功。初版封装为单一兼容配置，不自动轮换身份。

初稿时尚未实测的 SSE、工具调用/结果续聊、17 个当前公布的思考选项现已通过实机；
Solar 和 Nemotron Super 完成真实代码修复，Solar 完成 Shell 审批续接。
压缩、恢复、取消与清理竞态使用确定性回归；未人为耗尽真实额度或清除生产账号。
Beam 本轮 JSON/SSE 两轮和工具读取通过，原上游 Invalid request 未复现，不能据此推断其所有任务永久可用。

## 2. 本次代码审查发现的现有约束

| 位置 | 已有行为 | 对设计的影响 |
|---|---|---|
| `codey/providers/catalog.py` | `API_CONNECTIONS` 保存标签与模块路径 | Cline 只增加一个注册项 |
| `api_connections.py` | 按注册表延迟导入连接模块 | 内核不直接导入 Cline |
| `app/model_settings.py` | 遍历连接，读取缓存和刷新目录 | 复用来源、勾选、保存和刷新 |
| `frontend/src/provider_ui.ts` | 按 `efforts` 和 `thinking_toggle` 生成控件 | 不新增 Cline 专属思考控件 |
| `api_chat.build_payload()` | 写入 `reasoning_effort`、`chat_template_kwargs.enable_thinking` | Cline 适配器必须移除这些通用字段，再写实际支持的网关字段 |
| `api_chat._store_assistant_message()` | 保留文本、工具调用、归一化思考文本 | 不完整保留 `reasoning_details` 等协议元数据 |
| `api_stream.read_sse()` | 合并文本、思考字符串和工具调用 | 不完整合并结构化思考元数据 |
| `ReportedUsage` | 只有 token 统计，没有费用字段 | 本次不承诺新增费用 UI；不能把上游成本当用户费用 |
| `ConnectionStatusCoordinator.observe_catalog()` | 非空模型目录默认视为 ready | OAuth 来源不能仅因公共目录可读就显示已连接 |
| `provider_ui.selectedEffort()` | toggle 的 null 选择目前折成 off | Cline 若支持服务端默认，必须保留 null/on/off 三态 |

下面的字段、接口名称和阈值保留实施前设计语境。已落地的主要差异：完整清理通过
通用 `clear_data` 操作和停服维护工具执行，不在设置页增加删除按钮；安全存储首版仅 Windows；
意外用户费用阻断另存 `blocked.json`；真实非流式响应的 success/data 包装由包内 codec/usage 解开。

## 3. 文件组织与依赖边界

新增：

```text
codey/providers/cline/
  __init__.py
  connection.py          现有连接协议的实现
  runtime.py             单用户连接资源、生命周期、依赖注入
  identity.py            地址、兼容请求头、协议修订
  auth.py                OAuth 状态机和刷新协调
  credentials.py         安全存储，原始 token 的唯一持久所有者
  catalog.py             免费资格、缓存、刷新和模型能力
  capabilities.py        能力解析及经过验证的别名映射
  reasoning.py           已选思考模式到 Cline 网关字段的编译
  request_policy.py      有限重试、认证失败、冷却
  codec.py               Cline 的协议历史适配（需要时注入）
  stream.py              Cline 的 SSE 扩展字段归并
  usage.py               Cline token 用量解析

codey/app/connection_actions.py   通用连接操作分发，不导入 Cline
```

新增注册：

```python
"cline": ("Cline", "codey.providers.cline.connection"),
```

允许共享模块新增通用扩展点，但生产共享代码中不出现 `if id == 'cline'`、Cline URL、`workos:` 或 Cline 专属请求字段。Cline 包可以依赖通用层，通用层只能通过注册与接口调用包。

`runtime.py` 接收可替换的 HTTP、时钟、浏览器启动器、TokenStore、目录缓存目录。生产使用标准本机状态目录与系统安全存储；测试全部注入临时资源。模块导入不得打开浏览器、访问网络、读取凭证或启动线程。

同一进程的 UI 操作与生成工厂必须使用同一个 `runtime()`；不能由 UI 新建一个 TokenStore、推理工厂再读取另一份文件。初版一个操作系统用户只有一个默认凭证目标 `Codey/Cline/default`。测试使用独立命名空间，不能碰生产目标。以后增加多个登录配置时，应先扩展通用配置身份与 worker 传递，再开放多配置 UI。

## 4. 数据结构：逐字段定义

### 4.1 仅在后端内存/安全存储中的 CredentialRecord

```python
@dataclass(frozen=True, repr=False)
class CredentialRecord:
    access_token: str          # 原始 Cline token，不保存 workos: 包装
    refresh_token: str
    expires_at: float          # UTC Unix 秒
    account_epoch: str         # 每次完成新登录生成 UUID；刷新不改变
    token_version: int         # 每次成功刷新递增
    reauth_required: bool = False  # 已明确拒绝，独立于最近一次 OAuth 尝试状态
```

禁止自动 dataclass repr 打印 token；异常不得携带完整 HTTP 请求或 OAuth 响应。TokenStore 以整条记录保存，避免 access/refresh 分别写入导致半更新。Windows 初版用系统凭证存储；失败时显示存储错误，不静默降级成明文 JSON。

明确失效的凭证必须保留 `reauth_required`，重新登录失败/过期/取消、后端重启均不能
凭“存储里有记录”恢复 ready。按账号 epoch 与 token version 匹配迟到拒绝；只有新认证
成功写入的记录恢复准入。该标记不返回前端，旧记录缺字段按 false 读取。

### 4.2 公开的认证快照

```json
{
  "kind": "oauth_device",
  "state": "signed_out",
  "revision": 12,
  "attempt_id": null,
  "expires_at": null,
  "user_code": null,
  "error_code": null,
  "actions": ["login"]
}
```

`state` 枚举：`signed_out / starting / awaiting_authorization / exchanging / ready / expired / cancelled / reauth_required / error`。

`attempt_id` 是 Codey 自己生成的随机操作标识，不是 WorkOS device_code。`user_code` 只在等用户确认时返回给已认证的本机操作员 UI，禁止写入事件历史、运行快照、日志或 localStorage。access_token、refresh_token、device_code 永不进入公开快照。

`revision` 用来拒绝旧轮询结果；不能复用模型偏好 revision。`actions` 由后端状态计算，前端不能自行授权额外操作。

### 4.3 模型来源快照

```json
{
  "id": "cline",
  "label": "Cline",
  "discoverable": true,
  "connection_editor": false,
  "authentication": {"kind": "oauth_device", "state": "ready", "revision": 12, "actions": ["logout"]},
  "state": "ready",
  "stale": false,
  "checked_at": 1790000000,
  "models": [],
  "unavailable_models": []
}
```

模型条目继续暴露现有 `id/name/protocol/efforts/thinking_toggle`，新增可选的通用字段：

| 字段 | 类型 | 用途 |
|---|---|---|
| `thinking_default_allowed` | boolean，默认 false | 是否能选择服务端默认，保留 null |
| `availability` | available / unavailable / capability_unknown | 区分目录存在和可运行 |
| `unavailable_reason` | 字符串 | 不可选原因 |
| `trial` | boolean | 仅用于设置页/列表的试用说明 |

不得用模型是否有 reasoning token 来推断 `efforts`。`efforts` 示例中的 low/medium/high 必须来自可信能力声明并通过适配测试，不是所有模型的统一默认值。

## 5. OAuth：每个入口的执行顺序

### 5.1 identity.py

固定生产 HTTPS 地址和公开 WorkOS client_id；不接受 UI 传入任意 token endpoint、回调 URL、推理 base_url。

`inference_api_key(raw)`：strip → 拒绝空值 → 已有 `workos:` 则不重复添加 → 否则添加。此函数只用于 Authorization 构造，不修改存储记录。

`client_headers(task_id)`：返回已经验证的 CLI 兼容配置；版本号与协议修订集中保存。`X-Task-ID` 使用稳定的逻辑请求/任务标识，不由每个 TCP 重试重新生成。兼容配置升级独立审查，不在运行时下载或执行 CLI。

### 5.2 auth.start_login()

1. 在短锁内检查已有活动 attempt；重复点击返回同一个 attempt，而非打开多个浏览器。
2. 已有 ready 会话时不覆盖凭证；需要换账号先退出。
3. 创建 `attempt_id`、代次、取消事件；状态置 starting；返回可轮询快照。
4. 后台 POST WorkOS device authorization，Content-Type 为 form，body 为 client_id。
5. 校验 response 的 code、URL、expires_in、interval；浏览器 URL 只能取预期 HTTPS 授权域名，不能信任任意响应 URL。
6. 先确认 attempt 仍有效，再记录仅内存的 device_code，设置等待状态和用户可见 user_code，打开系统浏览器一次。
7. 浏览器打开失败保留待授权状态，提供“重新打开登录页”操作，不重新申请 device_code。

### 5.3 auth.poll_device()

- 使用设备响应给出的 interval；使用可取消 wait，避免长时间不可中断 sleep。
- authorization_pending：继续；slow_down：增加轮询间隔。
- expired_token/access_denied/invalid_grant：终止 attempt，清空 code，不无限重试。
- 短暂网络失败只在设备有效期与次数预算内等待重试；超过 deadline 结束。
- WorkOS 成功后置 exchanging，POST Cline `/api/v1/auth/register`，传原始 accessToken/refreshToken。
- 校验 success、data.accessToken、refreshToken、expiresAt；将整个 CredentialRecord 写入 TokenStore。
- 写入前后都校验 attempt 代次。若用户已退出/取消，迟到的成功结果不能恢复 ready，也不能留下凭证。
- 成功：新 account_epoch；清空一次性 code；通知状态和模型目录更新。不要自动启用模型偏好。

### 5.4 auth.get_valid_credentials(deadline, cancelled, expected_epoch)

1. 读取记录；缺失则抛认证必需错误。
2. epoch 不匹配则拒绝；不能用用户后来登录的新账号发送旧运行。
3. 剩余有效期超过刷新提前量（拟 120 秒）直接返回。
4. 否则取得刷新专用锁，再读记录；其他线程已更新 token_version 则复用新值。
5. POST Cline `/api/v1/auth/refresh`：`refreshToken`、`grantType=refresh_token`。
6. 刷新成功原子保存，保持 account_epoch、递增 token_version；只有存储成功才发布新版本。
7. 刷新 definitive rejection → reauth_required；网络失败保留原记录，不能把暂时断网当作主动退出。
8. 锁等待、HTTP 和落盘都受请求 deadline 和取消信号约束。

锁定顺序固定：不持 AppContext.lock 做 OAuth 网络；不持 auth 状态锁等待浏览器；刷新锁内只允许一个刷新事务。多进程支持必须使用系统命名互斥锁/等价机制保护 refresh token 轮换，不能仅靠 threading.Lock。

### 5.5 退出与关闭

logout：在通用连接操作层检查 Cline 是否有活动任务/模型使用计数；有则返回 409 `connection_in_use`，让用户先停止任务。无活动使用时，增加 auth 代次、取消登录 attempt、删除安全凭证、清除内存访问凭证、改变 status_key、发布 signed_out。

退出与开始任务的 admission 必须通过同一个短事务门控：不能检查“空闲”后解锁，再让新请求进入，然后删除其凭证。前置计算可在锁外，最终 admission/退出状态转换在短锁内完成；不在该锁内做网络。

close()：取消登录线程和等待任务，关闭本进程资源，保留已保存的登录凭证；正常退出 Codey 不等于退出 Cline。

## 6. 通用连接操作 API：共享代码的精确改动

### 6.1 新增 app/connection_actions.py

仅通过 `connection_for(source)` 延迟获取模块。模块可选暴露：

```python
authentication_payload() -> dict
connection_action(action: str, attempt_id: str | None) -> dict
shutdown_controls() -> None
```

未暴露认证能力的 Local/Zen 返回不支持，不要求它们补空实现。新增通用异常到 HTTP 的映射，禁止直接把第三方异常全文作为 500 返回。

### 6.2 server.py

在现有 `_GET_ROUTES` / `_POST_ROUTES` 各增加一项：

```text
GET  /api/connection_auth?source=cline
POST /api/connection_action
```

POST 示例：

```json
{"source":"cline","action":"login"}
```

取消示例：`{"source":"cline","action":"cancel_login","attempt_id":"Codey attempt UUID"}`。

后端白名单操作：login、reopen_login、cancel_login、logout。清理全部数据走单独的明确操作，不把普通 logout 自动扩成删除模型偏好与缓存。

成功返回 `{"ok":true,"authentication":{...}}`；开始后台登录返回 202。错误统一 `{"ok":false,"code":"...","error":"安全的用户说明"}`。

错误码：source_unavailable（404）、authentication_unsupported（400）、invalid_action（400）、stale_attempt（409）、connection_in_use（409）、reauth_required（401）、auth_storage_failed（503）。这里的 401 是本机连接状态错误，前端不得将它误当作 Codey 操作员 cookie 失效并退出整个应用。

新路由复用现有 operator session 和 Origin 校验，不添加未认证 OAuth token 接收入口，不要求本地回调监听端口。

### 6.3 app/model_settings.py

`settings_response()`：可选读取 authentication_payload 并合并来源；只读缓存，不能启动登录/刷新令牌/网络请求。

`discover_response()`：刷新公共目录本身不证明账号 ready；返回状态由连接快照提供。保留偏好 revision 冲突和 in_use 检查。

### 6.4 ConnectionStatusCoordinator

`observe_catalog()`：若 payload 提供合法的显式 `state`，使用它；缺省时保持现有 models 非空判 ready 的行为，避免改变 Local/Zen。

认证 signed_out/reauth_required 对应连接 unavailable/blocked，不能因为目录仍有模型就显示 ready。此状态必须使 status_key 变化，避免 `_last_good` 把旧账号 ready 结果保留下来。

`probe_status()` 不发送付费/免费探针聊天，不打开浏览器；只检查登录状态、令牌有效性、目录与所选模型资格。ready 表示可尝试发送，不表示已确认远端剩余额度。

### 6.5 生命周期

ProviderRegistry 新增通用的关闭连接控制资源方法，只遍历已初始化的控制模块；AppContext.close 调用它。不能为了关闭去导入所有可选包。连接包缺失时不阻止其他资源关闭。

## 7. 免费目录和能力：解析到字段的规则

1. catalog.refresh：请求 Cline recommended-models；只提取 free 数组中的精确 ID。
2. 校验所有条目类型，去重；不按 `free` 子串、名称、价格或前次名单补充资格。
3. 能力来源独立读取、缓存；匹配顺序为精确 ID → 包内经过验证的别名表。禁止只截取最后一段 slug 后模糊匹配。
4. 解析 context、最大输出、原生 tools、推理控制、独立输入上限。
5. 无有效上下文/输出上限的模型标为 capability_unknown，不用默认 128K 假装已知。
6. 有推理能力但无 effort 声明：不生成 low/medium/high。
7. 缓存带 schema_version、checked_at、资格 revision、能力 revision；原子替换完整快照。
8. 刷新失败保留旧快照并标 stale，不能用空网络错误响应抹掉用户选择。
9. 发送前免费资格快照拟要求不超过 300 秒；过期则先刷新，失败则阻止发送并提示重试。此阈值为产品策略，需可测试。
10. 新目录移除模型：不再准入新请求；旧聊天保留历史与模型名称，要求重新选择。不得自动切换付费模型。

只从免费分组准入不意味着 Codey 能控制远端账务；促销资格与额度最终仍由 Cline 执行。若服务端返回非零用户费用，应记录异常并阻止继续自动试用，不能篡改用量为零。

## 8. connection.py：从选择到生成的每一步

### capture_selection(selection)

1. selection 必须是 object，model 必须是非空字符串。
2. 不接受用户覆盖推理 base_url、Authorization、额外请求头或 OAuth endpoint。
3. 读取当前账号 epoch，必须已登录；检查模型仍在 fresh free 集合。
4. 加载该模型能力；不支持的 effort/toggle 明确报错，不默默改成其他值。
5. 优先复用 `compile_selection` 的 effort/toggle 互斥检查。
6. 冻结 model_id、protocol、预算、tools、effort、thinking。
7. connection_revision 编码/派生自协议修订、账号 epoch 和该模型能力契约摘要；不含 token 或 token hash。不因 access token 正常刷新而改变，不因其他无关模型加入目录而改变。
8. 不修改系统提示词来声明 Codey/Cline 身份。

### validate_selection(selection)

- 必须仍注册、当前账号 epoch 相同、该模型契约相同、仍免费、所选控制仍支持。
- 同账号 access token 刷新保持可用；退出重登或换账号使旧 selection 失效。
- 输出安全错误，不能拼接 token、邮箱、用户 code 或整个服务端 response。

### open_selection(selection)

- validate 后创建 ApiProvider，base_url 固定为 Cline 推理 API，protocol 使用已验证的 openai-completions。
- 不在 ApiProvider.api_key 中冻结长期使用的 token：传空字符串，动态 request_headers 回调在每个实际发送前取得有效 token，再设置完整 Authorization。
- request_headers 必须仅向预期 Cline HTTPS origin 提供凭证；禁止跨 origin 重定向转发认证。
- 请求头回调同时校验 expected account_epoch 和退出代次；401 后刷新不能借用另一登录账号。
- 回调保留取消信号和截止时间。为此给 ApiProvider 增加可选参数 `request_context_headers: Callable[[RequestContext], Mapping[str, str]] | None = None`，保留现有 request_headers(base_url) 签名。`_headers(context)` 的顺序是基础头 → 原 request_headers → 新 request_context_headers；新回调仅在生成请求有真实 RequestContext 时执行。Cline 使用新回调，不使用旧回调做有 deadline 的刷新。`_generate_attempt()` 在实际网络发送前将当前逻辑请求 context 传给 `_headers()`。辅助生成复制该回调。不能把线程私有上下文作为唯一凭证有效期来源。
- request_policy 使用包内 Cline 策略；configure_request 使用包内 reasoning 和 usage 配置。
- 完成后按现有 bind_usage、display、close 机制管理；不另建绕过内核的聊天通路。

## 9. 思考等级：UI 值到 HTTP 字段

### 9.1 统一语义

| 用户选择 | selection.effort | selection.thinking | 行为 |
|---|---|---|---|
| 默认 | null | null | 不发送强制思考控制 |
| 某个等级 | 真实支持的枚举 | null | 发送该模型已验证的 effort 映射 |
| 开 | null | true | 仅限支持 toggle 的模型 |
| 关 | null | false | 仅限支持关闭的模型 |

不能同时提交 effort 与 thinking；不能把“没有返回思考文字”当成思考关闭。

### 9.2 reasoning.configure_request(payload, selected_capability)

通用 api_chat 会先生成一些字段，因此适配器首先移除自己负责替换的 `reasoning_effort` 和 `chat_template_kwargs.enable_thinking`；不删除其他模块负责的字段。

读取包内 capability 声明的 wire strategy，再生成 Cline 当前源码支持的网关 reasoning 结构。示例：已验证支持网关 effort 的模型，可编译成 `reasoning: {effort: 'high'}`；默认不添加 reasoning。关闭思考的 wire 结构、预算控制、max_tokens/max_completion_tokens 必须按模型逐项核对源码并实测，不能一套映射套全部模型。

模型族特殊规则全部留在 Cline 包；不把 Cline 的规则改进 Local/Zen 的 build_payload。参数被服务端忽略也不能算等级验证通过；先验证发送参数，再确认模型能力与服务端接受情况，不能以回答长度推断 effort 生效。

### 9.3 provider_ui.ts 的精确修改

- 保持按钮文本生成逻辑，只显示 model.name，不拼接 connection label。
- ProviderModel 和 ModelSource 使用同一份模型条目类型，保留 efforts、thinking_toggle、thinking_default_allowed，避免某次 observeCatalog 合并后字段丢失。
- thinkingOptions：efforts 非空仍返回 default 加真实等级；toggle 且 thinking_default_allowed=true 时返回 default/off/on；旧模型缺字段保持原行为。
- selectedEffort：新模型 thinking===null/undefined 且允许默认时返回 default，不能用 `===true ? on : off` 抹掉三态。
- runSelection：default 发送 null；on/off 精确传 true/false；不要把 null 强制变 false。
- 模型切换清除不适用的另一控制轴。目录移除等级/控件或变更控制轴时，清理失效字段及
  缓存，并持久化 UI 专用 `reasoning_reselect_required`。提示重新选择模型或有效选项，
  在此之前阻止发送；隐藏控件不能继续发送旧等级。后端仍做最终验证。
- 每个聊天只按自身模型核对；不把当前聊天控制值复制到其他聊天。非当前聊天在提交前
  同样核对；新聊天与现有“保留模型/等级”规则一致，不能绕过待重新选择状态。
- 同名条目可显示来源辅助文字，但选中按钮继续只显示模型名。

## 10. 历史和 SSE：防止第一轮成功、续聊失败

现有 api_chat 会重建 assistant 消息，结构化 reasoning_details 可能丢失；SSE 也只归并思考字符串。不能将“通用 OpenAI compatible”视为无损兼容所有 Cline 模型。

建议采用可移除 codec 方案：

1. ApiProvider 构造器新增可选 `codec: ApiCodec | None` 和 `codec_revision: str = ''`，缺省使用现有 protocol → codec 映射。自定义 codec 必须声明非空 revision；model_identity 仅在非空时加入该 revision，避免改变旧连接身份。不能将 codec Python 对象序列化进 durable selection；恢复时由注册连接工厂按 connection_revision 重新构造。
2. `api_context.fork_auxiliary()` 传递同一 codec、动态认证、request policy、revision；压缩不能退回默认 codec 或旧 token。
3. SSE 读取器通过通用可选 chat message accumulator 扩展点处理额外字段，默认实现行为不变；Cline 注入自己的归并器。不要复制整个网络取消/超时循环。
4. Cline codec 复用通用工具 call/result 配对校验，新增有界的 assistant 扩展字段保留。允许字段按实际模型契约声明，不能把任意 HTTP 响应/用户输入塞进历史。
5. reasoning_details 若为分片，按稳定 id/index 归并并保留签名/不透明字段；不能一律字符串拼接或数组追加。未知结构明确拒绝/标模型未兼容，不猜测。
6. 文本思考用于现有 UI 显示；签名、不透明 reasoning 元数据只进入受限协议历史，不当作可展示文字。
7. 工具调用必须继续匹配 call_id；reasoning 字段不能改变工具权限与任务授权。
8. 上下文账本、prepare/validate_view、durable 恢复与压缩都要通过往返测试。跨连接切换只导出通用对话/工具事实，不转发原模型的不透明元数据。

共享扩展点的拟定签名与逐处传递：

```python
class ChatStreamAccumulator(Protocol):
    def add_delta(self, delta: dict[str, Any]) -> None: ...
    def finish_message(self) -> dict[str, Any]: ...
```

- `api_stream.read_sse()` 新增可选 `chat_accumulator_factory`，每个响应创建一个 accumulator，绝不复用上次请求对象。
- 原文本、工具、思考字符串累积代码移入默认 accumulator，保证默认输出不变；原 SSE framing、deadline、取消、DONE 校验保留在 read_sse。
- `api_generation.generate()` 从通用 transport_options 取 factory 并传入 read_sse；factory 对象不进入日志或 durable 数据。
- Cline accumulator 扩展默认实现处理 reasoning_details。只支持本次设置的单 choice；若出现不支持的多个 choice，不把它们混成一条历史。
- `finish_message()` 只在有效终止后调用；再按原 choices/message/finish_reason 结构返回给 codec，usage 仍通过 on_event 交给 UsageCollector。
- `api_context.fork_auxiliary()` 同时传递 codec、codec_revision、request_context_headers；保留原 generation 取消绑定，辅助请求也校验账号 epoch。

`ApiCodec` 当前已存在；新增注入点是通用能力，删除 Cline 后保留不影响其他连接。若采用通用历史增强替代包内 codec，也必须保持同样验证和可删除性；两种路径只选一种，不叠加双重重写。

## 11. 认证与生成错误：精确处理表

| 情况 | 动作 | 不允许的行为 |
|---|---|---|
| 发送前 token 即将过期 | 单次协调刷新 | 每个线程各自刷新 |
| 完整 HTTP 401 | 同账号强制刷新一次；在同一逻辑请求预算内最多重发一次 | 无限登录循环、后台换账号 |
| 刷新 definitive rejection | reauth_required，要求用户重新登录 | 清空其他来源账号 |
| 403/促销无资格 | 模型/账号不可用说明，保留诊断 | 自动改用付费模型 |
| 429 | 读取 Retry-After，允许取消、有上限的等待；次数受 AttemptBudget 限制 | 无界 sleep 或跨任务绕过额度 |
| 500 包装上游 400 Invalid request | 按明确请求错误处理 | 当瞬时网络故障反复重试 |
| 已收到部分 SSE 后断流 | GenerationUnknownError，交给已有恢复机制 | 自动从头重新生成并拼接 |
| 明确发送前失败 | 按现有有限预算处理 | 另建第二套无限重试 |
| 用户取消 | 关闭 active response，拒绝迟到结果写历史 | 后台继续提交工具结果 |

401 对照测试必须断言重发请求模型、body、task id 不变，仅认证 token 版本更新；刷新消耗的时间受原 deadline 约束。若刷新失败，不覆盖原始失败的安全诊断。

包内 request_policy.observe_failure() 可以使用现有 GenerationRejectedError.status/retry_after 和 AttemptBudget；不能在包装 provider 外层重新调用整个 send() 来重试，否则会重复准入和历史写入。

## 12. 用量、思考显示和日志

usage.py 将 prompt_tokens/completion_tokens/cached_tokens/reasoning_tokens 映射到现有 ReportedUsage。SSE 中 usage 是累计快照，沿用 UsageCollector 的 latest 语义，不对多个帧求和。

reasoning_tokens 是 completion_tokens 的包含项，不额外加到 total。缺失用量显示未知，不能当作 0。

当前 ReportedUsage 无费用字段。首版只复用 token 展示，免费试用标签来自目录。若后续做费用显示，需要独立的通用费用结构、持久化兼容和 UI 设计；Cline usage.cost、market_cost、upstream_inference_cost、is_byok 各有不同含义，不能混用。

诊断白名单：provider、model、阶段、HTTP status、错误分类、请求 ID、重试次数、token 用量。禁止完整头、OAuth body、含 user_code 的 URL、Authorization、refresh token。不能直接 repr urllib Request/HTTPError 响应对象。

## 13. 前端设置页：事件与 DOM 改动

### types/domain.ts

新增通用 ProviderModel、ConnectionAuthentication、ConnectionAction 类型；ModelSource.models 使用 ProviderModel[]，避免现有仅 id/name 的类型投影丢失思考能力字段。

### model_settings.ts

在 renderSource(source) 的 actions 区域后、模型列表前插入通用认证容器；依据 source.authentication 渲染，不判断 source.id==='cline'。

- signed_out：显示登录。
- starting/exchanging：显示进度、禁止重复登录。
- awaiting_authorization：显示等待浏览器、用户 code、到期说明、重新打开、取消。
- ready：显示已连接和退出。
- expired/error/reauth_required：显示明确说明和重新登录。

操作后更新认证容器和 runtime.Models 对应来源；登录状态不是模型偏好草稿，不改变设置页保存按钮的 dirty 状态，也不覆盖用户尚未保存的模型勾选。

只在设置页打开且有活跃 attempt 时每 2 秒轮询认证状态；最多一个请求在途。每次请求记录页面 generation 和 attempt_id；关闭设置页停轮询，旧页面响应不更新新 DOM。关闭页面不自动取消登录；后端 attempt 仍可在有效期内完成，重新打开设置可恢复状态。

所有第三方字符串用 textContent，不用 innerHTML。user_code/URL 不保存到 localStorage。对登录状态区域用 role=status；失败使用明确错误文本，焦点返回原操作按钮。

复用现有 styles 中的 source actions、hint、button、spinner；不增加 Cline logo、专属 composer、固定宽度品牌前缀。仅为必要的状态布局添加通用样式。

## 14. 删除合作包：代码删除与状态清理分开验收

退出 Cline只清凭证；“清除 Cline 数据”还清连接缓存和来源偏好。清除前必须空闲，否则 409。若删除过程中某个本地文件失败，返回待清理项；不能宣称完全成功。

合作结束的顺序：

1. 停止该连接的新任务准入，等待/取消活动任务完成结算。
2. 执行退出并清除 Cline 数据：安全凭证、目录缓存、能力缓存、登录临时状态、该来源偏好。
3. 清除运行时句柄和连接状态快照；保留用户已有对话和运行历史。
4. 删除 codey/providers/cline/ 与唯一的 API_CONNECTIONS 注册项。
5. 删除 Cline 专属测试、测试夹具与文档；通用连接操作/codec 扩展保留。
6. 重启验证包不存在时所有其他功能仍工作，旧聊天显示原模型不可用，不发新请求。

安全存储目标清理不能只靠删除包目录。新发布版本如果直接移除包，必须先提供前一版本清理流程或通用清理清单；不能假设卸载 Python 文件会删除 Windows 凭证。

缺包测试不能同时删除包、改任意运行时逻辑把错误藏掉。仅移除注册与模拟模块不可导入，其他模块的正常路径应无需修改。导入断言：运行时未尝试导入 codey.providers.cline，包括子进程、审查器、压缩、headless 和历史展示。

## 15. 测试：输入、操作、断言

| 测试 | 输入/操作 | 必须断言 |
|---|---|---|
| 令牌前缀 | raw / already prefixed / empty | 仅一个前缀；空值拒绝；TokenStore 值不变 |
| OAuth 状态 | pending→slow_down→success | 尊重间隔、只开一次浏览器、存储一次 |
| OAuth 取消竞态 | cancel 后 register 返回成功 | 不保存凭证、不恢复 ready |
| 旧轮询 | attempt A 的响应在 B 开始后返回 | A 不能覆盖 B |
| 刷新并发 | 10 个请求同时读到过期 token | 一次刷新，所有请求使用同一新版本 |
| 账号切换 | 冻结 A 的 selection 后登录 B | 不发送旧请求，不使用 B 的 token |
| 刷新失败 | 网络故障 / invalid_grant | 两类状态不同，不丢正常记录 |
| 公共目录 | 未登录但目录有模型 | 来源可显示模型，连接不能 ready |
| 免费资格 | paid 模型价格为零但不在 free | 不可准入；不按名称推断 |
| 免费轮换 | 已选择模型被移出目录 | 新请求拒绝，历史保留，不切模型 |
| 能力未知 | free 条目无 context/efforts | 不虚构预算与等级 |
| 三态 UI | default→on→off→default | null/true/false/null 准确，按聊天隔离 |
| effort 参数 | 每个公开的 effort 选项 | 精确编译字段，非法等级准入失败 |
| assistant 往返 | 文本+工具调用+reasoning 元数据 | 编码恢复不丢必要字段，不扩大工具权限 |
| SSE 分片 | 文本、工具 args、签名分片交错 | 合并正确，未 DONE 不提交完整成功 |
| 停止 | 中断 SSE 后迟到终帧 | 无迟到历史提交、无重复工具执行 |
| 压缩 | 触发 auxiliary | 同连接、同账号 epoch、动态 token、同 codec |
| 用量 | 多个累计 usage 帧 | 最新快照，不重复计 token |
| 设置草稿 | 未保存勾选期间完成 OAuth | 不覆盖勾选，不误改 dirty revision |
| 退出竞态 | 最终 admission 与 logout 同时 | 只有一个获准；活动请求不能换账号 |
| 凭证泄漏 | 全链路使用 marker token | HTTP UI、SSE、日志、快照、UI state 中无 marker |
| 缺包 | 注册移除、import finder 禁止 Cline | Local/Zen/启动/历史/工具审批仍正常 |

自动化网络测试用本地模拟 WorkOS/Cline，不依赖真实用户登录。真实免费模型冒烟使用显式 live 工具，与日常单测分开；仅选择当时 free 集合中的模型。先验证非流式两轮，再 SSE，再工具调用，再开放思考档位。测试普通问答时使用中性提示词，不要求自称 Codey。

## 16. 实施提交顺序与完成标准

1. 通用连接操作路由与类型：无 Cline 注册，Local/Zen 行为不变，路由鉴权和草稿 UI 测试通过。
2. Cline 包的凭证、OAuth、目录：注册后仅开放登录与模型浏览；不自动对外启用全部模型。
3. 非流式生产运行链路：通过实际 Codey admission/ApiProvider 对话，而不是只运行独立 HTTP 脚本。
4. codec/SSE 与工具链路：所有现有协议测试通过，确认本机工具审批与迟到结果边界。
5. 思考等级：逐模型映射、逐选项验证；未验证模型保留 default 或隐藏控制，不发布假档位。
6. UI 收尾、退出清理与缺包测试；前端 typecheck/build 及相关 Python 测试通过。

每步只运行相关测试，出现共享边界变更后再扩展到 Local/Zen、恢复、压缩和工具审批回归。真实 OAuth 冒烟不作为每次 CI 的自动步骤。

交付必须同时具备：可登录、可恢复凭证、免费资格校验、两轮对话、SSE 取消、工具审批、真实能力驱动的思考 UI、无凭证泄漏、完整缺包运行。做到这些才叫完成 Cline 接入；独立脚本 HTTP 200 只是起点。
