# Ghost 升级旧投影的消费者准入缺口

- 日期：2026-11-17；比较基线：`9e318370`。
- 状态：`EXPERIMENT_COMPLETE`（确定性基础设施负结果，无模型调用）。
- 假设：原始 canonical receipt 缺失或为 false 的 run，不能因升级前已有投影而重新
  获得 retrieval、模型可见 continuity 或待办执行资格。原始 true 的对照必须仍可用。

## 冻结实验

`tests/test_ghost_legacy_projection_admission.py` 先在独立 Ghost 中写入合法 observation、
continuity run_ledger、work_checkpoint 待办及其事件/游标，确认三个旧投影都可读取。
然后移除 schema7 的 authority 表和四个新 receipt trigger，置 schema6；独立 canonical
store 通过原始 admission commit 分别保存无 receipt、false、true。由正常 GhostSQLite
初始化升级到当前 schema 并绑定同目录 canonical authority，分别使用旧 owner 和重开 owner。

固定矩阵：3种原始 receipt × 4个真实消费者（retrieve、continuity renderer、待办列表、
claim_next）× 2种 owner，共24项。生产源码不做候选替换，不从当前 mutable content 补 receipt。
true为正对照；None/false仍被消费者采用是失败，不能把持久投影的存在当作接收授权。

```powershell
python -m pytest tests/test_ghost_legacy_projection_admission.py -q --tb=short --junitxml=.e2e-artifacts/ghost-legacy-admission-20261117/baseline.xml
```

## 结果与有效性

最终有效矩阵 **8 passed / 16 failed**，24项均执行到消费者判断。
四类消费者、两种owner都继续采用None/false来源；true正对照8项通过。
尤其claim_next返回旧任务，即便后续状态写入会过滤不合格来源，也已经向调用者交出待执行项。
原始来源receipt没有被这些消费者自动改写。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/legacy-projection-admission-baseline-20261117.json)
记录consumer、owner、原receipt与可见性，不保存原始state或失败栈中的临时目录。

前两次各24失败均在fixture的升级前可见性校验，不能算生产负结果。
待办session scope使用生产session_key，而非原始字符串；只增加status筛选不能修复身份错误。
修正为规范化session身份，并按renderer已有的尾部句点清理规则比较marker后才执行上述有效矩阵。

既有canonical receipt/SQLite回归 **70 passed**。随后全树Ruff、完整codey的mypy（701模块）、
Pyrefly（0 errors / 35既有suppressed / 159 warnings）、ty、precision gate依次通过。
新测试另经三种类型检查；额外Any审计最初发现SQLite Row短路表达式的两处隐式Any，
改为先接收object再比较，最终矩阵仍8通过/16失败；本文件最终零owned诊断。
191个未修改依赖文件4085项诊断、额外检查整体exit1，日志留本地`baseline-any-final.txt`。
未新增cast、ignore或豁免，未运行全量pytest或模型批次。

## 后续合同

默认消费者在读取/选择时复用原始receipt与来源退役判定；先过滤再做容量/排位，
不能让未准入旧来源挤掉健康数据。保留独立Ghost用法、明确人工确认的Hebbian偏好和研究笔记。
旧原始数据保持可审计，不凭空生成true receipt、不把None当成功，不让普通任务因Ghost问题失败。
补rebuild/Sleep/restart、实际应用入口、新输入及scope/关闭回归，再运行全树静态门。

容量裁剪、普通scope删除与历史事件保留契约仍有独立缺口；本次不证明完整retention、
所有旧输入迁移、异常scope、显式时间replay或World Model完成。
