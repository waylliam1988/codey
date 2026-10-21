# Ghost 偏好投递与预算路由实验

这里保存同 prompt 排序、扩槽位排序、分层路由、偏好族投递与预算压力实验的脚本和
历史报告。它们是 manual/evaluation 材料，不是生产功能或自动上线控制器。

## 文件与数据位置

| 文件 | 作用 |
|---|---|
| `seed_arms.py`、`run_arm.py`、`build_report.py` | 相同 prompt 条件下比较生产排序、新近排序与空白对照 |
| `round2.py`、`dump2.py` | 扩语言/命令等槽位，在条目上限下比较入选差异 |
| `round3.py` | 分层收益排序与冲突替换消融；新近/生产/空白数据复用前一个实验 |
| `round4.py` | 语言/命令 on/off、当前请求覆盖、预算压力及离线生命周期检查 |
| `summarize.py` | 从本地原始数据导出无回复 metrics，或独立复算随目录保存的快照 |
| `scope_authority_smoke.py`、`scope_authority_report.py` | 真实 planning builder/kernel/ApiProvider 的 on/off 结构 smoke、baseline/candidate payload 摘要与脱敏报告，不作 language 晋级 |
| `history_expiry_probe.py` | 固定 text/native 响应及显式时间，复现后续 HTTP history 的过期 hint 重发；保留负结果，不接生产 guard |
| [reports/](reports/) | 历史报告、合成偏好 directive 快照、parity/audit 与脱敏测量 |
| `artifacts/ghost_memory_routing/`（仓库根目录下） | 完整本地归档、原始 JSONL、隔离 state、生成报告；已被根目录 `/artifacts/` 规则忽略 |

`reports/REPORT.md` 对应同 prompt 排序，`REPORT2.md` 对应扩槽位排序，
`REPORT3.md` 对应分层路由，`REPORT4.md` 对应偏好族与预算压力验收。
`reports/REPORT4-frozen-evaluation.md` 是 0.6.0 冻结 evaluator 重新复算的脱敏报告；其
完整性规则和 `PROMOTE-MATCHED` rank 映射由 `evaluation_contract.py` 固定。
`reports/REPORT4-admission-live.md` 是 0.6.1 真实 runtime/provider-send smoke 的脱敏报告；
它只覆盖 admission smoke suite，不构成 language 晋级对照。
`reports/scope-authority.md` 记录请求作用域安全修复及新 live payload parity；
`reports/history-expiry.md` 记录完整 history 仍有 expiry 缺口，不能把初次准入通过当作完整发送验收。
数字保留为历史文件标识；长期路线使用实验名称和具体行为描述。

归档迁入日期为 2026-10-19。原目录共 1,491 文件、48,217,898 字节，迁移时逐文件
验证 SHA-256；来源文件摘要见 [reports/archive_manifest.json](reports/archive_manifest.json)。
manifest 仅保存 25 个代表文件的摘要，不含全部 1,491 个文件的逐文件清单；全量计数和
字节数是迁移时的验证记录。原始文件名保留在 `files` 键中；`snapshot_path_overrides`
记录快照命名修正（`parity/triiggers.json` → `parity/triggers.json`），原始归档不改名。
本目录 `.gitattributes` 禁止报告快照的自动换行转换，以保留原始字节摘要。
原始脚本与数据也完整保留在本地归档中；这里的可运行脚本仅调整路径、导入、输出文件名及静态检查问题，
保留历史 treatment 与 scorer。历史 capture 的 Git revision 未记录，不用迁移时 revision 冒充。

`modelA` 是数据中的 suite 标签，其记录模型 ID 为
`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；没有第二模型的测量。

## 不需要模型的复查

从仓库根目录执行：

```powershell
python tests/manual/ghost_memory_routing/summarize.py
python tests/manual/ghost_memory_routing/round4.py --help
```

`summarize.py` 默认读取随目录保存的 `reports/measurements.json`，仅使用 Python 标准库，
没有本地 state、原始回复或模型服务也能复算各 cell 的分子、分母与字符总数。
其中分层实验只有新增的 18 次运行，复用基线没有重复计入。

完整本地归档存在时，也可以复算原格式报告：

```powershell
python tests/manual/ghost_memory_routing/build_report.py
python tests/manual/ghost_memory_routing/round2.py report
python tests/manual/ghost_memory_routing/round3.py report
python tests/manual/ghost_memory_routing/round4.py report
```

如果 live 运行中断，恢复后可以只追加缺失 index，不覆盖已有 JSONL：

```powershell
python tests/manual/ghost_memory_routing/round4.py run --suite <suite> --cells lang_on_d2 --indices 14,15,16,17,18,19 --endpoint <URL> --model <MODEL_ID>
```

生成文件写到 `artifacts/ghost_memory_routing/`，不会覆盖随目录保存的历史 `reports/`。
设置 `GHOST_EXPERIMENT_HOME` 可将所有生成物和隔离 state 重定向到另一个目录。
重新导出 metrics 时显式执行 `summarize.py --export --input <新快照路径>`，不要覆盖历史快照。

## 第二模型或新配置运行

从仓库根目录执行；模型服务需已由使用者配置并启动：

```powershell
python tests/manual/ghost_memory_routing/round4.py seed
python tests/manual/ghost_memory_routing/round4.py run --suite modelB --endpoint <URL> --model <MODEL_ID>
python tests/manual/ghost_memory_routing/round4.py report
```

迁入的归档已有 `R4` seed，`seed` 会跳过。新数据目录需要重新 seed。
`round4.py offline` 的冲突/删除检查会自行创建 `R4C`，不依赖 `round3.py t5`；
但同一命令里的过期检查仍读取 `T5`。新目录运行完整离线检查时，先执行一次
`python tests/manual/ghost_memory_routing/round3.py t5` 准备过期 fixture，再执行
`python tests/manual/ghost_memory_routing/round4.py offline`。归档已有 `T5` 时无需重种。
不能把缺少该 fixture 时的空 directive 当成过期验证通过。
同名 suite/cell 的 `run` 会重写对应原始 JSONL，应为新模型或配置使用新的 suite 名。
这些是显式 live 运行，会启动本地测试 HTTP server 并调用指定模型；报告复算不调用模型。

## 历史结论的使用边界

- language 的 19/20 对 0/20 只支持受测模型和配置的有条件生产候选；生产 allowlist 与真实
  Context Epoch 发送链路尚未通过同一 treatment 验收。
- command 主 on/off 各 0/20，预算臂存在零星成功；intent 层宿主尚未被证明。
- 行数、内容和顺序同时变化，不能把预算曲线的变化全部归因于位置。
- cap8 分层无自述，但 cap12 仍有三条；零先验不等于显式排除。
- 冲突检查的总节点数不能证明 supersede 失败，应核对 active/status、superseded_by 与发送集。
- 历史 `family_verdicts()` 会跳过缺失 suite、把缺失 off 视作零，未强制最低样本量，
  且 `PROMOTE-MATCHED` 缺 rank 映射。它的 PROMOTE 不是生产 gate；未来使用前必须按
  [晋级协议](../../../docs/ghost_future_direction.zh-CN.md#06-晋级协议)修正，并冻结必测矩阵。
- 0.6.0 已将这些完整性问题移到 `evaluation_contract.py`：modelA 单模型试点固定为
  language/command 各自 on/off、4 个问法、每问法 5 次；缺失 suite、control 或样本量输出
  `INCOMPLETE`，不会把缺失当成 0。该结果仍是 `EXPERIMENT_ONLY`，没有改变默认 prompt。

长期结论与实现顺序见 [Ghost 方向](../../../docs/ghost_future_direction.zh-CN.md)和
[Roadmap](../../../ROADMAP.zh-CN.md)。原始报告保持历史判定，后续解释不回写成新测量。
