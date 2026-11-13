# Hybrid 编辑凭证：定向实验与实机 A/B（2026-11-13）

结论：程序能够绑定模型上次实际看到的完整文件凭证，保留真实版本冲突、显式重读和外部字段。但本轮实机 **A 4/4、B 3/4**，没有证明成功率收益。B 的一次失败发生在 changes 形状，尚未进入版本检查。保持实验状态，未接入生产。

脚本为 [hybrid_edit_receipt_ab.py](../../tools/hybrid_edit_receipt_ab.py)，测试为 [test_hybrid_edit_receipt_ab.py](../../tests/test_hybrid_edit_receipt_ab.py)，两组实机原始回复为[独立夹具](../../tests/fixtures/hybrid_edit_receipt_replies_20261113.json)。[机器结果](hybrid-edit-receipt-20261113.json)和[原始记录包](hybrid-edit-receipt-20261113.zip)随脚本在 `master` 提交，不改 `codey/`，不合并、不推送。

本实验只改变凭证传递。两组共用 edit_fields 入口、网页证据、文件初值、原生 read/edit 执行和字段替换逻辑。A 的参数为 changes、base_result_ref；B 只有 changes。两组都保留 CodeMode，也可自行选择入口。schema 回归确认差异只有凭证参数；模型均未被强制选用 edit_fields。A 也是实验性字段入口，并非原生产 Hybrid 全流程，所以这里比较的是显式传凭证与持有凭证，不能宣称相对原 CodeMode 生产入口提升了多少。

网页使用既有真实 Chromium 夹具，固定完成导航、快照、填写、重新观察、点击、等待和 browser_read。真实读回作为两组共享数据；网页取值仍由模型完成，导航和事实交付机制没有改动。项目文件操作通过生产 TaskSession、冻结合同、CodeMode、read_file、edit 和 WorkspaceRevisionStore 执行。模型循环、edit_fields 降低为真实 edit 的过程以及 done 判定属于隔离实验，不是生产新工具或生产完成约束。

初始完整读取真实执行并展示后，才在磁盘加入 external_revision。程序保存的是该已展示读取的 ref 和原文。编辑时用已有回执原文生成 content，并带同一 base_result_ref 调用原 edit；不读当前磁盘，不自动重读或重试。版本冲突后必须由模型调用 read_file，看到新内容，再提交修改。字段替换复用既有 replace_json_fields，保留未修改内容。只有成功、完整且直接返回给模型的目标文件读取可以更新持有凭证；隐藏、未展示、截断、其他文件以及失败程序内的读取不算。

| 场景 | A 显式凭证 | B 程序持有凭证 |
| --- | --- | --- |
| amount / code，seed 123456789 | 通过 | 通过 |
| amount / code，seed 123456790 | 通过 | 通过 |
| 净重 / 校验码，seed 123456789 | 通过 | 失败：changes 多套 value 外壳 |
| 净重 / 校验码，seed 123456790 | 通过 | 通过 |

七项成功均恰好一次真实版本冲突、两次完整读取、一次成功写入，两个目标字段正确，metadata、external_revision、backup.json 均保留。模型在八项中都主动选择 edit_fields；A 共 8 次提交，B 共 11 次提交，其中 B 失败项有 5 次在字段校验时被拒。失败项两字段仍为初值，外部字段保留，没有成功写入或真实版本冲突。

B 失败的六轮原回复依次是：把目标字段包在 changes.value；把 Result 的 result_ref 和 value 塞进 changes；重读；随后三次仍包 changes.value。没有拿模型的错误字段名替它改正，也没有改变提示后补采样。另一 seed 使用正确 changes 后完成了真实冲突恢复。两组共 10 条原始回复逐字留存为夹具，固定重放分别确认“字段形状错误、未到版本校验”和“真实冲突后恢复”，避免把这项失败归因于凭证绑定。

实机为本地 `http://127.0.0.1:5001/v1` 的 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature 0、thinking 关闭、JSON schema。每个场景两对，第二对反转 A/B 顺序，每项最多 6 次回复。预期值只用于最终验收，不传给模型；模型输入是自然问题、真实网页读回和初始文件读取。

| 整组成本 | A | B |
| --- | --- | --- |
| 真实模型调用 | 16 | 18 |
| prompt token | 27212 | 29025 |
| completion token | 778 | 757 |
| 累计回复耗时（秒） | 33.954 | 31.656 |

B 少传了顶层凭证参数，但没有少调用；整体输出 token 略少也伴随一次失败，不能据此宣称效率提升。计时是本地串行调用观测，并非性能结论。样本只有四对，且复用了同一网页记录，仅换了目标文件字段名，不能外推可靠率。

新增定向测试最终 18 项通过；此前与既有 task-contract 固定回复合跑 27 项通过（16 项新增与 11 项既有），后续两项是实机原回复重放，两个集合有重叠。测试覆盖首次旧凭证必触发冲突、显式重读后两字段正确及外部修改保留、错误 unwrap 在版本检查前拒绝、隐藏或未展示读取不刷新凭证、截断/其他文件/伪造凭证/额外参数/新增字段/篡改已展示文本均拒绝，以及两组 schema 的唯一差异。

另跑了两项 basic。普通生产入口实机未通过，7 次真实模型回复后 no_progress：首次实际执行的导航抄错 URL，空快照取 ref 报 undefined；后续又发生编译错误，然后在错误 URL 页面反复得到“Locality input not found”，未触发缺 ref 诊断。定向对照只固定首个“错 URL → 空快照 → 缺 ref”调用，随后 3 次回复由真实模型生成。请求记录确认模型收到了 `browser_fill missing required arg 'ref'`，先前导航与快照回执仍在，没有旧的回放覆盖错误；模型却只反复 snapshot 后返回控件未找到，没有回到任务 URL，最终也 no_progress。因此诊断修复确实生效，但本轮未带来实机任务恢复；URL 绑定仍是独立问题。

验证包括全仓 Ruff、生产全树 mypy（680 文件），包含新增脚本与测试的 pyrefly 和 ty，以及新增文件 mypy `--disallow-any-expr`、内核类型精度门和 git diff --check。均通过，无新增 Any、类型忽略或配置放宽。pyrefly 留存原有 35 suppressed / 157 warnings，不宣称全仓零警告。两次实机前后生产源码哈希均为 `d326c7e78cdf46062992381eca5ca8ed21ba4a295b24bbe77f65a5c879846038`。

复跑（产物目录必须全新；退出码 0 表示采样完成且生产源码未变，不代表两组通过）：

```powershell
python -m pytest tests/test_hybrid_edit_receipt_ab.py -q
python -u -m tools.hybrid_edit_receipt_ab --run-dir .e2e-artifacts/hybrid-edit-new --pairs 2 --basic
python -u -m tools.hybrid_edit_receipt_ab --run-dir .e2e-artifacts/basic-feedback-new --basic-feedback-only
```

记录包保留实机时脚本、全部提供方请求/回复、浏览器回执、文件操作回执与程序、外部修改时点、每轮交付、初始及最终文件和验证日志，并附逐文件 SHA-256 manifest。文件阶段的回执从会话导出，未验证重启后恢复持有凭证。A/B 运行后仅补充原回复记录和独立 basic 诊断入口，没有重调模型提示或重跑 A/B。`structured` 本轮暂停，`stale_ref` 对照保持原样。
