"""Frozen task-level gold and unselected source documents, not model scripts."""

from __future__ import annotations


def document(project, name, facts, *, padding=58, decoy=False):
    intro = (f"# {project}: {name}\n\nThis document is a frozen engineering record. "
             "Consult the actual rules below before drawing conclusions; catalogue labels alone establish no rule.\n\n")
    body = intro + "\n".join(facts) + "\n\n## Archive annotations\n"
    for i in range(padding):
        prefix = f"{project} remote export token administrator offline performance " if decoy else "Archive annotation "
        body += f"{prefix}{i:03}: a historical filing index, not an operational rule or measured deployment result.\n"
    return {"url": f"https://example.org/retention/{project.lower().replace(' ', '-')}/{name.lower().replace(' ', '-')}",
            "title": f"{project} {name}", "text": body}


def early(identity, project, split, *, padding=58, decoy=False):
    restriction = f"For {project}, remote export requires BOTH a valid token AND an administrator role."
    offline = f"{project} remote export is unavailable offline, even with a token and an administrator role."
    local = f"{project} has a fast local preview, but this local demonstration does not measure remote export performance."
    return {"id": identity, "split": split,
        "question": f"请先搜索并核查 {project} 的部署资料，用中文回答 remote export 需要哪些条件（token、administrator），"
            "offline 时能否使用，fast local preview 是否证明远程生产环境更快。保留全部条件与证据边界，引用实际原文；"
            "另列来源对照表：Deployment restrictions、Feature overview、Release catalogue 各自明确说了什么；"
            "Release catalogue 是否给出了操作规则？必须先分别实际读取这三类资料，再交完整答案，不能只读一份就回答。"
            "不使用材料外知识。",
        "documents": [document(project, "Deployment restrictions", [restriction, offline], padding=padding),
            document(project, "Feature overview", [local], padding=padding),
            document(project, "Release catalogue", ["This catalogue contains filing labels and no operational policy."],
                     padding=padding, decoy=decoy)],
        "keys": [{"quote": restriction, "name": "both_conditions"}, {"quote": offline, "name": "offline_exception"},
                 {"quote": local, "name": "measurement_scope"}],
        "rubric": ["remote export requires token AND administrator", "offline still unavailable",
                   "fast local preview does not prove remote production speed", "all three sources read and correctly compared; catalogue has no operational rule"],
        "forbidden": ["token alone sufficient", "remote works offline", "local demo proves remote speed"]}


def cases():
    dev = early("dev_early", "Orin R2", "dev")
    fit = early("dev_fit", "Pavo S3", "dev", padding=0)
    held = early("held_early", "Iven V4", "holdout")
    decoy = early("held_decoy", "Zerin L3", "holdout", decoy=True)
    control = early("held_fit", "Mavo T5", "holdout", padding=0)
    versions = {"id": "held_versions", "split": "holdout",
        "question": "请先搜索并读取 Vela 1 和 Vela 2 的版本说明及迁移资料，用中文逐项比较 local/remote writer 上限、"
            "license flag 的影响以及只读支持版本。分别列出两份 Version specification 与 Migration catalogue 的来源对照表，"
            "说明迁移目录是否提供版本规则。必须实际读取这三份材料，不要只读一份就回答。不要将不同版本的规则混用；引用实际读取原文。",
        "documents": [document("Vela 1", "Version specification", [
            "Vela 1 permits 5 local writers only when the license flag is active; remote mode permits 2 writers independently of that flag.",
            "Vela 1 does not support read-only opening."]),
            document("Vela 2", "Version specification", [
            "Vela 2 permits 9 local writers with the license flag, and 3 local writers without it; remote mode permits 4 independently of the flag.",
            "Vela 2 supports read-only opening."]),
            document("Vela", "Migration catalogue", ["This migration catalogue provides historical filing labels, not version rules."])],
        "rubric": ["V1 local 5 only with flag; no invented inactive limit", "V1 remote 2 independent of flag",
                   "V2 local 9 with flag or 3 without", "V2 remote 4 independent of flag", "only V2 read-only",
                   "all three sources read; migration catalogue supplies no version rules"],
        "forbidden": ["version mixing", "remote depends on flag", "V1 supports read-only"]}
    versions["keys"] = [{"name": f"version_rule_{i}", "quote": fact} for i, doc in enumerate(versions["documents"][:2])
                        for fact in doc["text"].splitlines() if fact.startswith("Vela")]
    middle = early("held_middle", "Tarin M6", "holdout")
    middle_doc = middle["documents"][0]
    critical = [k["quote"] for k in middle["keys"][:2]]
    lead = document("Tarin M6", "Handbook", ["## Overview\nThe handbook separates background from deployment restrictions."], padding=125)
    middle_doc["text"] = lead["text"] + "\n## Remote deployment restrictions\n" + "\n".join(critical) + "\n"
    middle_doc["title"] = "Tarin M6 long deployment handbook"
    logic = {"id": "held_logic", "split": "holdout",
        "question": "请先搜索并读取 Seno Q7 的访问规则，用中文回答只读打开的完整替代条件：A 和 B 是否必须同时成立？"
            "key 是否足以保证导出？不要混淆必要条件与充分条件，引用实际原文。",
        "documents": [document("Seno Q7", "Access specification", [
            "Seno Q7 read-only opening succeeds if EITHER readable sidecars exist OR the directory is writable; A and B need not both hold.",
            "A valid key is necessary for Seno Q7 export, but not sufficient: an approved account is also required."], padding=0)],
        "rubric": ["read-only A OR B, not AND", "key necessary not sufficient; approved account also required"],
        "forbidden": ["A AND B required", "key alone sufficient"]}
    logic["keys"] = [{"name": f"logic_{i}", "quote": line} for i, line in enumerate(logic["documents"][0]["text"].splitlines())
                     if line.startswith(("Seno Q7 read-only", "A valid key"))]
    return [dev, fit, held, middle, versions, decoy, control, logic]
