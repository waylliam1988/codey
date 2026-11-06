"""Hand-labelled synthetic support relations; labels never enter model input."""

from __future__ import annotations


def case(identity, split, evidence, sentences, *, question="根据给定材料回答适用范围和结论。", kind="html"):
    return {"id": identity, "split": split, "question": question,
            "evidence": [{"label": f"e{i}", "excerpt": text, "content_kind": kind,
                          "locator": {"source_id": f"{identity}-s{i}", "revision": 1,
                                      "start": 0, "end": len(text)}} for i, text in enumerate(evidence, 1)],
            "markdown": "\n".join(text for text, _ in sentences),
            "gold": [label for _, label in sentences]}


def cases():
    return [
        case("or_rule", "dev", ["Nera opens read-only if either a sidecar exists OR the directory is writable. Neither condition is necessary alone."], [
            ("Nera 满足存在 sidecar 或目录可写之一即可只读打开。[e1]", "entailed"),
            ("Nera 必须同时存在 sidecar 且目录可写才能只读打开。[e1]", "contradicted"),
            ("Nera 的吞吐量提升了两倍。", "unverified")]),
        case("version", "dev", ["Tavo 2 supports local files only. Tavo 3 additionally supports remote files."], [
            ("Tavo 3 支持远程文件。[e1]", "entailed"),
            ("Tavo 2 也支持远程文件。[e1]", "contradicted"),
            ("## 版本比较", "not_claim")]),
        case("association", "dev", ["An observational survey found an association between Luma use and shorter waits. Random allocation was not used; causation cannot be established."], [
            ("调查发现 Luma 使用与等待缩短相关。[e1]", "entailed"),
            ("该调查证明 Luma 导致等待缩短。[e1]", "contradicted"),
            ("该调查覆盖一万名参与者。", "unverified")]),
        case("compound", "dev", ["Pavo encrypts stored records. No information about backups is provided."], [
            ("Pavo 对存储的记录加密。[e1]", "entailed"),
            ("Pavo 对存储的记录加密，也支持自动备份。[e1]", "partially"),
            ("请继续核对备份信息。", "not_claim")]),
        case("arithmetic", "dev", ["Plan A costs 4 credits per job.", "Plan B costs 7 credits per job. All other charges are zero for both plans."], [
            ("由两份价格表可算出，同样一个任务 A 比 B 少花 3 credits。[e1][e2]", "entailed"),
            ("由两份价格表可算出，同样一个任务 A 比 B 多花 3 credits。[e1][e2]", "contradicted"),
            ("A 的可靠性更高。", "unverified")]),
        case("abstract", "dev", ["The abstract reports a mean reduction of 12%. It does not give sample size or allocation methods."], [
            ("摘要报告平均降低 12%。[e1]", "entailed"),
            ("全文方法部分表明样本量为 800 且采用随机分组。[e1]", "unverified"),
            ("摘要报告降低 12%，并给出了排除标准。[e1]", "partially")], kind="abstract"),
        case("necessity", "dev", ["For Sela, a valid key is necessary for export, but it is not sufficient. An approved account is also required."], [
            ("Sela 导出需要有效 key。[e1]", "entailed"),
            ("Sela 只要有有效 key 就必定能导出。[e1]", "contradicted"),
            ("## 导出条件", "not_claim")]),
        case("scope", "dev", ["In 20 tested steel specimens at 10 degrees, coating Z reduced wear. No other materials or temperatures were tested."], [
            ("在 10 度的 20 个钢样本上，Z 涂层降低磨损。[e1]", "entailed"),
            ("Z 涂层在已测钢样本降低磨损，且在所有塑料上也有效。[e1]", "partially"),
            ("Z 涂层没有毒性。", "unverified")]),
        case("unless", "holdout", ["Rivo accepts uploads only if quota remains. Even then, an expired account is rejected."], [
            ("Rivo 接受上传必须有剩余额度。[e1]", "entailed"),
            ("Rivo 只要额度有剩余，就一定接受已过期账户的上传。[e1]", "contradicted"),
            ("Rivo 支持离线编辑。", "unverified")]),
        case("exception", "holdout", ["All exported records are compressed except images, which are exported uncompressed."], [
            ("图片导出不压缩。[e1]", "entailed"),
            ("所有导出记录，包括图片，都压缩。[e1]", "contradicted"),
            ("是否需要继续研究？", "not_claim")]),
        case("cross_source", "holdout", ["An Ivo unit consumes 8 watts when active.", "In this setup exactly 3 Ivo units are active; no other equipment consumes power."], [
            ("根据单台功率和数量，该配置合计消耗 24 瓦。[e1][e2]", "entailed"),
            ("该配置消耗 24 瓦并能在水下使用。[e1][e2]", "partially"),
            ("该配置能连续使用十年。", "unverified")]),
        case("absence", "holdout", ["The brief describes installation on Linux. It says nothing about Windows compatibility."], [
            ("该简报描述 Linux 安装。[e1]", "entailed"),
            ("该简报证明该软件无法在 Windows 上运行。[e1]", "unverified"),
            ("## 平台", "not_claim")]),
        case("comparison", "holdout", ["In benchmark J, model O had lower latency than model P. Accuracy and energy were not measured."], [
            ("在 J 基准中，O 比 P 延迟低。[e1]", "entailed"),
            ("在 J 基准中，O 比 P 延迟低，而且准确率也更高。[e1]", "partially"),
            ("P 的训练耗能是 O 的十倍。", "unverified")]),
        case("ci", "holdout", ["The estimated effect was 2 units, with a 95% confidence interval from -1 to 5. The interval includes zero."], [
            ("效果点估计为 2，95% 置信区间为 -1 至 5。[e1]", "entailed"),
            ("95% 置信区间完全大于零。[e1]", "contradicted"),
            ("该结果保证每个人都获益。", "unverified")]),
        case("snapshot", "holdout", ["This is the abstract of the Veka study. It reports faster response but provides no numerical effect size or study protocol."], [
            ("可用摘要报告响应更快。[e1]", "entailed"),
            ("我们已读完整篇论文，方法部分列出了双盲协议。[e1]", "unverified"),
            ("摘要报告响应更快，并证明长期安全。[e1]", "partially")], kind="abstract"),
        case("time", "holdout", ["As of 2024, Weno supports at most 6 clients. This document does not describe later releases."], [
            ("这份 2024 年材料称 Weno 最多支持 6 个客户端。[e1]", "entailed"),
            ("截至 2024 年 Weno 可以同时支持 9 个客户端。[e1]", "contradicted"),
            ("2026 年最新版本已支持 100 个客户端。", "unverified")]),
    ]
