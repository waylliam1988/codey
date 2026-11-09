"""Fixed mechanism-based cases for paired browser/Research model experiments."""
from __future__ import annotations

from dataclasses import dataclass

ROOT = "https://example.com/research-gate/"


@dataclass(frozen=True)
class Document:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"


@dataclass(frozen=True)
class BrowserResearchCase:
    name: str
    category: str
    task: str
    documents: dict[str, Document]
    expected: tuple[str, ...]
    review: str
    abstain: bool = False


def html(text: str) -> Document:
    return Document(("<!doctype html><html><head><meta charset='utf-8'><title>原始资料</title></head>"
                     "<body>" + text + "</body></html>").encode("utf-8"))


def pdf(text: str) -> Document:
    stream = f"BT /F1 12 Tf 40 750 Td ({text}) Tj ET".encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(body)
    body.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode())
    return Document(bytes(body), content_type="application/pdf")


def cases() -> tuple[BrowserResearchCase, ...]:
    locality = ("<article>地区实测记录。请在地区输入框填写福州，再加载记录。初始正文没有净重数值。</article>"
                "<label>地区<input id='q' aria-label='地区'></label>"
                "<button onclick=\"document.querySelector('#out').innerText=document.querySelector('#q').value==='福州'"
                "?'福州样本实测鱼丸净重为173克。':'尚未选中福州地区';\">加载记录</button><p id='out'>等待选择地区</p>")
    expand = ("<article>福州鱼丸实测报告，测量方法及结果在展开正文后显示。</article>"
              "<button onclick=\"document.querySelector('#detail').hidden=false\">展开全文</button>"
              "<article id='detail' hidden>本次鱼丸样本的去皮净重为241克；该数值不包含包装。</article>")
    seasonal = ("<article>季节样本记录。请选择冬季记录后核对净重。当前为夏季记录：净重83克。</article>"
                "<button onclick=\"document.querySelector('#record').innerText='冬季鱼丸样本净重为159克。';\">冬季记录</button>"
                "<button onclick=\"document.querySelector('#record').innerText='夏季鱼丸样本净重为83克。';\">夏季记录</button>"
                "<p id='record'>请选择季节</p>")
    return (
        BrowserResearchCase("dynamic_generic", "dynamic", f"查看 {ROOT}locality 中福州样本鱼丸的净重，只使用该来源。",
            {ROOT + "locality": html(locality)}, ("173",), "不能把未选地区误述为不存在记录；应填写福州并捕获展开正文。"),
        BrowserResearchCase("dynamic_input", "dynamic", f"在 {ROOT}locality 输入福州并加载地区记录，报告实际鱼丸净重及引用。",
            {ROOT + "locality": html(locality)}, ("173",), "自然操作目标，无工具名或 ref 提示。核对地区、数值和对应 passage。"),
        BrowserResearchCase("dynamic_expand", "dynamic", f"展开并研究 {ROOT}expand 的完整报告，报告去皮净重及是否包含包装。",
            {ROOT + "expand": html(expand)}, ("241",), "净重241克，不含包装，必须来自展开正文。"),
        BrowserResearchCase("dynamic_select", "dynamic", f"在 {ROOT}seasonal 选择冬季记录，报告冬季净重，不能用夏季数值代替。",
            {ROOT + "seasonal": html(seasonal)}, ("159",), "冬季159克；不能引用初始夏季83克充当冬季证据。"),
        BrowserResearchCase("research_conditions", "research", f"根据 {ROOT}policy 说明公开和私有项目分别有哪些配额，保留例外。",
            {ROOT + "policy": html("<article>配额说明。公开项目默认配额为77个。私有项目默认配额为14个。"
                                  "例外：经批准的教学私有项目适用公开项目的77个配额。其他私有项目不适用此例外。</article>")},
            ("77", "14"), "必须保留教学私有项目需批准的例外，不能泛化到所有私有项目。"),
        BrowserResearchCase("research_expand_exception", "research", f"研究 {ROOT}eligibility 的资格条件和完整例外，说明 AND 与 OR 的区别。",
            {ROOT + "eligibility": html("<article>资格说明。年龄满21岁，或持有许可证且有9个月经验，满足任一路径即可。"
                "附加例外需要展开后核对。</article><button onclick=\"document.querySelector('#ex').hidden=false\">展开例外</button>"
                "<p id='ex' hidden>例外：危险作业必须同时持有许可证和9个月经验；年满21岁不能单独豁免。</p>")},
            ("21", "9"), "必须核对危险作业例外；年龄 OR (许可证 AND 经验)，危险作业不能只凭年龄。"),
        BrowserResearchCase("research_conflict", "research", f"对照 {ROOT}release-old 和 {ROOT}release-new 的测量，说明差异与版本，不能平均。",
            {ROOT + "release-old": html("<article>第一版测量记录。版本A对同一标准样本测得17克，测量日期为2025年。</article>"),
             ROOT + "release-new": html("<article>第二版测量记录。版本B于2026年修正仪器后测得23克。原17克来自旧校准，不适用新仪器。</article>")},
            ("17", "23"), "两个独立原文及版本差异都有引用，不能平均为20或声称数值无冲突。"),
        BrowserResearchCase("research_long_exception", "research", f"查阅 {ROOT}long 的默认保留期和法律保全例外，说明何时不能按默认期删除。",
            {ROOT + "long": html("<article><p>默认保留期为46天。</p>" + "<p>背景：记录由管理员按既定流程核对。</p>" * 500
                + "<h2>法律保全例外</h2><p>收到法律保全通知后，必须保留到通知解除，不能按46天默认期限删除。</p></article>")},
            ("46",), "关键例外在长正文后部；不可因初始窗口没看到就推断无例外。"),
        BrowserResearchCase("control_static", "control", f"根据 {ROOT}static 报告实测净重及记录日期。",
            {ROOT + "static": html("<article>静态样本原始记录。2026年10月15日，样本去皮净重为61克，已扣除包装。</article>")},
            ("61", "2026"), "不能改变净重、日期或包装范围。"),
        BrowserResearchCase("control_pdf", "control", f"阅读 {ROOT}sample.pdf 并引用实测数值；不要用网页工具猜测。",
            {ROOT + "sample.pdf": pdf("Official measured quantity is exactly 37 grams. Packaging is excluded.")},
            ("37",), "PDF仍走原字节下载/页提取合同，不能依赖保留网页页签。"),
        BrowserResearchCase("control_captured", "control", f"在 {ROOT}captured 的原始记录中定位“原始值”，引用其数值，保持同一正文快照。",
            {ROOT + "captured": html("<article>原始记录的说明。" + "<p>背景资料用于核对记录来源及适用条件。</p>" * 350
                                     + "<p>原始值：样本净重为87克，未经过换算。</p></article>")},
            ("87",), "读取/定位属于原捕获；独立版本与重启合同另有确定性贯穿测试。"),
        BrowserResearchCase("control_restricted", "control", f"仅根据 {ROOT}restricted 判断是否能确定样本净重，不可读取时如实说明。",
            {ROOT + "restricted": Document(b"<html><body>Access restricted. Verification required.</body></html>", status=403)},
            (), "不能把限制页当正文或虚构数值；如实不足是正确完成。", abstain=True),
    )
