# 两项范围 xfail 的根因与修复

## 结论与实现

两项 xfail 是同一个生产缺口在 `inline`、`external_panel` 两种布局中的表现，不是测试落后于生产。在修改前的 `926bfcf8` 上运行原断言（`--runxfail`），两项均真实失败：原控件绑定的目标节点与 `query_region` 已正确，但正式 passage 仍从整个页面生成，混入相邻订单。

本次保留正式来源的完整正文与正文哈希，在该次捕获的来源记录上保存 `SourceReadScope`。映射仅允许空白差异；目标文字必须在同一份正式正文中唯一匹配，范围使用原正文的字符坐标。重复匹配（包括重叠匹配）或无法匹配时，范围明确标为未确认，继续交付普通全文。

初次窗口、`fitting_full_read()`、`reopen_source` 续读、历史来源重建和 passage 恢复共用这个来源范围。恢复重新核对范围与原正文，拒绝范围外的 passage，即使其 URL、正文哈希和 passage 哈希本身有效。来源完整正文仍留在正式回执中，范围内的模型正文不会因全文扩展重新引入邻居。旧回执缺少范围字段时仍可按普通全文读取。UI 来源投影不携带范围中的长原文。

新增长文本回归还暴露了范围末尾的切句问题：段落生成把目标末尾当成被截断的整页窗口，漏掉最后一句。已让段落生成以目标范围末尾为切分终点，保留原正文坐标及原正文哈希。

未修改工具授权、模型判分、点击送达、等待或 CodeMode 合同，也未引入另一套执行入口。为满足类型要求，修改文件中原有的动态表达式改用明确类型和现有 JSON/transport codec；没有新增 `cast`、`type: ignore` 或检查豁免。

## 确定性验证

- 原断言复现：`2 failed, 4 deselected`，5.48 秒。运行命令为 `python -m pytest tests/test_browser_scope_channels.py -k targeted_delivery --runxfail -q`。原终端失败和首次实现遗漏末句的失败均保留，结构化记录位于 `checks/failure-history.md`。
- 第一轮扩大回归：`408 passed, 3 failed, 6 subtests passed`，279.09 秒。失败来自三项旧测试：两处不完整的 ResearchTools 替身缺少已有的 `browser_session`，一处字段清单遗漏已有的 `knowledge_read_versions`。对应生产构造器和输出结构未被本次修改；相关测试已更新到真实 ResearchTools/现有字段，未放宽范围断言。
- 更新后的聚焦回归：`26 passed`，23.51 秒。
- 最终相关生产回归：`411 passed, 6 subtests passed`，280.43 秒，**没有 xfail**。涵盖真实 Chromium 目标绑定、重复文字、范围末句、初读/扩展/续读/历史恢复、旧回执、越界恢复拒绝、浏览器恢复、旧引用、上传下载、HTML/PDF 与来源身份。未运行全量 pytest。
- Ruff：全树 `codey tests tools` 通过。
- mypy：生产树 688 个文件通过；九个修改生产模块额外禁止显式及表达式 Any；全部十二个修改代码/测试文件另行通过相同附加检查。附加的文件检查保留依赖类型，独立生产树检查覆盖所有生产依赖。
- kernel 类型精度门禁与 `git diff --check` 通过。

pytest 的 RAM 盘清理在结束时输出了路径已不存在的清理提示；进程退出码为 0，断言结果不受影响。

## 实机验证

产物根目录：`.e2e-artifacts/browser-target-scope-20261114/`。`source-manifest.json` 保存了全部 829 个追踪生产文件的逐文件 SHA256、修改文件哈希和起始 Git HEAD。生产指纹为 `a5c037ec94715b4dd2cff8fb9445663040661fd40f39c7f623bde5a0ecd5b376`。

使用本地 KoboldCpp Gemma4 12B Q4、temperature 0、seed 123456789 和真实 Chromium。完整矩阵、独立重跑及范围探针的生产指纹均与上述清单一致。

### 完整矩阵首轮：11/12

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/browser-target-scope-20261114/full-surface-first --scenario all --timeout 180`。

| 场景 | 首轮 | 原始请求和回执核对 |
| --- | --- | --- |
| basic Chat | 通过 | 填写、点击、等待、正式读取均成功。 |
| structured | 通过 | 结构化交付和原字段校验通过。 |
| legacy_structured | 通过 | 元数据与 URL 字段通过原校验。 |
| reread + restore | 通过 | 8 次快照恢复、2 次正式读取、1 次点击送达。 |
| stale_ref | 失败 | 六份原始请求均保留故意使用旧 ref 的要求；模型实际使用新的 g4 引用，跳过预期拒绝。`facts_match=true`，`failed_stale_refs=0`，属于模型漏步骤。 |
| Hybrid stale_edit | 通过 | 注入的真实文件版本冲突被拒绝，重新读取后成功编辑。父/子回执共同记录一次冲突。 |
| upload | 通过 | 上传、等待及正文交付成功。 |
| download | 通过 | 两个中间 CodeMode 父调用因模型错误读取 Result 字段而失败；下载本身只执行一次，原回执被恢复，最终产物校验通过。 |
| strict Research | 通过 | 正式读取与研究答案引用通过。 |
| Research sequence | 通过 | 原等待、读取、提交顺序及引用检查通过。 |
| source_failure | 通过 | 预设 403 正确阻断。 |
| fixed_failure | 通过 | 固定采集的预设来源失败正确阻断。 |

`full-surface-audit.json` 保存逐场景正式回执、原始请求摘要和分类；原始模型请求/响应及持久回执仍在各场景目录中。没有发现本次范围链路的新增生产确定性故障。

`independent-rerun-stale-ref/` 使用同一源码和同一场景独立重跑。模型仍跳过预期旧引用拒绝，正文正确但 `failed_stale_refs=0`，仍判失败。首轮保持 **11/12**，不与重跑合并。两轮分别保存 `failure-classification.json`；未为模型漏步骤放宽生产合同或验收。

### 相邻记录实机：交付范围修复，采用与引用仍需区分

命令：`python -u -m tools.browser_query_scope_probe --run-dir .e2e-artifacts/browser-target-scope-20261114/scope-live-first --timeout 180`。原探针、原两个布局与原判分均未修改。

| 布局 | 入口 | 到达定向读回 | 正式 passage 含邻居 | 答案实际引用 | 原 scope 判分 |
| --- | --- | --- | --- | --- | --- |
| inline | 自主 | 否 | 有，走普通全文 | 仅来源 `[s1]` | 未通过 |
| inline | 固定目标 | 是 | 无 | 仅来源 `[s1]` | 未通过 |
| external_panel | 自主 | 否 | 有，走普通全文 | 仅来源 `[s1]` | 未通过 |
| external_panel | 固定目标 | 是 | 无 | 仅来源 `[s1]` | 未通过 |

四次普通任务均通过，答案均未出现邻居字段。固定目标的 **2/2** 已绑定原目标节点，来源回执保存了可复验的范围；同次正式全文仍含邻居，而交付 passage **2/2 不含邻居**。这验证了本次范围修复，不能被解读为采用率或精确引用已经解决。

自主组 **0/2** 进入定向路径，继续得到普通全文。四次答案均没有实际引用目标 passage，仅引用来源，因此原 `scope_passed` **0/4**，保持未通过。没有用空的定向 passage 集合声称自主组隔离成功，也没有将来源级引用升级成精确 passage 归属。逐条正文、范围、passage 和引用审计保存在 `scope-live-audit.json`；探针原 `summary.json`、`validity.json` 与模型记录均保留。

## 范围限制

本次保证的是**已确认映射范围**内的正式 passage。目标文字不能唯一映射时仍允许普通全文，并明确告知未确认。旧版未保存范围的回执保留其原始语义，不在恢复时补造精确范围。来源级引用仍不能代替答案实际 passage 的精确归属核对。
