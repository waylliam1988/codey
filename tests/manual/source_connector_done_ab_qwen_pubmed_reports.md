# Qwen Source Connector Done AB Reports

Generated from live manual results on 2026-08-19.

Result files:

- `source_connector_done_ab-qwen-pubmed-baseline-20260819-134023.json`: baseline from the first isolated run.
- `source_connector_done_ab-qwen-pubmed-finalizer-20260819-134023.json`: finalizer from a clean single-arm rerun.

## Baseline Metrics

- seconds: 172.983
- turns: 14
- done_attempts: 2
- quality_retry_count: 1
- first_done_passed: false
- eventual_done_passed: true
- connector_valid: false
- opened_target_host: false
- evidence_count: 4
- notes_created: 5
- score: 5
- proof_ok: false

## Baseline Report

## 结论
免疫检查点抑制剂相关性肝损伤（ILICI）的临床管理基于严重程度分级，采取阶梯式治疗策略。皮质类固醇是当前指南推荐的主要治疗手段[1]，但约20-30%的患者对激素无反应，需使用二线免疫抑制剂如霉酚酸酯（MMF）[2]。诊断上需排除其他肝病病因，肝活检仅用于非典型或难治性病例[2]。

## 关键证据
1. **一线治疗**：皮质类固醇是ILICI治疗的主流方案[1]。Grade 2患者若实验室指标恶化，起始口服泼尼松0.5–1 mg/kg/天；Grade 3患者需静脉甲泼尼龙1 mg/kg/天。约70-80%的患者在停药及激素治疗后中位5-7周内肝功能恢复正常[2]。
2. **二线治疗**：对于激素无反应者，推荐使用霉酚酸酯（MMF），最大剂量1500 mg每日两次。其他备选药物包括托珠单抗、英夫利昔单抗和他克莫司[2]。
3. **诊断与评估**：ILICI是排他性诊断，必须评估病毒性肝炎、其他药物毒性及自身免疫性肝炎。肝活检不常规推荐，仅在临床表现不典型、出现合成功能障碍或激素/MMF治疗无效时考虑[2]。
4. **机制与现状**：ILICI由浸润肝脏的CD8+ T细胞和单核/巨噬细胞介导。尽管指南推荐激素，但其副作用负担重，且并非所有病例均需使用，亟需高质量临床试验优化指南[1]。

## 反证与限制
未找到强反证。现有证据表明激素治疗有效率高，但部分患者（尤其是继发性硬化性胆管炎亚型）对激素反应极差（仅8.5%完全缓解），提示需进一步细分表型[2]。此外，激素长期使用可能导致代谢并发症。

## 来源质量
- [1] Nature Reviews Gastroenterology & Hepatology (2024): 权威综述，涵盖机制与管理要点。
- [2] Clinical Liver Disease (2024): 提供详细的临床分级、用药剂量及二线治疗方案，具有高度临床指导性。

## 搜索覆盖
检索了2024-2025年关于免疫检查点抑制剂肝毒性管理的指南、综述及共识，覆盖了诊断流程、激素及非激素治疗方案。

## 来源
[1] Immune-mediated liver injury from checkpoint inhibitors: mechanisms, clinical characteristics and management - https://www.nature.com/articles/s41575-024-01019-7
[2] Immune-mediated liver injury from checkpoint inhibitors: Best practices in 2024 - https://journals.lww.com/cld/fulltext/10.1097/cld.0000000000000191~immune-mediated-liver-injury-from-checkpoint-inhibitors-best

## Finalizer Metrics

- seconds: 126.24
- turns: 13
- done_attempts: 1
- quality_retry_count: 0
- first_done_passed: true
- eventual_done_passed: true
- connector_valid: false
- opened_target_host: false
- evidence_count: 1
- notes_created: 2
- score: 5
- proof_ok: false

## Finalizer Report

## 结论
免疫检查点抑制剂（ICI）相关性肝损伤（ILICI）的临床管理主要依据损伤严重程度进行分级处理。根据2024年中国肝病学会发布的共识，轻度（1级）损伤通常无需停药，仅需监测和保肝治疗；中度（2级）需暂时停药并积极保肝；重度（3-4级）则需永久或长期停药，并启动糖皮质激素治疗。对于激素难治性病例，应加用霉酚酸酯或他克莫司等免疫抑制剂。大多数患者经规范治疗后肝功能可恢复正常。 [1]

## 关键证据
1. **分级管理原则**：
   - **1级**：继续ICI治疗，每周监测肝功能，可口服保肝药[1]。
   - **2级**：暂时停用ICI，每3天监测肝功能，给予积极保肝治疗，待肝功能稳定1-2周后可考虑重启[1]。
   - **3级**：停用ICI，每1-2天监测，若进展或反应不佳，启动甲泼尼龙0.5–1.0 mg/kg/day治疗[1]。
   - **4级**：永久停用ICI，住院并给予甲泼尼龙1–2 mg/kg/day静脉注射。若3天无改善，加用霉酚酸酯（500–1,000 mg，每日两次）或他克莫司[1]。

2. **激素难治性处理**：定义为糖皮质激素治疗3-7天后肝功能仍持续异常。推荐联合使用霉酚酸酯、硫唑嘌呤或他克莫司[1]。

3. **预后**：70-80%的3级以上ILICI患者在糖皮质激素治疗后23-46天内ALT恢复正常[1]。

## 反证与限制
未找到强反证。一项西班牙临床试验显示，部分HCV-HCC患者发生3级以上肝损伤后未接受激素治疗，肝功能也逐渐恢复正常，提示部分病例可能具有自限性[1]。此外，本共识主要基于肝癌患者人群，其他肿瘤患者的管理可能存在差异。

## 来源质量
来源[1]为2024年发布的专家共识，由中华医学会肝病学分会组织，遵循GRADE系统对证据质量和推荐强度进行分级，具有较高的临床指导价值。

## 搜索覆盖
检索了2024-2025年关于ICI肝毒性管理的指南和共识，重点覆盖了分级治疗、激素使用及难治性病例的处理方案。

## 来源
[1] Consensus on the Management of Liver Injury Associated with Targeted Drugs and Immune Checkpoint Inhibitors for Hepatocellular Carcinoma (Version 2024) - PMC Lock - https://pmc.ncbi.nlm.nih.gov/articles/PMC12643877/
