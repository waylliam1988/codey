# 跨模型最终代码质量复核：预注册与运行记录

## 用户目标与判断标准

只以最终交付的代码质量判断规划、前置 audit、consensus 和第二 advisor 的价值。
请求数量、token 和耗时仅作描述，不作为删除或不开启的理由。不运行全量 pytest。
任何失败先区分夹具/评估器、生产代码、模型行为和上游服务；修复后保留原失败证据。

## 本轮起点

- Git 起点：`97e4ea3d`，工作树干净。
- 原有相关回归：55 passed。
- 上轮自动规划的 `zero-inventory` 虽然 `no_progress`，最终独立质量 4/4、suite 通过；
  `cross-file-rename` 才是实际质量退步（2/4、漏改调用方）。不能将 3/5 完成率当作质量通过率。
- 连接预检：本地 Gemma4、Lightning、Solar 均真实返回 READY。Super 返回 HTTP 404；
  刷新 Cline 目录后仅有 Solar、Beam、Lightning，Super 已不在目录中。这是上游可用性变化，
  不是 Codey Review parser 的故障。用户已明确不要求固定某个在线模型。
- 固定本轮角色：本地 Gemma4 Writer、Lightning 第一 advisor、Solar 第二 advisor；
  Solar 作为各组统一的独立 Reviewer。Reviewer 独立于 Writer，但双 advisor 组中兼任 advisor，
  不将其宣称为第三个独立在线意见。Cline 是同一在线连接，不能把两种模型叫作两个独立服务商。

## 设计

- 10 个项目夹具：8 个新留出题与 rename/slug 两个旧回归题。
- 已有项目：跨文件改名、原子转账、递归配置隔离、事件版本去重、范围解析验证。
- 从零项目：slug、LRU、动态字典序依赖排序、假时钟过期缓存、闭区间合并。
- 每组保留生产项目地图与 coding current context；使用相同独立 Review、最多一次 Writer
  返修与二次 Review。Writer 总预算 32 turn，返修同时受剩余预算与生产 12 turn 上限约束。
- baseline 不加前置步骤；plan 加一个在线计划；audit 使用生产只读审计工具循环；
  consensus_one/two 使用生产 owner draft → 一/两个 advisor → aggregate。
- audit 仅已有项目，consensus 仅从零项目。已有项目 plan 是对旧失败的诊断性扩展，
  单独标记，不能冒充生产自动新项目规划的默认触发范围。
- 首批两轮，第二轮将每题 arm 顺序反转，共 70 行；题目顺序打乱，调度 seed 61106。
  seed 只控制执行顺序，未控制模型采样，不宣称模型输出确定性或统计性排名。
- 先做少量接线 pilot，不把 pilot 混入正式计数。扩大或复核的批次分别归档。

## 评估与证据

- 每题独立外部行为 oracle、生成/现有测试是否有效且通过、允许修改范围及必需交付文件。
- 最终 quality 与 `done/no_progress`、Review approved、实际 advisor 数量分别记录。
  保留所有失败、最终文件、工具事件、请求和原回复，不删除不利样本。
- 外部 oracle 不交给 Writer/Reviewer，也不作为自动返修反馈。参考实现仅验证夹具，
  不发送给模型。每个新题都有能被 oracle 拒绝的代表性错误实现。
- 先记录 Writer 初稿，再记录整个 Review/返修后的结果；主要比较后者，初稿作诊断。
- 检查需求遗漏、边界/原子性、调用方兼容、输入隔离、测试有效性与范围。当前没有经过
  盲评的可维护性分数，不用“测试全绿”声称架构、可读性和大项目质量均已验证。
- 保存真实模型身份、完整生产源码哈希、实验配置与顺序。配置/源码变化禁止混合 resume。
- 这是中小型受控项目实验，尚不覆盖真实大型仓库、长期任务或所有语言。

## 初始门禁

新夹具的参考实现/缺陷对照及调度、质量口径：首次 12 passed；Ruff 通过。
生产返修预算核对后新增上限回归，正式运行前补跑。

## 后续记录

运行结果、失败根因、修复、检查和提交记录在本文件及最终审计报告持续追加。
