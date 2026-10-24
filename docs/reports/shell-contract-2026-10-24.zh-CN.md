# Shell 解释器收敛

原版基线为 25c7ceba。范围为共享 shell 合同、指导、授权和实际启动，保留原来
Windows CMD / POSIX sh 的默认方言，没有新增模型专用解析器或运行时。

## 实现

- `policies/command_line.py::ShellSpec` 统一解释器类型、绝对路径、启动方式和方言指导。
  `resolve_shell` 在新任务创建时解析，TaskSession 保存不可变对象。
- kernel 的审批请求、ActionSubject 兼容校验、ShellApprovalRequest 和原 command
  artifact 传递同一合同；恢复从已验证 artifact 读取，不用当前 COMSPEC 补齐。
- ShellExecutionTicket 携带合同；原 cancellation.start_process 显式启动解释器，
  继续使用已有 Stop gate、Job Object、超时、捕获和结算流程。没有 shell=True。
- CMD `/d /s /c` 使用原始 command tail，避免把脚本内引号当作 CRT argv 重编码。
  `/d` 关闭 AutoRun；POSIX 用 `["/bin/sh", "-c", command]`。
- 精确授权绑定 artifact 中的合同，不再从当前环境推测 executor。旧授权不自动升级，
  缺少合同的旧待批准 artifact 不成为可执行请求；需要重新发起审批。
- 模型提示区分允许验证的 run 直接 argv 执行与经批准的 shell 脚本；Windows 提供
  双引号、%NAME%、显式解释器及工具替代管道的指导。path 是项目相对目录，省略则
  使用根目录；不替模型修正非法路径。

## 参考与取舍

- Codex `codex-rs/core/src/shell.rs::derive_exec_args`：按解释器生成启动方式，
  而非仅靠平台提示。Codey 保留已支持的 CMD/sh，不复制全部发现和回退链。
- OpenCode `packages/core/src/shell/select.ts::args` 与
  `packages/core/src/tool/plugin/shell.ts::prepare`：分析和启动面向同一 invocation。
  Codey 把已有兼容检查接到同一合同，没有引入两套 shell scanner。
- DeepSeek Harness `packages/shell/shell/src/index.ts::ShellExecutor` 及
  `packages/shell/pwsh-local/src/index.ts::argv`：明确方言，底层过程管理保持共同边界。
  Codey 继续用原 shell_service/cancellation，没有复制 Bash/Pwsh 工具家族。

## 验证与证据

最终相关 15 文件选择：185 passed、1 skipped、96 subtests passed；随后新增的批处理
引号和超时测试及最终指导选择：53 passed、8 subtests passed，其中 shell 合同为
22 项。另有直接 run/verification 选择 98 passed、1 skipped、93 subtests passed。
全量 pytest 和性能对照不属于本批准入。选择有重叠，不累加成全量测试总数。

Ruff、全树 mypy（513 源码文件）、Pyrefly、ty 通过，目标 Python 3.11。Pyrefly
保留原有 29 抑制项与 80 未展示警告，不声称零警告。

确定性原版/候选实机对照：同一票据创建后把 COMSPEC 改成不存在的路径。原版
通过 shell=True 重新读取环境，spawn_error；候选仍使用票据解释器，输出
SHELL_PINNED，exit_code=0。脚本在 tests/manual/shell_interpreter_drift.py，材料在
artifacts/shell-contract-execution-20261024/{baseline,candidate}.json。

本地模型对照使用生产 HTTP、真实 worker、原生调用、审批、结果回执与续跑，模型
自行生成命令。审批器仅批准有限集合中的无害探针命令，没有替换模型或工具循环。
每次原始请求、响应、审批 artifact 引用和结果均保留。

最终基线 `shell-contract-baseline-20261024-02` 与候选 `shell-contract-live-20261024-06`
均为 4/4：含空格文件读取、环境变量、退出码 7 的脚本、拒绝后续跑。候选的合法
原生 shell 调用与结果实际配对，没有把普通文本中的模型工具标记执行。
同一脚本 hash，实际请求参数相同：Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M、
temperature=0.3、max_tokens=1024；未发 seed 或 thinking 控制参数。不能把 GateTarget
的声明默认值当作实际请求值。原运行器曾在 AppContext 建立后写 thinking=false，
实际绑定未采纳它；该无效配置写入已删除，没有修改生产 provider。

比较器校验完整四场景、原始请求参数、脚本 hash、生产 diff hash，regressions 为空，
COMSPEC 漂移从失败变成功，accepted=true。比较结果为
`artifacts/shell-contract-comparison-20261024.json`。两份实机目录各保存当次
`evaluation_harness.py`；候选还保存 `production.patch`，父提交号不是最终源码的全部。

## 未通过的中间结果

- live-01 的三个探针任务写有“不修改/只读”，现有入口据此否决 shell；fixture 也
  未关联项目。这是运行器设定不符合场景，没有通过放宽生产授权来解决。
- live-02/03 保留了 fixture 放在项目外和模型提交绝对 path 的失败；非法 path
  始终被拒绝。模型已能生成正确 CMD 命令，但完整流程不算通过。
- live-04/05 的部分响应把裸模型工具标记放在普通文本中，没有合法调用。相对
  原版对照按新增回归处理，没有扫描 Gemma 标记后执行，也未声称 shell 已成功。
  撤掉新增 schema 字段说明，并把 path 的带引号默认值示例改为语义说明、建议省略
  默认参数；isolation-guidance 定向实机恢复为合法 shell + done。单次隔离不证明
  某段用词具有普遍因果关系，仍须完整题集验收。
- 第 05 次完整对照在完成前三场景后中止，不计作完整通过；其中最初场景失败保留。

## 证明边界

这能检验解释器身份、CMD 引号、实际退出码、审批和恢复。不能据四个模型场景宣称
普遍降低了命令错误率。POSIX 参数与指导有确定性检查，本机没有可用 Linux/WSL，
没有 Linux 实机成功宣称。PowerShell 仍需显式调用，没有增加默认 PowerShell 方言。
