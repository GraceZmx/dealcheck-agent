"""评测集：读取案例、给单次运行打分、汇总指标（PRD 第 13.2、14 节；格式见 eval/README.md）。

这里只放纯函数，不调用模型。运行评测的命令行在 scripts/run_eval.py。
judge 类型的风险点、来源有效率、邮件质量需要 LLM 评审，这一版标为"待评审"，不计入自动指标。
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

SCORE_RANGES = {"accept": (75, 100), "negotiate": (50, 74), "decline": (0, 49)}
SEVERE_PAIRS = {("accept", "decline"), ("decline", "accept")}
REJECT_RULES = {"HR-1", "HR-2", "HR-3"}  # 拒绝类：决策必须是拒绝；其余是封顶类：决策不能是接受
CATEGORIES = ["accept", "negotiate", "decline", "hard_rule", "adversarial"]


class CaseError(ValueError):
    """案例文件不完整，或编号重复。"""


def load_cases(directories: list[Path]) -> list[dict]:
    """读取多个目录下的案例，按编号排序。编号重复、没有标准答案时报错。"""
    cases: dict[str, dict] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            case_id = case["case"]["id"]
            if case_id in cases:
                raise CaseError(f"案例编号重复：{case_id}（{path}）")
            if not case.get("expected", {}).get("decision"):
                raise CaseError(f"{case_id} 还没有标准答案（expected.decision 为空）")
            cases[case_id] = case
    return [cases[k] for k in sorted(cases)]


def select_cases(cases: list[dict], split: str = "all", ids: list[str] | None = None) -> list[dict]:
    if ids:
        known = {c["case"]["id"] for c in cases}
        unknown = sorted(set(ids) - known)
        if unknown:
            raise CaseError(f"找不到这些案例：{', '.join(unknown)}")
        return [c for c in cases if c["case"]["id"] in ids]
    return [c for c in cases if split == "all" or c["case"]["split"] == split]


def request_body(case: dict) -> dict:
    """接口需要的输入：只有 profile 和 deal，标准答案和标注信息一律不传。"""
    return {"profile": case["profile"], "deal": case["deal"]}


# ---------- 单次运行打分 ----------


def rule_respected(rule: str, decision: str | None) -> bool:
    """决策有没有遵守这条硬性规则的效果。不看规则编号，所以仅 Agent 配置也能比较"拦截"。"""
    if rule in REJECT_RULES:
        return decision == "decline"
    return decision in ("negotiate", "decline")


def _check_risk(check: dict, result: dict, hits: set[str]) -> bool | None:
    """自动检查一个风险点。返回 None 表示需要 LLM 评审，或这个配置没有对应输出（仅 Agent 没有条款灯和报价位置）。"""
    evaluation, findings = result["evaluation"], result.get("findings") or {}
    kind = check["type"]
    if kind == "hard_rule" and result.get("config") == "agent_only":
        return None
    if kind == "hard_rule":
        return check["rule"] in hits
    if kind in ("term_light", "pricing_position") and "contract" not in evaluation:
        return None
    if kind == "term_light":
        lights = {t["term"]: t["light"] for t in evaluation["contract"]["terms"]}
        return lights.get(check["term"]) == check["light"]
    if kind == "pricing_position":
        return evaluation["pricing"]["position"] == check["position"]
    if kind == "severe_flag":
        return check["flag"] in {f["type"] for f in findings.get("severe_flags", [])}
    if kind == "detected_category":
        return check["category"] in {c["category"] for c in findings.get("detected_categories", [])}
    if kind == "judge":
        return None
    raise CaseError(f"未知的检查类型：{kind}")


def grade_run(case: dict, result: dict) -> dict:
    """把一次运行结果（FullEvaluationResult 的 JSON）和标准答案比对。"""
    expected, evaluation = case["expected"], result["evaluation"]
    hits = {h["rule_id"] for h in evaluation["hard_rules"]}
    must_hit = expected["hard_rules"]["must_hit"]
    must_not_hit = expected["hard_rules"]["must_not_hit"]
    got, want = evaluation["decision"], expected["decision"]

    # 标准答案里有任何必须触发的硬性规则时，决策由规则决定，不检查分数
    score_ok = None
    if not must_hit and evaluation["total_score"] is not None:
        low, high = SCORE_RANGES[want]
        score_ok = low <= evaluation["total_score"] <= high

    # Agent 没有运行（跳过、不可用、仅规则配置）时没有实体判断，不计入
    should_confirm = expected["brand_entity"]["should_confirm"]
    findings = result.get("findings")
    entity_ok = None
    if should_confirm is not None and findings is not None:
        entity_ok = findings["brand_entity_confirmed"] == should_confirm

    risks = []
    for risk in expected["key_risks"]:
        ok = _check_risk(risk["check"], result, hits)
        risks.append({"id": risk["id"], "type": risk["check"]["type"], "ok": ok})

    usage = result.get("agent", {}).get("usage", {})
    return {
        "decision": got,
        "decision_ok": got == want,
        "severe_misjudgment": (want, got) in SEVERE_PAIRS,
        "score": evaluation["total_score"],
        "score_ok": score_ok,
        "hard_rules_hit": sorted(hits),
        "missed_rules": [r for r in must_hit if r not in hits],
        "rules_respected": [rule_respected(r, got) for r in must_hit],
        "false_rules": [r for r in must_not_hit if r in hits],
        "entity_ok": entity_ok,
        "risks": risks,
        "agent_status": result.get("agent", {}).get("status"),
        "config": result.get("config"),
        "cost_cny": usage.get("cost_cny", 0.0),
        "elapsed_seconds": usage.get("elapsed_seconds", 0.0),
    }


# ---------- 汇总 ----------


def _rate(values: list[bool]) -> dict:
    """比例指标：分母为 0 时 rate 为空，避免把"没有可算的案例"误报成 0% 或 100%。"""
    n = len(values)
    return {"passed": sum(values), "total": n, "rate": round(sum(values) / n, 4) if n else None}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(cases: list[dict], grades: dict[str, list[dict]]) -> dict:
    """grades：案例编号 → 每次重复运行的打分结果。决策类指标按"运行次数"计，稳定性按"案例"计。"""
    by_id = {c["case"]["id"]: c for c in cases}
    runs = [(by_id[cid], g) for cid, gs in grades.items() for g in gs]

    def decisions_for(pred) -> dict:
        return _rate([g["decision_ok"] for c, g in runs if pred(c)])

    risk_checks = [r["ok"] for _, g in runs for r in g["risks"] if r["ok"] is not None]
    term_checks = [r["ok"] for _, g in runs for r in g["risks"] if r["type"] == "term_light" and r["ok"] is not None]
    respected = [ok for _, g in runs for ok in g.get("rules_respected", [])]
    rule_id_runs = [(c, g) for c, g in runs if g.get("config") != "agent_only"]
    must_hit_total = sum(len(c["expected"]["hard_rules"]["must_hit"]) for c, _ in rule_id_runs)
    missed = sum(len(g["missed_rules"]) for _, g in rule_id_runs)
    repeated = {cid: gs for cid, gs in grades.items() if len(gs) > 1}
    costs = [g["cost_cny"] for _, g in runs]
    times = [g["elapsed_seconds"] for _, g in runs if g["agent_status"] not in (None, "unavailable", "skipped")]

    return {
        "cases": len(grades),
        "runs": len(runs),
        "decision_accuracy": decisions_for(lambda c: True),
        "decision_accuracy_by_split": {s: decisions_for(lambda c, s=s: c["case"]["split"] == s) for s in ("dev", "test")},
        "decision_accuracy_by_category": {
            k: decisions_for(lambda c, k=k: c["case"]["category"] == k) for k in CATEGORIES
        },
        "severe_misjudgments": sum(g["severe_misjudgment"] for _, g in runs),
        "hard_rule_interception": _rate(respected),
        "hard_rule_recall": {
            "passed": must_hit_total - missed, "total": must_hit_total,
            "rate": round((must_hit_total - missed) / must_hit_total, 4) if must_hit_total else None,
        },
        "false_hard_rules": sum(len(g["false_rules"]) for _, g in rule_id_runs),
        "term_light_accuracy": _rate(term_checks),
        "key_risk_auto_recall": _rate(risk_checks),
        "key_risks_pending_judge": sum(1 for _, g in runs for r in g["risks"] if r["ok"] is None),
        "brand_entity_accuracy": _rate([g["entity_ok"] for _, g in runs if g["entity_ok"] is not None]),
        "score_in_range": _rate([g["score_ok"] for _, g in runs if g["score_ok"] is not None]),
        "stability": _rate([len({g["decision"] for g in gs}) == 1 for gs in repeated.values()]),
        "agent_fallbacks": sum(1 for _, g in runs if g["agent_status"] in ("fallback", "unavailable", "limited")),
        "cost_cny": {
            "total": round(sum(costs), 4),
            "mean": round(statistics.mean(costs), 4) if costs else None,
        },
        "latency_seconds": {"p50": _percentile(times, 0.5), "p90": _percentile(times, 0.9)},
        "label_changes": sum(len(c["label"].get("history", [])) for c in cases if c["case"]["id"] in grades),
    }


# ---------- 报告 ----------


def _fmt(metric: dict) -> str:
    if metric["rate"] is None:
        return "—（没有可算的案例）"
    return f"{metric['rate']:.0%}（{metric['passed']}/{metric['total']}）"


def render_report(meta: dict, summary: dict, cases: list[dict], grades: dict[str, list[dict]]) -> str:
    """评测报告（Markdown）。只写案例编号和汇总数字，不写品牌名，可以整理后放进 docs/notes/。"""
    s = summary
    lines = [
        f"# 评测报告：{meta['config']}",
        "",
        f"- 时间：{meta['started_at']}；模型：{meta.get('model') or '—'}；每个案例运行 {meta['repeats']} 次",
        f"- 案例：{s['cases']} 个（{meta['selection']}），共 {s['runs']} 次运行",
        f"- 标签修改记录：{s['label_changes']} 条（跑系统前后的修改都算，见各案例 `label.history`）",
        "",
        "## 指标（PRD 13.2）",
        "",
        "| 指标 | 结果 | MVP 目标 |",
        "|---|---|---|",
        f"| 决策一致率 | {_fmt(s['decision_accuracy'])} | ≥ 80% |",
        f"| 　其中校准组 dev | {_fmt(s['decision_accuracy_by_split']['dev'])} | |",
        f"| 　其中测试组 test（方向性） | {_fmt(s['decision_accuracy_by_split']['test'])} | |",
        f"| 严重误判（接受↔拒绝） | {s['severe_misjudgments']} 次 | 0 |",
        f"| 硬性规则拦截率（决策遵守了规则效果） | {_fmt(s['hard_rule_interception'])} | 100% |",
        f"| 　硬性规则命中（规则编号出现在结果里） | {_fmt(s['hard_rule_recall'])} | |",
        f"| 不该触发却触发的硬性规则 | {s['false_hard_rules']} 次 | — |",
        f"| 条款判定准确率 | {_fmt(s['term_light_accuracy'])} | 100% |",
        f"| 关键风险点（自动检查部分） | {_fmt(s['key_risk_auto_recall'])} | — |",
        f"| 品牌实体识别准确率 | {_fmt(s['brand_entity_accuracy'])} | ≥ 95% |",
        f"| 分数落在决策区间内 | {_fmt(s['score_in_range'])} | — |",
        f"| 结果稳定性（多次运行决策一致） | {_fmt(s['stability'])} | ≥ 90% |",
        f"| Agent 降级次数 | {s['agent_fallbacks']} | — |",
        f"| 成本 | 合计 ¥{s['cost_cny']['total']}，平均 ¥{s['cost_cny']['mean']} | 单次 ≤ ¥2.00 |",
        f"| 时延 P50 / P90（只算 Agent 运行过的） | {s['latency_seconds']['p50']} / {s['latency_seconds']['p90']} 秒 | ≤ 45 / 90 秒 |",
        "",
        f"> 待 LLM 评审（这一版不计入）：{s['key_risks_pending_judge']} 个 judge 类风险点、来源有效率、邮件质量分。",
        "",
        "## 按类别",
        "",
        "| 类别 | 决策一致率 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in s["decision_accuracy_by_category"].items()]
    lines += [
        "",
        "## 每个案例",
        "",
        "| 案例 | 组 | 类别 | 标准答案 | 系统决策 | 没触发的规则 | 误触发的规则 | 没通过的自动检查 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    by_id = {c["case"]["id"]: c for c in cases}
    for cid, gs in grades.items():
        c = by_id[cid]
        failed = sorted({r["id"] for g in gs for r in g["risks"] if r["ok"] is False})
        missed = sorted({r for g in gs for r in g["missed_rules"]})
        false = sorted({r for g in gs for r in g["false_rules"]})
        decisions = " / ".join(("✓ " if g["decision_ok"] else "✗ ") + (g["decision"] or "无结果") for g in gs)
        lines.append(
            f"| {cid} | {c['case']['split']} | {c['case']['category']} | {c['expected']['decision']} | {decisions}"
            f" | {', '.join(missed) or '—'} | {', '.join(false) or '—'} | {', '.join(failed) or '—'} |"
        )
    return "\n".join(lines) + "\n"


def group_grades(items: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for case_id, grade in items:
        grouped[case_id].append(grade)
    return dict(sorted(grouped.items()))
