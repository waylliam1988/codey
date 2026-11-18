# 2026-11-18 逐记录维护批次交接

用户指令：**这一批处理完停下，留下交接文档，commit。** 本批只收尾六个既有生产候选文件和两个typed验收文件；提交后停止，不启动下一实验。长期Ghost/World Model目标保持暂停，未标记完成。

## 后续文档修订说明（2026-11-18）

用户随后授权修改 Ghost 相关文档；生产主线仍暂停，另行获准的 Hopfield 独立实验已完成。
本次文档修订不恢复生产实现或新增实机实验。本文件保留本批交付事实、
检查结果和当时的缺口列表；下文“然后”描述原批次排序，新调度以
[用户连续性与个人适应主线](../../ROADMAP.zh-CN.md#用户连续性与个人适应主线2026-11-18)和
[团队接手顺序](../ghost_handoff.zh-CN.md#推荐接手顺序)为准。
恢复实现时优先共同经历连续性及一个相处方式闭环，按实际消费者补相关 G0 边界；
其他 family、Explain 或协议适配不作为统一前置。现有容量、旧身份与恢复等缺口仍保留，
两个重复事实 World Model 负结果不重跑。此次文档修订不新增生产能力或模型收益。

独立实验与本维护批次分开记账：[现代 Hopfield 第二轮结果](hopfield-confirmatory-result-20261118.md)
为 `EXPERIMENT_COMPLETE`，生产决策 `EXPERIMENT_ONLY`；受测实现未展示替换简单关联
主基线的收益，不修改本批生产状态，也不证明现有 Hebbian 整体最优。个人适应案例与
通用指令遵循的区分、历史对照和机制研究调度，按上述新主线执行。

## 接手时先读

1. 先检查 `git status --short`、当前分支与最新提交；在原workspace工作，不创建/复制worktree。
2. [本批生产记录](selective-maintenance-production-20261118.md)、[冻结/原始负结果](maintenance-record-scope-20261118.md)、[进度板](README.md)、[团队流程](../ghost_handoff.zh-CN.md)、[类型检查](../typechecking.md)及roadmap 0.6.1–0.6.4。
3. 保护用户原改动：`dist-sdist-check/codey-0.5.11.tar.gz`已删除；`codey-uiux-interactive.html`未跟踪。两项不属于本批commit，不恢复、不清理、不stage。

本批默认入口、事件语义、检查结果和可复现命令以生产记录及JSON报告为准；原四领域12格已经闭合，不因为大项仍INCOMPLETE就重复实现。queue先前提交 `e5ca1bf7`；三个领域本批生产提交与本交接同一批，在 `git log` 可定位。没有新增模型请求，也不把契约修复表述为新的模型A/B收益。

最终交付：33文件1614通过/2子测试；类型整理后最终13文件439通过/2子测试，含全部135项新边界及原12格。最终全仓Ruff/完整702生产树三类型门/precision均通过，八owned文件三检查器通过、额外Any零诊断；未修改依赖10864项/334文件额外诊断及Pyrefly警告分列。最终704源文件hash一致。不要把中间370批或较早1614批伪写成同一份最终源字节的单批结果。

## 恢复授权后继续的真实缺口

- 优先审查受限scope下消费窗口有界化：四owner目前暂缓全库checkpoint，长期deny会增长。需要独立冻结选择性checkpoint/retention语义，实际容量、并发撤回/删除/重建与故障恢复验收，不能删除原始历史或直接恢复全库压缩。
- 证明canonical session父project及旧身份来源，再考虑放宽现保守暂停；不从hash猜归属。本轮不证明任意旧alias。
- 补真实pending daemon payload交接/硬退出、混版本旧reader以及完整生命周期/retention/purge。新Affinity/continuity事件没有无缝降级证明；隔离与回滚按生产记录执行。
- 然后按依赖推进剩余语言遥测、其他family消费者、provider outcome、repair projection、Explain。Deletion Manifest已有默认调度的进展应先核对现记录，未证明边界独立补验，勿重复既成实现。
- World Model仍是目标：选择有新增预测信息的真实预测复盘/恢复/验证消费者，先冻结再跑最小闭环。已终止的verification重复事实与结构化传输两候选不只换提示词补跑。无proof/verification/明确纠正保持unjudged，预测不能成为事实/evidence/权限/完成裁决，模型投影仍经过独立ContextSource和admission。

恢复时继续沿用单本地模型/config收益与相关生产边界门；通过就完成正常生产入口、受测范围默认启用及回滚。没有收益留负结果。每个独立可复查批次及时commit，改动文件完全typed；相关回归后按仓库配置跑完整生产树三类型门、全仓Ruff和precision，测试/工具额外检查Any。不跑全量pytest。

## 本地证据

`.e2e-artifacts/ghost-selective-maintenance-20261118/` 保存候选、夹具失败、验收、扩大回归、静态门日志以及state与702生产文件hash。原始回复/完整日志/state不入仓；仓库只保存typed复现入口、脱敏报告和文档。不要将中间失败改写为一次全通过，也不要把依赖遗留额外Any诊断当作owned已清理。
