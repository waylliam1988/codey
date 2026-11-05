# 编码管线候选 A/B 预注册与运行日志

主树 `D:\codey`，起点 `2c000ebd`。本轮只审计候选价值；删除或默认关闭必须有质量证据。
用户授权在发现确定性生产问题时修复、定向扩大测试、检查 Ruff/类型、分批提交。

## 固定方法

- 当前 KoboldCpp 单模型，原生 native Writer，串行请求，600 秒 provider 超时。
- 同题 arm 用独立临时目录与全新聊天；保存每行结果、原始回复、文件、工具回执、真实 wire
  请求和 usage。失败也计入分母，不把 `no_progress` 直接归为模型能力或服务故障。
- 质量由项目外的独立行为检查判定；可见测试和被模型修改的测试不作为唯一质量依据。
  授权文件范围、生产终态、有效验证、耗时与实际 API usage 分别记录。
- 首轮固定顺序，确认轮反转顺序。不得把更改代码、任务或预算后的样本与旧批次混算。
- 核心任务覆盖已有 bug、功能、跨文件重构和从零生成；不是仅改常量。

## 六个候选及可回答范围

| 候选 | 对照 | 接线与范围 |
|---|---|---|
| Project Map | baseline / map | 相同 AgentRequest，只注入生产地图 |
| coding current context | baseline / context | 相同 AgentRequest，只切生产开关 |
| 自动规划 | 无规划 / 同模型额外规划 | 仅从零任务，复用生产规划 prompt 和 ChangeBrief；不伪称独立 advisor |
| 前置 audit | 无 audit / 同模型只读 audit | 已有项目，调用生产只读 advisor kernel 和 ChangeBrief；不改 advisor provider 选择策略 |
| 项目记忆 | 无注入 / 生产事实注入 | 冷启动空 store 与真实成功运行后的续作分别测试；持久化再加载，不手写答案冒充记忆 |
| Review / 返修 | 不启用 / Review 无返修 / Review 加一次返修 | 相同候选 diff 的缺陷留出与正确控制样本；生产 Review attempt、snapshot 与 Coordinator，最终行为独立检查 |

规划与 audit 的真实自动调度在只有一个 provider 时不会选到独立 advisor。本轮明确检验
“同模型多走这一步值不值”，不通过注册假 provider 改写产品语义。Review 使用本地
self-review，其结果也不等于第二模型独立审查。

## 运行日志

- 2026-11-05：核对生产入口和已有实验脚本。旧四类 fixture 中存在评估歧义：功能题预置了
  已有覆盖却强制要求测试文件变化；重构题强制要求修改未引用旧符号的测试。新任务用
  独立行为 oracle 与授权文件范围判定，避免把“不做无意义测试改动”算失败。
- 下一步：建立统一证据与计费观察，先跑地图/current context，再跑规划/audit、持久记忆与
  固定 diff 的 Review/返修。每批结果追加到本日志和主审计报告。
