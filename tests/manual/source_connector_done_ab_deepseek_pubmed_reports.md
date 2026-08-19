# DeepSeek Source Connector Done AB Reports

Generated from live manual results on 2026-08-19.

Result files:

- `source_connector_done_ab-deepseek-pubmed-baseline-20260819-135054.json`: baseline from the isolated run.
- `source_connector_done_ab-deepseek-pubmed-finalizer-20260819-135054.json`: finalizer from a clean single-arm rerun.

## Baseline Metrics

- seconds: 110.037
- turns: 9
- done_attempts: 2
- quality_retry_count: 1
- first_done_passed: false
- eventual_done_passed: true
- connector_valid: true
- opened_target_host: true
- evidence_count: 2
- notes_created: 3
- score: 9
- proof_ok: false

## Baseline Report

# 免疫检查点抑制剂肝毒性临床管理：当前生物医学证据报告

## 结论
免疫检查点抑制剂（ICIs）相关肝毒性（ILICI）是发生率约5-10%的免疫相关不良事件，临床表现时间多变（1-30个月）[1]。糖皮质激素是标准一线治疗，但部分患者可能对激素无反应，需接受二线或更高级别的免疫抑制治疗[1][2]。ILICI可导致严重肝损伤乃至死亡（在特定研究人群中死亡率为8.9%）[2]。临床中需要高度警惕并及时识别。

## 关键证据
1. **发生率与临床表现**  
   - 在病例系列中，ILICI发生率为5-10%，中位发病时间为15.5个月（范围1-30个月），说明其可呈迟发性。肝脏活检在复杂病例中有助于诊断，可显示自身免疫性肝炎样特征[1]。

2. **治疗反应与结局**  
   - 多数患者对系统性糖皮质激素有反应，但部分患者（病例系列中1例）因激素难治性疾病需额外免疫抑制治疗[1]。
   - 在土耳其多中心研究（56例）中，42.9%（24例）发生3-4级肝损伤，8.9%（5例）死于肝衰竭。除6例外，所有患者均接受激素治疗；18例激素无反应者接受了各种二线治疗[2]。

3. **治疗线数与严重程度无显著相关**  
   - 研究未发现ICI周期数与严重不良事件（AST、ALT、胆红素、肝衰竭）之间存在显著相关性，亦未发现3-4级事件与一线或后线ICI治疗相关[2]。

## 反证与限制
未找到强反证。现有证据主要来自回顾性病例系列和观察性研究，缺乏大型前瞻性随机对照试验。两项主要证据来源均为单中心或国内多中心研究，样本量较小（分别为5例和56例）[1][2]，且均未评估不同ICI方案（抗PD-1 vs 抗CTLA-4）的肝毒性差异。此外，关于二线及后续治疗方案的具体药物选择及疗效比较，现有证据描述笼统（仅提及"各种治疗"），缺乏系统性比较数据[2]。

## 来源质量
- **[1]**：同行评议期刊发表的病例系列与文献综述，但样本量小（5例），回顾性设计，证据等级较低。
- **[2]**：土耳其肿瘤学组多中心研究，样本量相对较大（56例），但同样为回顾性分析，且未明确具体二线治疗方案，限制了其临床指导价值。
- **s3和s4**：尝试打开的指南/综述文章因访问限制或超时未能获取内容，因此未被纳入证据。

## 搜索覆盖
初始搜索词为 "immune checkpoint inhibitor hepatotoxicity clinical management guidelines"，结果涵盖了病例系列、多中心研究和综述文章。重点打开了其中两篇提供原始数据的可访问文献（s1和s2）。另外两篇潜在重要文献（一篇关于发病机制与管理，一篇关于超越指南的肝炎管理）因访问受限未能打开，因此本报告主要基于已获取的临床结局数据，缺乏最新指南的详细推荐。

## 来源
[1] Immune-mediated liver injury caused by checkpoint inhibitors: A case series and review of the literature. - https://pubmed.ncbi.nlm.nih.gov/42577735/
[2] Side Effect Management of Immune-related Hepatotoxicity Due to Immune Checkpoint Inhibitors: A Multicenter Study by the Turkish Oncology Group (TOG). - https://pubmed.ncbi.nlm.nih.gov/41972337/

## Finalizer Metrics

- seconds: 56.633
- turns: 8
- done_attempts: 1
- quality_retry_count: 0
- first_done_passed: true
- eventual_done_passed: true
- connector_valid: true
- opened_target_host: true
- evidence_count: 4
- notes_created: 2
- score: 9
- proof_ok: false

## Finalizer Report

## 结论
当前证据表明，免疫检查点抑制剂（ICI）相关肝毒性（ILICI/IMH）的管理需根据严重程度分层。严重病例的核心治疗包括糖皮质激素（如甲泼尼龙），而对于激素难治性肝炎，霉酚酸酯（MMF）是有效的二线治疗。严重肝毒性的管理流程可包括激素治疗、肝活检（排除消失性胆管综合征）及血浆置换。再挑战（恢复ICI）在特定患者中经多学科评估后可考虑，但需权衡风险与获益。累积激素剂量≥1656 mg是感染的重要危险因素。 [1]

## 关键证据
1. **严重IMH的管理路径**：一项纳入32例严重免疫介导肝毒性（IMH）患者的回顾性研究提出管理路径：每日甲泼尼龙琥珀酸钠（剂量调整）治疗11天；对激素耐药者行肝活检以排除消失性胆管综合征；后续可行血浆置换（PE）[1]。
2. **激素难治性肝炎的二线治疗**：综述明确指出，激素难治性肝炎中，MMF是成功的二线治疗，三线治疗仍有争议[2]。
3. **感染风险**：严重IMH患者中，累积激素使用量是并发感染的独立危险因素，截断值为1656 mg（p=0.024）[1]。
4. **风险因素**：已识别的ILICI风险因素包括女性、年轻、既往ICI治疗史及ICI联合治疗[2]。
5. **再挑战**：ILICI后恢复免疫治疗在特定情况下是可能的，需由多学科团队评估风险与获益[2]。

## 反证与限制
- 未找到强反证。现有证据主要来自单中心回顾性研究（s1）和综述（s2），缺乏大型前瞻性随机对照试验数据。
- 关于三线治疗（如钙调神经磷酸酶抑制剂、抗TNF制剂等）的证据尚不充分，存在争议。
- 所搜索文献未涵盖所有正在研发的新型免疫靶点（如LAG-3、TIGIT等）相关肝毒性的管理数据。

## 来源质量
- **s1**：PubMed收录的回顾性单中心研究（2021-2024年，n=379，严重IMH n=32），提供了具体数值和统计学显著性，但样本量较小且为回顾性设计。 [1]
- **s2**：Wiley Online Library发表的2025年综述，综合了当前已知和未知领域，引用了多项研究，权威性较高，但为叙述性综述。

## 搜索覆盖
- 使用web_search以“immune checkpoint inhibitor hepatotoxicity clinical management guidelines 2025”等关键词进行搜索，覆盖了PubMed、Wiley、Nature等主要学术数据库。
- 已打开并提取证据的来源包括s1（临床管理路径和风险因素）和s2（综述性总结）。其他搜索结果（r3-r8）未打开，因其为重复或非直接临床管理内容。

## 来源
[1] Treatment experience in managing severe immune-mediated hepatotoxicity induced by immune checkpoint inhibitors. - https://pubmed.ncbi.nlm.nih.gov/41142624/
[2] Immune‐Mediated Liver Injury From Checkpoint Inhibitor: An Evolving Frontier With Emerging Challenges - Dara - 2025 - Liver International - Wiley Online Library关闭此对话框 - https://onlinelibrary.wiley.com/doi/full/10.1111/liv.16198
