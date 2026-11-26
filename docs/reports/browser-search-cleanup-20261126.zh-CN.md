# 浏览器搜索链路改造与实机验证（2026-11-26）

本轮将搜索成功判定、表单会话、网络守卫、后端降级、失败记录和资源清理接入同一条生产执行链。没有增加第二套浏览器运行时，没有用白屏后补按回车作为恢复协议，也没有放宽来源证据合同。

## 生产实现与事实归属

| 责任 | 唯一实现 |
|---|---|
| 最终搜索状态、结果规范化、去重和范围过滤 | `research/search_contracts.py` |
| 引擎首页、真实表单、结果区域配置 | `search_profiles.json` → `search_profiles.py` 的类型化配置 |
| 查询预算、后端选择、引擎降级和学术补充合并 | `WebSourceProvider.search()` |
| 表单提交、搜索标签页复用及页面观察 | `TaskBrowserSource`，使用现有 `BrowserSession` |
| 同次 DOM 观察的搜索状态分类 | `search_page.py` |
| 请求访问判定和同域 DNS 合并缓存 | `policies/network.py` |
| 守卫注册、事件诊断及主文档响应事实 | `BrowserNetwork`，是现有运行时的组成部分 |
| 历史搜索事实 | 版本 2 的 `SearchRecord` / `SearchAttempt` |

`BrowserSearchProvider` 保留独立入口，内部只组合现有运行时和统一编排。原搜索工作线程、同步搜索/提取实现、整页文字长度启发式及 `ConnectorAwareSearchProvider` 已删除。`last_search_errors` 等兼容属性从最终结果投影，不独立维护错误副本。任务绑定复用已存在的浏览器和搜索页，不丢弃热会话。

搜索结果区外的同样式链接不参与提取。导航栏、帮助链接、挑战页、未渲染壳页不再构成“搜索完成且没有结果”的依据。仅有明确无结果标记才返回 `empty`；候选全被范围过滤返回 `filtered_empty`；其他失败保留分类和尝试记录。

旧记录缺少状态时按 `unknown` 恢复，不补造“确认零结果”；当前版本缺字段和未知版本均拒绝。搜索失败同样进入账本；搜索摘要、诊断和截图不变成已阅读来源的证据。

## 实机确认的根因与首次失败

完整本机证据保存在 `.e2e-artifacts/search-cleanup-20261126/`，包括原始模型请求、响应、工具回执、DOM、网络诊断和各次测试日志。该目录不纳入提交。

| 首次证据 | 确认的问题 | 修复 |
|---|---|---|
| `form-attempt-1.json`、`home-controls.json`、`submit-hit-test.json` | Bing 首页提交 input 尺寸为零，可点击的是关联 label | 配置选择实际可见的 label 或 input；不伪造隐藏参数 |
| `form-attempt-2.json` | 原并发上限把工具命令容量用于网页请求，Bing CSS/JS 被 `route_capacity_exceeded` 拒绝 | 使用有界信号量施加背压，等待资源请求而非直接丢弃 |
| `form-attempt-3.json`、`result-submit-controls.json` | Bing 首页和结果页提交控件不同 | 同一协议支持两者，后续查询复用真实落地页 |
| `teardown/result.json`、`teardown/console.log` | 清理时 IPC 停止收路由回执，守卫排空与进程退出互相等待，0/3 完成 | 清理期间继续处理回执，完成后关闭确认；仅卸载自己的路由并排空 |
| `expanded-first.txt`、`expanded-second.txt` | 有导航文字的加载壳和 HTTP 登录页可能被当成正文 | 加载状态优先、浏览器/HTTP 共用认证页面识别 |
| `boundary-reproductions-first.txt` | 结果区外的同样式链接被接受；损坏 HTTP 分块异常绕过后端降级 | 限定结果容器提取；将 HTTP 协议异常纳入后端失败 |
| `final-regression-third.txt`、`deadline-reproduction-first.txt` | 子进程先返回截止回执，父进程下一时钟 tick 前可能将其当普通失败；单独重跑偶尔不复现 | 当命令使用调用方截止时间时，以子进程 `deadline` 回执传播 `DeadlineExceeded`；无调用方预算时保留普通局部失败 |

`baseline-browser.json` 的原始直达 Bing 查询在那次运行中成功。因此，本次没有把“Bing 必然拒绝裸 GET”当作已经证明的根因。`form-attempt-4.json` 也保留了外部服务的间歇性空壳现象，程序现在明确失败或降级，不伪报零结果。

后台窗口参数在统一启动处定义，仅对新启动的受控浏览器生效。附加到用户已有进程时不声称参数已经应用。用户页面不因清理空标签而被批量关闭。

## 验证过程与失败归因

- `final-regression-first.txt`：414 通过、19 失败。该轮与工作进程文件编辑重叠，子进程读到不可执行的中间版本；记录保留，不作为最终验证。
- `final-regression-second.txt`：442 通过、1 失败、4 个子测试通过。失败测试给未知版本记录漏填 `attempts`，先触发字段校验。修正夹具，不放宽生产解码。
- `architecture-final.txt`：146 通过、1 失败、474 个子测试通过。文件缩短后门禁要求同步降低上限；将 `browser_runtime.py` 上限由 1326 降为 1323，没有提高上限。复测 `architecture-final-second.txt`：147 通过、474 个子测试通过。
- `boundary-reproductions-first.txt`：两项新增边界测试均失败；对应生产修复后，`boundary-reproductions-second.txt` 两项通过。
- `final-regression-third.txt`：483 通过、5 失败、4 个子测试通过。除上述截止回执问题外，两项恢复测试仍返回旧列表，两项旧 HTTP 测试夹具不符合当前服务器/调用签名。正确迁移夹具，保持生产合同。
- `deadline-reproduction-first.txt` 保留“父时钟尚未到期但子进程已返回截止回执”的确定性失败。修复后 `failed-cases-correction-first.txt` 的 40 项测试全部通过。
- `final-regression-fourth.txt`：最终综合回归 **490 通过，4 个子测试通过**，耗时 347.93 秒。没有执行全量 pytest。

搜索合同迁移还覆盖了 45 个相关测试/工具模块。夹具数据由 `tests/support/search_fixture.py` 调用生产 `complete_search()` 构造，不保留生产入口兼容旧列表的分支。扩展测试 `fixture-migration-first.txt` 首次选错了嵌套测试路径，未执行测试；纠正运行路径后的 `fixture-migration-second.txt` 为 530 通过、13 失败。失败包括旧脚本越过当前工具快照直接调用叶子工具、过时控制器导入、旧知识发布/计划工具断言及过期 prompt golden。

这些问题均在测试侧按现有生产合同修正：正常入口使用已开放的 CodeMode；错误状态依据结构化状态而非 `ERROR:` 文案；旧控制器测试指向当前实现；golden 更新为现有工具说明。原始请求与拒绝回执保存在 `baseline-entry-raw.json`。复测结果：`baseline-entry-correction.txt` 3 通过；`fixture-failures-correction-first.txt` 69 通过、1 个遗留断言失败；修正该断言后 `fixture-failures-correction-second.txt` 1 通过。没有为这些旧脚本修改生产权限、来源或模型输出合同。

生产树检查使用仓库 CI 的完整 `codey` 范围，Ruff 检查整个仓库。最终 `ruff-delivery.txt`、`mypy-verified-final.txt`、`pyrefly-verified-final.txt`、`ty-verified-final.txt` 均通过；mypy 覆盖 830 个源文件。Pyrefly 保留仓库原有的 2 项抑制和 153 条警告，本轮没有新增抑制。类型精度门禁通过，另有 8 个关键测试/实机工具文件的严格 mypy 检查通过（`typing-focused-delivery.txt`）。修改文件的签名、显式动态类型、cast 和 ignore 审计结果保存在 `changed-file-type-audit.json`；没有用新增抑制通过检查。

最终架构/导入检查 `architecture-verified-final.txt`：147 通过、474 个子测试通过。运行时清单通过现有 `tools.publish_runtime` 发布，新增模块进入安装运行时清单；未提高文件行数上限。

## 最终实机结果

| 实测 | 结果 | 本机证据 |
|---|---|---|
| 冷会话 Bing 查询 | 成功，12.812 秒，落地 `cn.bing.com`，取得真实结果 | `browser-verified-final/browser.json` |
| 同页热会话第二个查询 | 成功，1.297 秒，由现有结果页真实表单提交 | 同上 |
| 本地 Gemma 模型经生产 headless 入口 | `web_search`、`open_url` 均成功，最终 `done` | `model-verified-final/provider.jsonl`、`events.jsonl`、`model-summary.json` |
| 真实浏览器并发资源请求后连续退出 | 3/3 成功，未处理 Future、残留任务及 TargetClosedError 均为 0 | `teardown-verified-final/result.json`、`console.log` |

模型端点为用户当时运行的 `http://127.0.0.1:5001/v1`，模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。实测没有将脚本动作冒充模型输出，也没有修改生产合同来接纳错误输出。

浏览器和模型两次最终实测均保存运行前后生产源码 SHA-256，均为 `cc0ba088b5c6e6388d82717a411c3c57a268ff53d80c20e21ac40db2ee6e6616`，`source_stable=true`。此前各次实测与失败仍独立保留，没有用最终成功覆盖首次失败。提交检查随后仅移除了 `search_urls.py` 末尾多余空行；`eof-normalization.json` 验证前后 AST 完全一致，未重复无语义变化的测试。

额外审计旧手工实验脚本的依赖时，`manual-typing-third.txt` 仍报告 6 个未修改的旧辅助模块共 67 项错误。本轮修改的五个实验脚本没有自身诊断；未为通过该额外审计改动无关辅助模块或添加忽略规则。

全树 pytest 仅作收集检查，没有运行全量 pytest。收集暴露三个既有导入错误：两个旧测试引用不存在的 `_new_log`，一个引用不存在的 `_tool_result`。`unrelated-collection-baseline.json` 验证这些符号在本轮之前的 HEAD 已不存在，相关模块未被本轮修改；没有扩展修复这些无关测试。

## 可选结构化搜索配置

默认不需要 API Key，使用浏览器搜索。用户可在可信进程环境中设置：

```powershell
$env:CODEY_SEARCH_SEARXNG_URL = "http://127.0.0.1:8080/search"
# 需要 Bearer 认证的服务再设置 CODEY_SEARCH_API_TOKEN。
```

配置后，通用检索优先请求 SearXNG JSON。有效命中或确认零结果不会启动浏览器；HTTP、形状、上游失败或协议异常进入浏览器降级。禁止意外 API 重定向，避免凭据投递到新主机。本地授权仅用于明确配置的 API 端点，不放开搜索结果或页面资源对内网的访问。

SearXNG 通道通过真实本地 HTTP 夹具验证，包含重定向拒绝、损坏分块和无效响应；没有把夹具验收冒充为连接真实 SearXNG 实例的实测。

## 边界

商业引擎仍可能改变 DOM 或要求验证；本轮保证失败分类、降级和证据链，不保证外部服务永久可用。应用 URL/DNS 策略仍不是浏览器级网络沙箱，保留原 DNS 重绑定限制和 TUN Fake-IP 兼容边界。系统 `getaddrinfo()` 不会因调用方等待超时而被取消，本轮没有宣称已改变操作系统解析行为。
