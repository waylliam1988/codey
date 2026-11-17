# 图同分裁剪与重建一致性

- 日期：2026-11-18；关联 0.6.4。
- 状态：`INCOMPLETE`（冻结假设，尚未执行新矩阵）。
- 基线：`0f36de60`；不重做已闭合的时间/损坏事件矩阵。
- 模型：不调用模型；这是存储确定性实验，不计模型收益或阴性。

## 冻结目标、变量与停止条件

代码审查发现共享节点/边裁剪只按状态、权重和时间排序，同分保留输入顺序；SQLite reader
按语义身份排序，event replay 按历史插入顺序。假设：边达到真实 degree cap 后新增更强边，
普通 Hebbian writer 与同一日志 rebuild 会保留不同的旧同分边。当前尚未声称已复现。

主要变量仅为旧边权重是否并列。不修改生产常量、degree cap、时钟、scorer 或历史。
冻结 `maintenance_as_of` / 事件产生时间为 2026-11-18T00:00:00Z；不增加模型配置。

主矩阵 3 scope（user/project/session）×2 恢复方式（warm/cold）×2 权重组（tie/unique），
共 **12 cell**。每 cell 用实际 inbox ingest/review 和公开 reinforce_candidate 建立 hub、
9 个 peer；按反向语义身份顺序逐个建立 8 个旧边（真实上限 8），再加入第 9 个更强边。
tie 旧边 reward=0.5；unique 旧边 reward=0.1+index×0.05；新边 reward=1.0，confidence=1。
所有节点同 run、不同 slot，scope 正常绑定。比较普通 writer 状态与连续两次 rebuild；
检查事件未变化、8 边上限、新强边保留、节点集合及状态 digest。冷组在 rebuild 前重开 owner。
unique 是正对照；若夹具未建成、scope/来源无效、未达到真实 cap 或重建拒绝，记
`INCOMPLETE_FIXTURE` 并先修夹具，不能算生产负结果。有效状态差异按失败保留，不放宽断言。

辅助契约：三个有效领域节点/边的全部 6 种排列分别裁剪到 2 节点/1 度边，12 项。
主优先级保持既有状态/权重/时间；完全同分以节点 id、边 (source,target,relation) 升序
作确定性 fallback，与 SQLite 顺序一致。未重排语义事件、未合并证据或改变学习/衰减算法。

成功目标：原 24 项全部通过；普通路径默认使用同一稳定裁剪，无新关闭开关。
若仅加身份排序不能闭合实际主矩阵，继续追查 event/live transition，不把纯 helper 通过
当生产修复。生产晋级还须相关正常入口、禁用、scope、撤回、删除、reset、重建、重启及
事务故障恢复回归。故障必须保留有效投影和原历史；不得通过删除原日志获得一致。

## 验证与交付约定

先保存原始失败、有效性检查和脱敏 cell 指标并独立提交，再改生产。修改的 Python 文件
参数、容器、回调和返回类型明确，清理完整文件 Any，不使用 cast/ignore/新豁免。
相关回归后串行运行全仓 Ruff、完整 codey 的 mypy/Pyrefly/ty、kernel precision gate；
本轮文件另做三类型和 Any 审计。不运行全量 pytest，不改前端。

原始 stdout/XML/state 留 `.e2e-artifacts/ghost-graph-tie-replay-20261118`；仓库只保留
可复现测试、脱敏指标和记录。原始基线与生产修复分开 commit，只有正常 master 路径
默认接线后标 `PRODUCTION_MERGED`。同分选择的兼容变化、回滚和限制在结果中明确。
本轮不证明完整 G0、全部容量/排序/digest、跨机器运行或 World Model 生产闭环。
