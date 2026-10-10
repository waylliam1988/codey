# Codey 与安装版 OpenCode 实机稳定性比较

2026-10-10。完整原场景 40 次运行、20 对比较：**Codey 18/20，OpenCode 14/20**。
本轮新增原生 OpenCode 比较脚本和观察器，未修改生产代码。
全量 pytest：**8311 passed、7 skipped、1503 subtests passed，895.06 秒**。

这次运行的是安装版 OpenCode 的原生 agent、工具、重试和上下文处理，
不是此前的压缩函数回放。结果不支持“Codey 全面领先”或“已稳定达到 20/20”。

## 实际运行身份与条件

- OpenCode 安装目录：`C:/Users/Administrator/AppData/Local/Programs/@opencode-aidesktop`。
  安装包与原生健康检查均报告 **1.18.35**；内置 Node **24.15.0**、Electron **42.3.3**。
- exe SHA-256：`e1e09d8ef8cf360318cf40ef4d7f91a25155f0707530523f43ce49730990c7dd`。
  app.asar SHA-256：`aaa772c154f6ca3a604052ae4a15b63303883cb9a6ee0573be0bc9acedb31a39`。
- 参考源码为 `E:/codey/reference-projects/opencode`，提交
  `ecc4916b5a9608c30e6dd58a67f2137b594407ca`。
  **安装构建对应的源码提交未知**，不把参考 checkout 当成实际运行身份。
- Codey 起始提交 `ded65fac65b45e493168168e996e701669f5a9fc`；生产字节哈希
  `f180758c226fb70fb393848c22b7c88fbf4f257f0513cca56eb1e24938e979a7`。
  与上次 Pi 比较的生产代码相同；之前的 20/20 是那轮表现，本轮暴露了已有不稳定性。
- KoboldCpp **1.117.1**，同一已加载模型
  `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
  全部模型请求经回环地址观察代理，不使用远程模型或真实 API key。
- 原场景种子 51/52，温度 0，窗口 32,768，输出 2,048；每 arm 180 秒、
  最多 24 次生成，Codey 的真实 Local review 和 OpenCode 的辅助生成都计入预算。
  原始请求已核对采样参数，代理没有重写请求；原生流式/非流式差异保留。
- Codey keep 为 12,000；OpenCode 保留原生 auto/prune 策略，未替换成 Codey 的策略。
  执行先后交替，计时期间产品及比较脚本冻结，不并行运行重检查。

观察器通过 Electron 的原生 parent-port 启停安装包 sidecar，再调用本地 session API。
没有另写 OpenCode agent 循环，也没有改它的系统提示或补一个 Codey reviewer。
每个 arm 使用独立 fixture、session 和 XDG 状态目录；没有改用户的应用配置、账户或安装包。
模型地址限制为回环 HTTP、使用本地占位 key；未安装依赖或启用付费服务。
这不是操作系统网络沙箱，不宣称阻断了 native 工具可能发起的一切网络访问。

## 完整原场景结果

| 场景 | Codey | OpenCode |
| --- | --- | --- |
| 普通修复 | 1/2 | 1/2 |
| 先测再改 | 1/2 | 2/2 |
| 无需修改 | 2/2 | 2/2 |
| 只读且修复被禁止 | 2/2 | 0/2 |
| 多文件修复 | 2/2 | 1/2 |
| 长输出指定中段恢复 | 2/2 | 0/2 |
| 503 恢复 | 2/2 | 2/2 |
| 停止运行中的测试 | 2/2 | 2/2 |
| 编辑中断后续接 | 2/2 | 2/2 |
| 停止后改变需求 | 2/2 | 2/2 |
| **完整场景验收** | **18/20** | **14/20** |

12 对双方都通过，6 对仅 Codey 通过，2 对仅 OpenCode 通过；原轮次没有缺失或被排除的配对。
场景验收包含正确报告阻塞和停止，不表示每个场景都应该完成修改。
外部 oracle 检查补丁、隐藏输入及文件范围；实际测试 journal 检查本轮代码身份和新鲜度。
外部测试不能代替 agent 自己运行的测试。

双方都通过的 12 对中，完整生命周期累计耗时为 Codey **436.55 秒**、OpenCode **710.34 秒**，
本轮 Codey 少 **38.5%**；10 对更快，2 对更慢。
慢的两对是种子 51 的 503 恢复（+8.73%）和种子 52 的先测再改（+6.64%）。
失败或超时的样本不参与这项速度比例。
时间包含启动、收尾和 observer 开销，不能直接等同于常驻桌面界面的响应速度。

Codey 原 20 次的服务端 usage 完整；OpenCode 原 20 次均有部分流式 usage 缺失。
**本轮无法公平比较总 token**；未知不是零，也不使用已知部分总和代替完整消耗。
此前压缩函数回放的 token 优势不能直接套到这次原生 agent 比较。

## 两个 Codey 失败的真实根因

### 种子 51：先测再改后，最终补丁正确，但收尾未完成

实际记录证明它先跑了原始失败测试。原始 Windows CRLF 文件哈希是
`b0e7f84adbbca5ee67e7ef17008979718f4e854443d5d83baa5d7d70f421e49f`；
不能用 LF-only fixture 字符串的哈希推断提前修改。过程中对这个问题的早期判断已更正。

随后模型修改、运行失败测试，再修改到正确实现；最后连续三次提交已经失效的 SEARCH，
均返回 exact replacement 未应用，触发 `no_progress`。
外部 oracle 证明最终补丁正确，但 agent 最后一次登记的验证对应较早的失败源码，
**最终代码没有新通过回执**，也没有正常完成。这个失败不能通过外部测试补成成功。

源码中的停滞处理见 [task_loop.py](../codey/operations/task_loop.py:716)；
review 的正常候选入口受 `done` 条件约束，见 [coordinator.py](../codey/reviews/coordinator.py:90)。
目前缺少停滞后的明确候选收尾路径：对已有修改及已授权的待验证命令进行一次有界真实验证，
再用当前观察、review 和完成门决定结果，而不是只等模型主动结束编辑循环。
这项建议尚未实施，必须保留取消、审批、失败结果及预算约束，不能直接把停滞改成完成。

### 种子 52：语义反例被发现，但修复停在命令审批

错误实现把标点替换为空格，可见测试通过，Local reviewer 也批准。
真实行为 worker 返回 `property_mismatch`，受管结果为 `out_0001_73d17046a9fe`；
完成门阻止了误完成。随后有界修复阶段请求：

```text
python3 -c "from app import normalize_name; print(normalize_name('Qr7t'))"
```

该命令进入 shell 审批，headless 按默认策略拒绝并停止，最终原因是 `approval`。
处理入口见 [headless_runner.py](../codey/app/headless_runner.py:107)。
这不是“行为检查漏掉了错误”，也不是应取消默认拒绝策略的理由。
需要加强反例到修复的操作路径：优先读取已有实际反例、修改候选、运行已授权验证、重做行为检查与 review，
减少依赖模型生成临时脚本。其收益仍需 TDD 和新的实机回放验证。

原轮次 Codey **0 次误完成**，但安全停止不等于成功完成任务。
这两个缺口说明完成事实可信与任务完成率需要分别改进。

## OpenCode 失败与可以借鉴的部分

- 普通修复种子 51：多次 `oldString` 精确匹配失败，包含复制进搜索串的行号，最终 180 秒超时。
- 只读场景两次修改了禁止修改的 `app.py`；种子 52 还报告成功，记为一次误完成。
  OpenCode 配置允许原生 bash/edit，Codey 保留自己的任务权限准入。
  这里比较各自能否落实用户的只读要求，不声称使用了相同的底层权限实现。
- 多文件种子 52：真实测试失败，继续出现精确编辑失败，耗尽生成预算。
  记录中的 429 是 observer 的预算拒绝，不是供应商限流。
- 长输出两次按页读取保存结果，读到约第 3,000 行，未到指定中段，均超时；
  Codey 两次使用保存结果的关键词读取通过。

OpenCode 的先测再改 **2/2**、503 恢复 **2/2**、停止/续接/需求更改均 **2/2**，这些能力不能忽略。
Codey 值得借鉴它能把任务继续推进到验证的工作流，同时保留自身的权限、真实反例和完成证据。
参考源码展示了原生 session、重试及工具状态机制，但安装构建对应提交未知，
不能把一次安装版行为直接归到参考 checkout 的某行实现。

## 补充变体与所有失败记录

变体把目标改为 `labels.py / squash_label`，并增加已正确实现的只读验证。
这些任务和种子此前已用于 Pi 比较，不是独立未见样本。

本轮补充计划 8 次运行，实际启动 5 次。
种子 61 的两对均完成：**Codey 2/2、OpenCode 2/2**。
修复这对 OpenCode 更快（39.75 秒对 46.82 秒）；只读验证 Codey 更快（26.63 秒对 36.63 秒）。
种子 62 的 Codey 生成在 179.996 秒超时，后端持续 busy，随后端口拒绝连接（WinError 10061）。
这一观察保留并标注 `backend_isolation_error`，没有对应的 OpenCode arm；三次计划运行未启动。
**补充轮次未完成，不报告 4/4，也不把未配对的超时算作另一方胜出。**

校准阶段三个早期 pilot 分别暴露启动 ready/health 接线和异步 prompt 准入误判；
它们属于 observer 失败，排除出 agent 胜负，修正都有先红再绿的测试。
第四个 pilot 的原生观察有效：Codey 通过，OpenCode 把标点替换为空格后误报完成。
这个有效失败仍保留；后来正式补充的同一任务/种子通过，说明单次结果不能被当成稳定能力保证。

## TDD、检查与观察边界

新增 `test_opencode_installed_agent_observation_and_isolation.py`，**16 项用例**：
回环配置、采样先于准入、实际构建身份、工具状态去重、实际终态、ready/health、
异步准入不等于完成、原生 `filePath` 保存输出读取和正确配对名称。
初次缺模块的导入失败不算测红；增加接口骨架后，行为红测试用于实现和修正观察器。
复用现有 pytest、任务 fixture、proxy、独立 oracle 和配对汇总，没有新增第二套 agent 循环或测试框架。

最终定向回归 **70 passed，7.10 秒**；机器契约 **928 passed、14 subtests，190.92 秒**。
Ruff、mypy Windows/Linux（406 个生产文件）、Pyrefly `check codey`（0 errors）、compileall、diff 通过。
初次误用无范围 Pyrefly 检查的日志保留，不把其仓库外围结果冒充生产检查结果，也没有调整抑制设置。

机器契约首次 **1 failed、927 passed、14 subtests，219.74 秒**，定位到上次已提交报告的两条
说明包含禁用的旧环境变量前缀；只改说明，不改原始实验指标、哈希或白名单。
同一命名测试修绿（4 passed、14 subtests），之后完整契约通过。
最后唯一一次全量 pytest **8311 passed、7 skipped、1503 subtests，895.06 秒**，之后才写本报告和 test report。

观察限制：

- 同一模型/种子仍不能消除提示、工具 schema、文件路径、历史和生成非确定性的影响。
- OpenCode API 轮询可能在工具已经完成后才首次看到它；不能可靠排名单次 mutation、
  同工作区无效重复等依赖精确开始快照的指标。文件范围及测试新鲜度采用独立源码 journal。
- 原生 bash 的 `completed/ok` 表示交付成功，不等于测试退出码为零；不直接排名两边的 tool-ok 数。
- `stop_seconds` 包含 sidecar 收尾。两边停止场景都没有残留 fixture 子进程，
  但这个指标不能证明谁的界面取消反馈更快。
- 这是小型 Python 任务、两个复用种子的回放，不证明大仓库、多语言、所有未知任务全面领先。
  本轮也不提供完整原生 token 排名或常驻桌面延迟排名。

## 重放与证据

[比较入口](../tests/manual/codey_vs_opencode_agent_stability_ab.py)、
[安装版观察器](../tests/manual/opencode_installed_agent_worker.py)、
[便携结果与校验信息](reports/codey-vs-opencode-agent-stability-2026-10-10.json)。

```powershell
python tests/manual/codey_vs_opencode_agent_stability_ab.py `
  --run-dir artifacts/codey-vs-opencode-agent-new-round `
  --opencode-install 'C:/Users/Administrator/AppData/Local/Programs/@opencode-aidesktop' `
  --seeds 51,52 `
  --cases normalize-name,test-first,no-op,scope-blocked,multi-file,long-output,http-503,cancel-running-test,resume-after-edit,correction-after-stop `
  --timeout 180 --request-limit 24 --max-tokens 2048 --window 32768 --keep 12000
```

目录必须新建，后端必须已加载同一模型并空闲。场景失败使命令非零退出；
observer 或隔离失败会停止后续运行，不能假装完成全矩阵。
原始记录位于 `artifacts/codey-vs-opencode-agent-20261010-original`、
`artifacts/codey-vs-opencode-agent-20261010-holdout`；校准、TDD 红日志及检查也保留在 artifacts。
版本仍为 0.5.11；不创建 tag 或 release。
