"""Held-out mechanisms for observation defaults and controlled search guidance."""
from __future__ import annotations

from codey.types.json_value import JsonObject
from tools.browser_research_cases import ROOT, BrowserResearchCase, html


def policy_cases() -> tuple[BrowserResearchCase, ...]:
    slow = ("<app-root><main>Loading...</main></app-root><script>setTimeout(() => "
            "document.querySelector('main').textContent='原始测量记录：冬季样本去皮净重327克，不含包装。', 5500)</script>")
    multi = ("<article>样本分两步揭示；先打开适用条件，再打开实测结果。</article>"
             "<button onclick=\"document.querySelector('#condition').hidden=false\">适用条件</button>"
             "<section id='condition' hidden>仅批准的教学项目可使用公开配额，其他私有项目不适用。"
             "<button onclick=\"document.querySelector('#measurement').hidden=false\">实测结果</button></section>"
             "<p id='measurement' hidden>公开配额为57个；私有配额为11个。</p>")
    return (
        BrowserResearchCase("policy_slow_hydration", "dynamic", f"查看 {ROOT}slow 的冬季样本净重和包装范围，引用原文。",
                            {ROOT + "slow": html(slow)}, ("327",), "等待策略应取得实际正文；重复导航会重置加载计时。"),
        BrowserResearchCase("policy_multistep", "research", f"研究 {ROOT}multistep 的两步完整记录，说明配额和教学例外。",
                            {ROOT + "multistep": html(multi)}, ("57", "11"), "必须保留中间出现但后续不再变化的批准条件。"),
        BrowserResearchCase("policy_ranked_search", "research", "核对星桥配额规范版本B的公开配额、私有配额和教学例外，引用原始材料。",
            {ROOT + "summary": html("<article>第三方早期摘要：星桥版本A公开配额68个，私有配额13个，未核对例外。</article>"),
             ROOT + "official-b": html("<article>星桥官方规范版本B：公开配额73个，私有配额19个。"
                "例外：经批准的教学私有项目适用73个配额；普通私有项目不能使用该例外。</article>")},
            ("73", "19"), "第一条是旧摘要；直接核对版本B原文及批准条件。"),
        BrowserResearchCase("policy_followup_search", "research", "研究星桥归档规则：默认保留期和法律保全例外，明确什么时候不能删除。",
            {ROOT + "retention": html("<article>星桥官方一般归档规则。默认保留期为52天。特殊情形应另查法律保全说明。</article>"),
             ROOT + "hold": html("<article>星桥官方法律保全说明。收到保全通知后必须保留到通知解除，"
                                   "即使超过52天也不能删除。解除后再按普通流程处理。</article>")},
            ("52",), "需要沿精确缺口补搜，不能将默认52天泛化到保全期间。"),
    )


def search_hits(case: BrowserResearchCase, query: str, limit: int) -> list[JsonObject]:
    """Fixed leads isolate query/ranking choices; they do not claim live SERP coverage."""
    rows: list[JsonObject]
    if case.name == "policy_ranked_search":
        rows = [
            {"url": ROOT + "summary", "title": "星桥配额热门摘要（早期版本A）", "snippet": "公开68，私有13；摘要未核对例外。"},
            {"url": ROOT + "official-b", "title": "星桥官方配额规范 · 版本B", "snippet": "最新B版公开、私有和教学批准例外。"},
        ]
    elif case.name == "policy_followup_search":
        if any(term in query for term in ("保全", "例外", "hold", "exception")):
            rows = [{"url": ROOT + "hold", "title": "星桥官方法律保全说明", "snippet": "通知解除前的特殊保留规则。"}]
        else:
            rows = [{"url": ROOT + "retention", "title": "星桥官方一般归档规则", "snippet": "默认期限和特殊情形索引。"}]
    else:
        raise ValueError("no controlled search fixture for " + case.name)
    return rows[:limit]
