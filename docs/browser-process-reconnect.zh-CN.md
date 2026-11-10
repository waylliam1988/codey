# Codey 重启后接回仍存活的浏览器页面

本轮实现的是：Codey 进程退出后，原 CDP Chromium、上下文和标签仍在，新进程恢复同一任务时接回原页。页面里的草稿、脚本内存和登录状态继续由原浏览器保存。Codey 不导出或重建这些网页状态。

## 单一事实来源

浏览器工具执行后，`ExecutionDelegate` 把资源描述放进该次 `ToolResult.audit.browser_resource`，随现有工具回执一起提交。没有新增浏览器状态文件、数据库表、恢复工具或执行循环。

描述包含：本机 CDP 端口、浏览器 WebSocket UUID、当前 target ID，以及已登记页面的 target ID、context ID、任务所有权。它不包含 Cookie、登录令牌、DOM、元素句柄或导航响应的旧副本。

- `BrowserSearchProvider` 仍是页面资源和销毁权限的所有者。
- `BrowserSession` 仍负责页面交互和当前进程内的元素引用。
- `browser_binding.py` 只编码资源身份和连接指定 CDP 目标，不调度工具、不运行模型。
- 账本仍只投影原有来源读取回执。接管、snapshot、点击和元数据恢复都不会新增来源证据。

## 恢复顺序

1. 现有内核从正式回执恢复 `ToolResult`、证据投影和执行结果缓存。
2. `KernelContext.restore_facts()` 从这些结果取出最后一份浏览器资源描述；此时不连接浏览器。
3. 第一次实际浏览器调用通过现有浏览器 worker 连接已记录的端口，核对浏览器 UUID、target ID 和 context ID。
4. 匹配成功后，恢复当前页和已登记页面的所有权。不会重新导航，因此未保存草稿仍在。
5. 新 `BrowserSession` 使用新的引用作用域，旧 ref 返回 `stale_ref`；模型必须重新 snapshot。

`restore_snapshot()` 仍只恢复冻结的 ToolSpec、catalog 和 executor 身份。资源重连使用同一内核的回执恢复与资源层，二者没有混成另一套恢复运行时。

已提交的点击、填写等继续走原有工具重放规则，读取旧回执不会再次执行它们。动作已经发生、回执尚未提交时发生崩溃，仍属于原有的结果不确定窗口，不会为了恢复而盲目重试。

## 失败与清理

端口被另一个浏览器占用，或者原标签已经关闭时，恢复明确失败。即使 URL 相同，也不会按 URL 或标签序号选替代页；该恢复会话中的 navigate/new-tab 同样不能绕过身份检查。需要用户重新开始任务并明确导航。

任务结束时，只关闭记录为任务创建、且身份仍可确认的页面，保留借用的用户页。恢复后即使没有继续执行浏览器动作，正常资源清理也会尝试接回已登记资源。如果原浏览器或活动标签已无法确认，不冒险选择替代页进行清理。

重新接管的页面没有当前进程的导航回执。当前页读取从存活文档的 Navigation Timing 观察可用的 HTTP 状态，并观察文档类型，再使用与普通浏览器获取相同的页面验收函数。已验证恢复后的 HTTP 403、503 和挑战页不会进入来源账本。共用验收函数移到 `browser_page.py`，没有保留第二份实现。

## 剩余边界

- 需要可访问的本机 CDP Chromium，以及原浏览器、上下文和标签仍存活。没有 CDP 的进程内 headless 会话不在接管范围。
- 本轮不实现优雅重启时挂起任务、为任务强制保活浏览器或新的恢复 UI。正常完成或取消任务仍按现有生命周期清理页面。
- 没有资源描述的旧版本回执无法凭空找回原页。
- 不恢复已关闭页面、DOM ref 或任意网站的完整状态，也不保证网站自己的登录会话没有过期。
- 只能恢复已经提交的页面所有权描述；崩溃前尚未记入回执的新页面无法可靠认领。
- 恢复后的网页可能已经变化。新证据仍须执行 `browser_read`，不能把旧回执当作当前网页的新观察。

## 验证记录

生产回归运行了浏览器执行、失败页面、冻结快照、跨进程接管、真实 CDP 生命周期、合同、上传下载、CodeMode、来源获取及回执恢复相关测试，没有运行全量 pytest。

- 相关生产回归：148 项通过。
- 浏览器获取与内容重试的补充回归：25 项通过。
- 跨进程接管专项：15 项通过，包含草稿、Cookie、已提交点击不重放、旧 ref、原标签消失、同端口浏览器替换、仅清理、403/503/挑战页。
- Ruff 通过；修改的 7 个生产文件和 2 个测试文件通过严格 mypy。
- 最终架构测试 97 项通过、478 个子测试通过，尺寸检查 1 项失败。本轮 `browser_search.py` 从原来的 1066 行降到 998 行；最终尺寸检查只剩 HEAD 已有的问题：`durable/model.py` 1014 行超过登记值 1013，`toolchain/runtime.py` 的登记上限未下调，以及 `toolchain/tool_spec.py` 1199 行超过 1000。没有修改这些无关文件或放宽检查。

实机使用 KoboldCpp Gemma4 + 真实 Chromium，复用生产入口、授权、CodeMode、执行与回执。原始请求、响应、工具事件、正式存储和 summary 保存在工作区的以下目录，不覆盖首次结果：

- `.e2e-artifacts/browser-reconnect-first/`：完整矩阵首次 6/7。Chat basic 因模型生成多余转义、把等待语句吞入注释，缺少必需的 `browser_wait`，最终 `no_progress`；其他六项通过。
- `.e2e-artifacts/browser-reconnect-basic-rerun/`：只重跑上述 basic，1/1 通过。
- `.e2e-artifacts/browser-reconnect-final/`：最终版本完整矩阵 5/7。structured 的模型答案抄错 URL；Hybrid stale_edit 在第六次提供方请求遇到 180 秒截止时间，返回 `GenerationUnknownError` / `submission_uncertain`，此前浏览器动作和 stale edit 拒绝均有正式回执。未发现由此新增的浏览器生产确定性问题。
- `.e2e-artifacts/browser-reconnect-structured-rerun/`：只重跑 structured，1/1 通过。
- `.e2e-artifacts/browser-reconnect-hybrid-rerun/`：只重跑 Hybrid stale_edit，将单请求超时从 180 秒调为 300 秒，1/1 通过；整场 79.313 秒。没有改变工具合同或生产实现。

最终矩阵的 structured 曾出现工具调用全部成功、最终答案 URL 被模型从连字符抄成下划线的失败。原始工具回执保留正确 URL，不能通过放宽合同或修改生产结果来掩盖这种模型输出错误。

| 场景 | 首次完整矩阵 | 最终完整矩阵 | 单独重跑 |
| --- | --- | --- | --- |
| Chat basic | 失败：模型漏掉等待调用 | 通过 | 通过 |
| Chat structured | 通过 | 失败：模型抄错 URL | 通过 |
| 重复读取 + 冻结快照序列化恢复 | 通过 | 通过 | 未重跑 |
| Hybrid stale_edit | 通过 | 失败：提供方超时 | 通过，超时上限 300 秒 |
| 上传 | 通过 | 通过 | 未重跑 |
| 下载 | 通过 | 通过 | 未重跑 |
| strict Research | 通过 | 通过 | 未重跑 |

完整实机脚本里的 stale_edit 是文件回执过期场景；浏览器 stale-ref 和真正的两个 Codey 进程接管另由上述真实 CDP 专项覆盖，不把冻结合同往返测试冒充页面状态恢复。
