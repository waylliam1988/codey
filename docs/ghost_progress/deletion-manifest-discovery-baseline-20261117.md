# Ghost 删除响应丢失后的发现缺口

- 日期：2026-11-17；比较基线：`e09a44ac`。
- 状态：`EXPERIMENT_COMPLETE`（确定性基础设施负结果，不涉及模型收益）。
- 目标：客户端未指定 ID、未收到响应时，通过普通 export/summary 找回原操作，
  再显式以同 ID 和原 scope 恢复，避免新建删除误删完成后的新输入。

## 冻结实验与结果

`tests/test_ghost_manifest_discovery.py` 使用普通 GhostControlSurface，种入真实 Ghost
store 数据，子进程调用缺省生成 ID 的 delete_scope。三个窗口使用 `os._exit(73)`：
manifest 已提交、首个 store 已提交但第二个尚未执行、全部 store 完成但响应尚未返回。
首个窗口的 hook 在进入 coordinator 之前，确保控制面的 manifest 外层事务已提交；
不能在嵌套 start 返回、外层尚未提交时退出并将其误当持久操作丢失。
父进程不知道操作 ID，重开普通 surface，通过 export/summary 寻找并尝试显式恢复。
子进程前后全部 `codey/**/*.py` 哈希一致，期间没有改动生产源码。

```powershell
python -m pytest tests/test_ghost_manifest_discovery.py -q --tb=short
```

最终基线 **3 failed**，均在 `exported["deletions"]` 处 KeyError，子进程退出码73、
无 stdout 及源码冻结断言已通过。尚未执行的 summary、恢复、新输入保护断言不能记成通过。
此前一次同样三个 KeyError 的运行使用了过早的 start hook，已修正并重新执行；
不把那次当作“manifest 已提交”窗口的有效证据。

随后全树 Ruff、完整 codey 的 mypy（701 模块）、Pyrefly（0 errors / 35 既有
suppressed / 159 warnings）、ty 和 precision gate 依次通过；新测试单独经三种类型
检查通过。额外 `--disallow-any-expr` 审计该测试零 owned 诊断，192 个未修改依赖
文件4086项诊断、整体 exit1；日志留本地 `.e2e-artifacts/ghost-discovery-baseline-any.txt`。
`git diff --check` 通过；未运行全量 pytest、模型或前端构建。

## 下一步与边界

增加有界、可分页、重放校验的操作发现，并默认接入现有生产 summary/export/认证 action。
完成记录也保留发现能力；pending 仍需显式同 ID 恢复，blocked 不自动重试。
分页必须报告损坏或孤儿事件，不能伪装成没有操作；scope 输出隔离、用户关闭和认证保留。
不增加启动时自动执行删除，也不重跑停止的语言模型批次。
本记录不证明完整 retention、升级输入、显式时间 replay 或 World Model 已完成。
