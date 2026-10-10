# Codey、安装版 OpenCode 与 Pi 的实机比较记录

本文汇总 2026-10-10 已完成的本地模型比较、候选恢复回放和本轮只读停滞收尾验证。比较使用 KoboldCpp 1.117.1 与
`Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，温度 0，窗口 32768，输出上限 2048，保留上下文 12000，
每臂最多 24 次生成、180 秒。所有模型请求均为本地回环服务。

## 运行身份

| 参与者 | 实际运行身份 |
| --- | --- |
| Codey | 提交 `bada8cd91536086051704d82ea08a4e2d98b8f55` 的生产源码；比较产物记录源码哈希 `bfe81c03dde12aed19b5ffb6e3cee4f30899acfe0b4e6785461cb8f0d4f08529` |
| OpenCode | 安装目录 `C:/Users/Administrator/AppData/Local/Programs/@opencode-aidesktop`，版本 1.18.35；可执行文件 SHA-256 `e1e09d8ef8cf360318cf40ef4d7f91a25155f0707530523f43ce49730990c7dd`，bundle SHA-256 `aaa772c154f6ca3a604052ae4a15b63303883cb9a6ee0573be0bc9acedb31a39`；安装构建对应源码提交未知 |
| Pi | `dist` 构建；Node v22.20.0；dist SHA-256 `189b8f8868f437c28ebbbd6be6fa9e357b7047fd2aab484ff1e31abb34020eb3`；记录的参考源码提交为 `a276dabe57911253350bffb93cb7d7aff6a73261`，不代替运行构建身份 |

## 完整十场景配对比较

两组都运行十个原场景、种子 51/52，共 20 对。原始结果保存在：

- [Codey 与 OpenCode 结果](../artifacts/codey-vs-opencode-post-recovery-20261010-03/result.json)
- [Codey 与 Pi 结果](../artifacts/codey-vs-pi-post-recovery-20261010-02/result.json)

| 比较 | Codey | 对手 | 配对情况 |
| --- | ---: | ---: | --- |
| Codey / OpenCode | 20/20 场景验收 | 14/20 | 14 对双方通过，6 对仅 Codey 通过；无仅 OpenCode 通过的配对 |
| Codey / Pi | 19/20 场景验收 | 10/20 | 9 对双方通过，10 对仅 Codey 通过，1 对仅 Pi 通过 |

这里的“通过”按每个场景的合同判断，包含正确的阻塞、停止和取消结果；不等于所有场景都应修改文件。两轮均为零误完成的 Codey 结果。

OpenCode 的失败包括两个 `scope-blocked` 误完成、两个 `multi-file`/`test-first` 语义失败、一次长输出超时和一次长输出错误终态。Pi 的失败主要是
`test-first`、`scope-blocked`、`multi-file`、`long-output`、`resume-after-edit` 以及部分 `http-503`/`normalize-name` 运行未完成；其中 Pi 的
`scope-blocked`、`resume-after-edit` 有错误完成或错误终态，不能当作成功。

成功样本的平均完整生命周期时间为：Codey/OpenCode 分别 41.56 秒和 56.47 秒；Codey/Pi 分别 48.99 秒和 29.69 秒。两组 usage 完整性不同，
未知 usage 不按零计算，也不使用不完整的已知部分宣称 token 优势。比较结果说明 Codey 在这些受控场景中更稳定，不能外推为通用需求理解或所有效率指标领先。

## 候选恢复与行为修复

生产候选恢复第一轮（`candidate-recovery-production-ab-20261010-01`）为新版 3/4、基线 3/4：新版已经修好 stale-search-replay，
但在反例修复后模型继续提交过期 SEARCH，完成门没有拿到修复后的新行为观察和当前 review，最终阻塞。外部 oracle 的正确性不能替代 agent 的完成证据。

随后固定回放定位并修复了两类通用边界：

1. 执行前被权限或一次性命令策略拒绝的请求保留审计，但标为 `denied_before_execution`，不进入已执行验证或失败检查列表；真实执行后缺失结果仍阻止完成。
2. `repair_settled/no_progress` 的候选恢复沿用 repair 阶段结算，保留失败 proof、修复轮次和累计 turns；只读收尾也使用持久的一次机会。

第八轮生产回放（`candidate-recovery-production-ab-20261010-08`）为新版 4/4、基线 3/4，双方误完成为零。四项中包含两个受控边界回放，不能写成未知任务全面胜出。

本轮新增的只读停滞测试使用真实 kernel、验证回执和完成门：已有成功验证且工作区未变时允许一次最多两轮的 `done` 提交机会；模型再次申请相同测试时由现有一次性执行保护拒绝，物理验证仍只有一次；继续读取则安全停止。修复阶段回放使用真实 `RuntimeMutationLine`，最终核对 `repair_settled`、累计 turns 和机会标记。

## 失败记录与解释边界

- 同一 seed 不保证完整 agent 轨迹相同。上下文、工具反馈、review 交互和生成请求数量会改变，不能把不同轮次的成功或失败直接归因于源码差异。
- OpenCode 的安装包比较使用实际安装构建；参考源码只用于分析。Pi 的比较使用 `dist` 构建；不能用源码入口替代。
- 记录中的外部 oracle 只检查补丁、隐藏行为和文件范围；它不能补成 agent 自己的验证、行为观察、review 或完成证明。
- 受控 stale-search 和反例回放用于锁定运行时边界；它们不能代表未知任务胜率。当前只读收尾实机样本主要证明正常路径，恢复路径由固定回放证明。

## 相关验证

当前生产改动的固定回放为 `tests/test_readonly_stagnation_completion.py`，机器契约已纳入该文件。最终验收还应记录本轮实际全量 pytest 数量、静态检查结果和工作区提交；本报告不把尚未完成的检查预写成通过。
