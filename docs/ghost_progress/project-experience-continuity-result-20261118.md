# 跨会话项目经历：冻结实机结果

日期：2026-11-18。状态 `EXPERIMENT_COMPLETE`，当前生产决策 `EXPERIMENT_ONLY`；
收益通过，尚须正常入口与相关边界验收。冻结实现提交 `1dc4e79f`，
[协议](project-experience-continuity-protocol-20261118.md)与
[脱敏逐格指标](../../tests/manual/ghost_memory_routing/project_experience_continuity_metrics.json)。

用户可见目标：新会话继续同项目时，记住上次选了什么及其记录理由。
这是已有经历检索消费者，相对无历史的收益；不是新增学习机制或 World Model。

20条独立合成经历流、40次物理生成，on/off各一次、交替次序。
受测 Gemma4 12B QAT 本地配置详见协议，无 embedding、重试或第二遍回答。
on 19/20，off 0/20；borosql 9/10、lumenlog 10/10。
唯一错误是正确选项和理由之后多一个句号，严格逐字 scorer 仍记错。
未更换 scorer、对照或追加样本。无缺失/服务故障，所有请求均一次完成。

额外输入最多280 tokens（门槛600）；on p95总延迟2547ms，off 2594ms，
通过冻结成本门。逐格输出tokens、请求bytes、准备耗时和可见回答时间见JSON。
非流式native的可见时间为adapter返回时间，不是网络首token。
原始回复、wire、SQLite/state、完整日志留在本地
`.e2e-artifacts/project-experience-continuity-20261118-live/`。

复现：

```powershell
python -m pytest -q tests/test_project_experience_continuity_trial.py
python -m tests.manual.ghost_memory_routing.project_experience_continuity --output <新的本地目录> --live
```

实机命令已执行一次，不因生产边界补验再次运行40请求。
实验内替换profile/source，生产尚未启用。下一步补真实App/InputQueue跨会话入口、
模型/config回退、开关、scope、组装后删除/撤回、故障隔离，完成静态门后再晋级。
20条合成流不代表真实用户总体、所有项目问题、其他模型或流式收益。
