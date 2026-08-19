# MiMo Source Connector Done AB Reports

Generated from live manual results on 2026-08-19.

Result files:

- `source_connector_done_ab-mimo-pubmed-max24.json`: baseline from the paired max-24 run.
- `source_connector_done_ab-mimo-pubmed-finalizer-only.json`: finalizer from a clean single-arm process after the paired run.

## Baseline Metrics

- seconds: 492.44
- turns: 17
- done_attempts: 2
- quality_retry_count: 1
- first_done_passed: false
- eventual_done_passed: true
- connector_valid: true
- opened_target_host: true
- evidence_count: 4
- notes_created: 5
- score: 9
- proof_ok: false

## Baseline Report

# 免疫检查点抑制剂肝毒性临床管理：现有生物医学证据综述

## 结论

免疫检查点抑制剂（ICI）可引起免疫介导的肝损伤（ILICI/IMH），其机制以肝内浸润的CD8+ T细胞和单核/巨噬细胞激活为核心[1]。当前指南（ESMO、ASCO、AGA及中国2024共识）均以糖皮质激素为主要治疗手段，并根据肝损伤分级实施阶梯式管理：轻度（1-2级）以停药观察和保肝为主；重度（3-4级）需系统性激素治疗，激素无效者加用免疫抑制剂（吗替麦考酚酯、他克莫司等）[1][3]。然而，现有证据主要来自回顾性研究和专家共识，缺乏高质量随机对照试验，指南推荐等级总体偏低[1][2][3]。

## 关键证据

**1. 发病机制**
ILICI以肝内免疫细胞（特别是CD8+ T细胞和单核/巨噬细胞）激活、浸润及组织炎症为特征。PD-1/CTLA-4等免疫检查点通路在维持肝脏免疫耐受中起重要作用，ICI阻断这些通路可导致免疫失衡和肝损伤[1]。

> 「ILICI is characterized by the activation of immune cells, with liver-infiltrating CD8+ T cells and monocytes and macrophages contributing to tissue inflammation.」[1]

**2. 糖皮质激素在指南中的地位**
糖皮质激素是当前指南中ILICI治疗的基石，但其不良反应负担显著，且可能并非所有病例均需使用[1]。美国ASCO、欧洲ESMO及美国AGA均在其指南中推荐糖皮质激素作为一线治疗[1]。

> 「Corticosteroids are the mainstay of treatment for ILICI in current guidelines but are associated with a substantial adverse effect burden and might not be required in all cases.」[1]

**3. 重度IMH的真实世界治疗方案**
一项回顾性研究（32例重度IMH患者）提出如下治疗路线图：每日甲泼尼龙钠琥珀酸酯调整剂量治疗11天；激素耐受者行肝活检排除胆管消失综合征，随后进行血浆置换。累积激素用量≥1656 mg为并发感染的独立危险因素[2]。

> 「A road map was proposed for the management of severe IMH patients: conventional applications of daily methylprednisolone sodium succinate with dose adjustment for 11 days, a liver biopsy to exclude vanishing bile duct syndrome for steroid-resistant patients, and subsequent plasma exchange (PE). Furthermore, cumulative steroid use was identified as an independent risk factor for concurrent infection with a cutoff value of 1,656 mg.」[2]

**4. 中国2024共识的分级管理方案**[3]

| 分级 | 处置方案 | 证据等级 |
|------|---------|----------|
| 1级 | 无需停ICI，每周监测肝功能，口服保肝药（双环醇、多烯磷脂酰胆碱等） | C1 |
| 2级 | 暂停ICI及潜在肝毒性药物，积极保肝治疗，每3天监测；肝功稳定1-2周后可重启ICI | B1 |
| 3级 | 停用ICI，每1-2天监测肝功能及凝血；保肝治疗；若病情进展，予甲泼尼龙0.5-1.0 mg/kg/d；好转后口服泼尼松逐渐减量 | B2 |
| 4级 | **永久停用ICI**，立即住院，甲泼尼龙1-2 mg/kg/d静脉给药；≥3天无效加用吗替麦考酚酯500-1000 mg BID；仍无效可考虑他克莫司；必要时人工肝支持；好转至≤1级后4-6周减量 | A1 |
| 激素耐受 | 激素联合免疫抑制剂（吗替麦考酚酯、硫唑嘌呤或他克莫司），必要时人工肝支持 | B2 |

> 「ICIs-associated liver injury is predominantly IMH, and glucocorticoid therapy is the primary treatment for Grades 3–4 liver injury.」[3]

> 「Grade 4 liver injury: Permanent discontinuation of ICIs is recommended...Glucocorticoid therapy should be administered at 1–2 mg/kg/day. If no improvement occurs after ≥3 days of intravenous glucocorticoids, an immunosuppressant such as mycophenolate mofetil (500–1,000 mg orally twice daily) should be added.」[3]

## 反证与限制

未找到强反证。但以下限制值得注意：

1. **糖皮质激素并非对所有ILICI均必需**：Nature Reviews综述指出部分病例可能不需要激素治疗，提示当前指南可能过度依赖激素[1]。
2. **真实世界数据中部分患者未接受激素也可恢复**：中国共识引用的一项西班牙研究纳入21例HCV-HCC患者，45%出现≥3级肝损伤，但均未使用激素治疗，肝功能自行恢复[3]。
3. **重度IMH中累积激素剂量>1656 mg独立增加感染风险**，提示激素治疗存在安全阈值[2]。
4. **缺乏高质量RCT**：综述与共识均强调亟需针对ILICI的临床试验来优化指南推荐[1][3]。

## 来源质量

| 来源 | 类型 | 质量评估 |
|------|------|----------|
| [1] Nature Reviews Gastroenterology & Hepatology (Triantafyllou et al., 2025) | 系统综述（顶刊） | 高——综合人类与动物研究证据，涵盖ESMO/ASCO/AGA指南比较 |
| [2] PubMed (Hebei Medical University, 2025) | 回顾性单中心研究（32例重度IMH） | 中等——样本量较小，单中心，但提供了真实世界治疗路线图和感染风险量化数据 |
| [3] PMC/JCTH (Zhao et al., 2025; 中国共识2024版) | 多中心专家共识 | 中高——由中华医学会肝病学分会制定，分级管理建议含证据等级标注 |

## 搜索覆盖

- 检索词："immune checkpoint inhibitor hepatotoxicity clinical management guidelines 2024 2025"
- 初始检索结果：8条
- 深入打开并保存证据的来源：3条
- 覆盖范围：综述文献（Nature Reviews）、真实世界回顾性研究（PubMed）、中国专家共识（PMC/JCTH）
- 未覆盖：ESMO/ASCO/AGA原始指南全文、免疫检查点抑制剂联合抗肿瘤治疗的肝损伤管理、生物标志物预测研究
- 可能遗漏：2025年8月之后发表的最新临床试验或指南更新

## 来源

[1] Immune-mediated liver injury from checkpoint inhibitors: mechanisms, clinical characteristics and management - https://www.nature.com/articles/s41575-024-01019-7

[2] Treatment experience in managing severe immune-mediated hepatotoxicity induced by immune checkpoint inhibitors - https://pubmed.ncbi.nlm.nih.gov/41142624/

[3] Consensus on the Management of Liver Injury Associated with Targeted Drugs and Immune Checkpoint Inhibitors for Hepatocellular Carcinoma (Version 2024) - https://pmc.ncbi.nlm.nih.gov/articles/PMC12643877/

## Finalizer Metrics

- seconds: 359.724
- turns: 12
- done_attempts: 1
- quality_retry_count: 0
- first_done_passed: true
- eventual_done_passed: true
- connector_valid: true
- opened_target_host: true
- evidence_count: 3
- notes_created: 4
- score: 9
- proof_ok: false

## Finalizer Report

## 结论
免疫检查点抑制剂（ICI）相关肝毒性（免疫介导性肝炎，IMH/ILICI）是ICI治疗中常见的免疫相关不良事件之一。当前的生物医学证据支持以下临床管理要点：

1. **糖皮质激素是标准一线治疗**：在ESMO、ASCO和AGA等主要指南中，糖皮质激素是ICI相关肝损伤（ILICI）管理的基石药物，但其伴随显著的不良反应负担，且并非所有患者均需使用[1]。

2. **分级管理为核心策略**：中国中华医学会肝病学分会发布的共识（2024版）提供了详细的分级处理方案[2]：
   - **1级**：无需停用ICI，每周监测肝功能，口服保肝药（如双环醇、多烯磷脂酰胆碱等）。
   - **2级**：暂停ICI，积极保肝治疗，每3日监测肝功能，肝功能稳定1-2周后可恢复ICI。
   - **3级**：停用ICI，每1-2天监测肝功能及凝血指标；若保肝治疗效果不佳，启动甲泼尼龙0.5-1.0 mg/kg/日；恢复后可口服泼尼松并逐渐减量。
   - **4级**：永久停用ICI，静脉甲泼尼龙1-2 mg/kg/日；若≥3天无改善，加用霉酚酸酯（500-1000 mg口服每日两次）；霉酚酸酯无效可考虑他克莫司；必要时行人工肝支持治疗。激素减量至少4-6周。

3. **激素耐药的处理**：激素治疗3-7天后肝功能仍持续异常者定义为激素耐药，推荐加用霉酚酸酯或硫唑嘌呤，必要时改用他克莫司[2]。

4. **真实世界证据支持的治疗路径**：一项回顾性研究（379例患者中32例重度IMH）提出了一条治疗路线：甲泼尼龙常规应用并调整剂量11天，激素耐药者行肝活检排除胆管消失综合征，随后进行血浆置换（PE）[3]。该研究同时发现，累积激素剂量≥1,656 mg是并发感染的独立危险因素（p=0.024），提示激素使用需权衡感染风险[3]。

5. **鉴别诊断至关重要**：ILICI仅占ICI治疗期间肝功能异常的少数，排除其他病因（病毒性肝炎再激活、肿瘤进展、其他药物性肝损伤等）是管理的首要步骤[1]。

6. **研究缺口**：目前缺乏高质量临床试验来优化ILICI管理指南，需要更好地理解病理生理机制、识别预测性生物标志物并开发靶向治疗[1]。

## 关键证据
| 证据要点 | 来源 | 证据摘录 |
|---|---|---|
| 糖皮质激素是ILICI指南治疗基石，但副作用重 | [1] Triantafyllou et al., Nat Rev Gastroenterol Hepatol 2025 | "Corticosteroids are the mainstay of treatment for ILICI in current guidelines but are associated with a substantial adverse effect burden and might not be required in all cases." |
| 4级ILICI应永久停ICI，静脉甲泼尼龙1-2 mg/kg/日 | [2] Zhao et al., JCTH 2025 (Chinese Consensus) | "Grade 4 liver injury: Permanent discontinuation of ICIs is recommended... Glucocorticoid therapy should be administered at 1–2 mg/kg/day. If no improvement occurs after ≥3 days of intravenous glucocorticoids, an immunosuppressant such as mycophenolate mofetil (500–1,000 mg orally twice daily) should be added." |
| 重度IMH激素耐药者可行血浆置换；累积激素≥1,656 mg增加感染风险 | [3] Hebei Medical University study, PubMed 2025 | "A road map was proposed for the management of severe IMH patients: conventional applications of daily methylprednisolone sodium succinate with dose adjustment for 11 days, a liver biopsy to exclude vanishing bile duct syndrome for steroid-resistant patients, and subsequent plasma exchange (PE). Furthermore, cumulative steroid use was identified as an independent risk factor for concurrent infection with a cutoff value of 1,656 mg (p = 0.024)." |
| 霉酚酸酯为激素耐药IMH首选二线免疫抑制剂 | [2] Zhao et al., JCTH 2025 (Chinese Consensus) | "For steroid-refractory immune-mediated hepatitis, glucocorticoids should be combined with immunosuppressants such as mycophenolate mofetil, azathioprine, or tacrolimus. Artificial liver support therapy should be considered when necessary (Grade B2)." |
| ILICI仅占ICI期间肝功能异常的少数，排除鉴别诊断是关键 | [1] Triantafyllou et al., Nat Rev Gastroenterol Hepatol 2025 | "ILICI accounts for a minority of cases of abnormal liver function tests during immune checkpoint inhibitor treatment; investigation and exclusion of differentials is a vital step in ILICI management." |

## 反证与限制
未找到强反证。已检索的文献在分级管理框架上基本一致（ESMO、ASCO、AGA及中国共识均以糖皮质激素为核心），但存在以下局限：

- 目前缺乏大规模随机对照试验（RCT）直接比较不同治疗策略（如糖皮质激素vs.观察等待、不同二线免疫抑制剂之间）在ILICI中的疗效。
- [1]明确指出"corticosteroids...might not be required in all cases"，提示对于低级别ILICI，积极使用激素可能并非必要，但尚无前瞻性数据支持替代策略。
- [3]的回顾性研究为单中心、小样本（32例重度IMH），其激素剂量-感染风险阈值（1,656 mg）需在更大人群中验证。
- 对于不同ICI药物（抗PD-1 vs. 抗PD-L1 vs. 抗CTLA-4）导致的肝毒性差异，以及联合治疗（ICI+靶向药物）的肝损伤管理，现有证据仍不充分。

## 来源质量
| 来源 | 类型 | 质量评估 |
|---|---|---|
| [1] Nat Rev Gastroenterol Hepatol | 系统性综述（Nature Reviews子刊） | 高质量——由Triantafyllou等人撰写，发表于顶级综述期刊，综合人类和动物研究证据，涵盖ESMO/ASCO/AGA三大指南。付费墙限制全文获取，但摘要和关键点信息充分。 |
| [2] JCTH 2025 | 专家共识（中华医学会肝病学分会） | 中高质量——多学科专家共识，覆盖HCC靶向治疗和ICI相关肝损伤的监测、诊断、预防和治疗，提供详细的分级推荐和证据等级（A1/B1/B2/C1）。内容聚焦HCC患者，可能不完全适用于其他肿瘤类型。 |
| [3] PubMed 2025 | 回顾性单中心研究 | 中等质量——真实世界数据，379例患者中32例重度IMH，提供了激素路线图和感染风险的定量证据。样本量小、单中心设计、回顾性分析，证据级别有限。 |

## 搜索覆盖
本次检索以"immune checkpoint inhibitor hepatotoxicity clinical management guidelines"为主要查询词，覆盖PubMed、Nature Reviews、PMC、JCTH等主要医学数据库。共识别8条相关结果，深入分析3篇核心文献。主要指南（ESMO、ASCO、AGA）的推荐意见通过综述[1]和共识[2]间接获取。未单独检索的来源包括：Clinical Liver Disease（LWW，r5）和Science Direct综述（r8），因访问受限未能获取全文。检索时间为2026年8月，文献发表截止期覆盖至2025年初。

## 来源
[1] Immune-mediated liver injury from checkpoint inhibitors: mechanisms, clinical characteristics and management | Nature Reviews Gastroenterology & HepatologyClose bannerClose banner - https://www.nature.com/articles/s41575-024-01019-7
[2] Consensus on the Management of Liver Injury Associated with Targeted Drugs and Immune Checkpoint Inhibitors for Hepatocellular Carcinoma (Version 2024) - https://www.xiahepublishing.com/2310-8819/JCTH-2025-00228
[3] Treatment experience in managing severe immune-mediated hepatotoxicity induced by immune checkpoint inhibitors. - https://pubmed.ncbi.nlm.nih.gov/41142624/
