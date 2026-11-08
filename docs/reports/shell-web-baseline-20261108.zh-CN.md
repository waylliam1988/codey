# Shell 与联网：修改前双模型诊断，2026-11-08

本轮只新增诊断脚本、脚本测试和报告，没有修改 `codey/` 或前端业务代码。目的是确认故障发生在哪个边界，缩小后续修改范围。

脚本：[shell_web_diagnostic_gate.py](../../tools/shell_web_diagnostic_gate.py)。
机器可读结论：[shell-web-baseline-20261108.json](shell-web-baseline-20261108.json)。
完整本地证据：`.e2e-artifacts/shell-web-verified-20261108/`。

## 测试方式与边界

- Solar Mini4：使用现有 Cline 登录、目录验证和生产连接工厂，模型 `cline-free/solar-mini4`。
- Gemma4：连接 `http://127.0.0.1:5001/v1`，实际加载 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，保留生产已有的 KoboldCpp/Gemma 兼容协议选择。
- 执行器检查调用真实 Shell ticket；审批用例经过真实 HTTP 入口、任务 worker、审批和继续执行；网页用例经过真实 Headless 入口与实际来源服务，没有替换搜索或网页返回值。
- 受控回放中的 candidate 是测试输入：分别补充退出状态、明确截断恢复信息、使用生产 HTML 提取器取得正文。它们没有部署到产品，也不能证明修改后的完整产品已通过验收。
- 首轮每个模型每个用例运行一次。Shell 方言用例明确给了命令指导，并只执行严格允许的输出标记命令。不能据此作模型排名或宣称所有复杂命令都兼容。

完整矩阵包括 11 项执行环境/策略/提取检查、2 项模型预检、16 项模型回放、4 项真实审批、4 项真实网页入口，共 37 项，无缺失或重复槽位。

22 项通过、15 项未满足所定义目标。这个数字混合了环境观测、受控实验与产品能力缺口，**不是模型成功率**。基线返回非零是预期诊断结果，不应通过放宽断言或修改业务代码来刷绿。

## 主要观测

| 检查 | Solar Mini4 | 本地 Gemma4 |
|---|---|---|
| 缺少退出码的当前回执 | 诚实回答未知 | 本次错误推断为退出码 0 |
| 补充退出码和 stderr 的候选回执 | 正确识别退出码 2、命令错误 | 正确识别退出码 2、命令错误 |
| 明确标记采集截断且无法完整读回 | 正确识别 | 正确识别 |
| 只看巨大 favicon 前 20KB | 无法确定标题，未编造 | 无法确定标题，未编造 |
| 先提取完整夹具标题和正文再展示 | 正确识别唯一标题和 API gateway 信息 | 正确识别唯一标题和 API gateway 信息 |
| 有指导的 CMD / PowerShell 标记命令 | 两项均通过 | 两项均通过 |
| 真实审批后的初次 Shell 回执 | 缺少退出码 | 缺少退出码 |
| 普通聊天未申请 `web.read` | 无网页工具，转向 Shell 后被 Headless 默认拒绝 | 同样受阻 |
| 显式申请 `web.read`，打开指定首页 | 被首页筛选规则拦截 | 被同一规则拦截 |

真实网页输入为 `https://api.lihe.chat/`。两个模型在显式开启联网后都实际调用了 `open_url`，返回：

```text
SKIPPED: low_value_landing_page_url. Choose a specific article, document, or readable source page.
```

因此，**只给普通聊天增加联网授权仍不够**。用户明确要求分析的首页，需要与 Research 自动挑选证据候选的筛选规则区分。此次真实入口没有成功读取目标首页，不能声称验证了该站的完整浏览流程，也没有验证福州美食搜索质量。

## PowerShell 与退出状态

通过真实执行器验证：CMD 中子程序退出 23，观察到 23；PowerShell 7 中直接运行该子程序，观察到 1；后面再成功输出文字，观察到 0；明确 `exit $LASTEXITCODE` 时，观察到 23。

这符合 Shell 的执行语义，是执行契约需要处理的差异。调用者不能仅凭 PowerShell 的退出码 1，还原任意子进程的原始退出码。

真实审批用例的命令含 `sys.exit(7)`：CMD 记录的退出码为 7，PowerShell 记录为 1。两模型都完成了继续执行，但在 PowerShell 用例中，最终回答仍将退出码说成 7，与记录不符。

初次工具回执没有退出码，`ok:true` 不能提供进程成功的证据。Solar 后续通过额外读取回执取得状态；这也说明需要分别度量“初次可见”和“后续读回”，不能让后续读取掩盖初次结果丢信息的问题。

GBK 中文字节输出在 CMD 和 PowerShell 两条受测路径都未正确显示。PowerShell 的 UTF-8 前缀不能保证任意原生程序都输出 UTF-8；部分文字可能已经在 Shell 转换阶段损失，需要同时检查原生进程调用方式和宿主解码方式。

## 测试脚本自身的修正记录

探索运行发现，旧审批 gate 假定存在原生工具窗口，不能直接作为兼容协议的共同验收标准。新脚本复用了生产入口和审批过程，取消了这个测试专用假设，并继续验证实际执行和模型收到的回执。

真实网页用例初次分析把 `stopped` / `max turns reached` 当作模型 JSON 解析，错误标成通用异常。已修正分析器，并从已保存的事件和请求重新分析，**没有重复联网或模型调用，也没有覆盖原始报告**：

- 原始报告：`summary.json`。
- 重新分析：`summary.reanalyzed.json`。
- 版本与源码记录：`manifest.json`，持久化结论同时记录运行脚本与最终分析脚本的 SHA-256。

脚本保护性测试共 18 项通过，Ruff 检查通过，覆盖缺失矩阵、重复槽位、布尔/整数混淆、初次与后续回执区分、兼容协议结果识别以及运行状态误归因。

## 据此建议的最小修改顺序

1. 统一初次工具回执，显示实际退出状态、stderr、截断与可恢复范围，保持审批重建和历史恢复的信息一致；补充最终回答与执行事实的一致性验证。
2. 打通普通聊天的现有联网能力，并为用户明确指定的 URL 增加首页读取路径，复用现有正文提取组件。此次已有证据支持先修这两个边界，再判断是否需要重做搜索提供方。
3. Windows 默认优先 PowerShell 7，同时明确原生子进程和脚本各自的退出语义，解决编码路径。不能只改默认 Shell。
4. 对同一矩阵复测，再补真实搜索词相关性、动态页面、跨轮恢复和多次重复。当前没有证据支持立即进行大规模架构重写，也没有完成修改后可靠性评估。

## 复现

使用一个尚不存在的输出目录：

```powershell
python tools/shell_web_diagnostic_gate.py --run-dir .e2e-artifacts/shell-web-next --only offline replay e2e web --timeout 180
```

默认只运行 `offline replay e2e`，真实网页测试通过 `--only web` 显式选择。可使用 `--models solar` 或 `--models gemma`、`--shells cmd pwsh` 和 `--repeat 3` 缩小或重复矩阵。不同模型之间不会自动替换或故障转移。

```powershell
python -m pytest -q tests/test_shell_web_diagnostic_gate.py
python -m ruff check tools/shell_web_diagnostic_gate.py tests/test_shell_web_diagnostic_gate.py
python tools/shell_web_diagnostic_gate.py --reanalyze .e2e-artifacts/shell-web-verified-20261108
```

重新分析只检查已保存证据；其退出码 0 表示矩阵完整，报告中的 `ok:false` 仍然保留基线能力缺口。
