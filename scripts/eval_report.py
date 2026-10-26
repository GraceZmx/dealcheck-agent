"""把三组评测（仅规则 / 仅 Agent / 混合）合成一份中文对比报告（PRD 14.3）。

用法：
    python scripts/eval_report.py --rules <目录> --agent <目录> --hybrid <目录> [--out local/eval/results/report.md]

报告用答题卡题号（第几题）指代案例；题号对照表 local/eval/answer_sheet_key.json 不存在时用案例编号。
报告里有品牌名相关的结论原文和来源链接，只写到 local/（不进 GitHub）。
"""

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.evalset import load_cases, summarize  # noqa: E402

CASE_DIRS = [ROOT / "eval" / "cases", ROOT / "local" / "eval" / "cases"]
KEY = ROOT / "local" / "eval" / "answer_sheet_key.json"
REASONS = ROOT / "local" / "eval" / "results" / "wrong_reasons.json"  # 人工写的错题原因（题号 → 一句话），没有时用自动草稿
DECISION_ZH = {"accept": "接受", "negotiate": "谈判", "decline": "拒绝", None: "无结果"}
CONFIG_ZH = {"rules_only": "仅规则", "agent_only": "仅 Agent", "hybrid": "混合"}
SAMPLE_SOURCES = 10
SEED = 20261026


def load_run(run_dir: Path) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    results = {p.stem: json.loads(p.read_text(encoding="utf-8"))["result"] for p in (run_dir / "results").glob("*.json")}
    checks = {}
    if (run_dir / "source_check").exists():
        checks = {p.stem: json.loads(p.read_text(encoding="utf-8"))["claims"] for p in (run_dir / "source_check").glob("*.json")}
    source_summary = None
    if (run_dir / "source_check.json").exists():
        source_summary = json.loads((run_dir / "source_check.json").read_text(encoding="utf-8"))
    return {"dir": run_dir, "meta": summary["meta"], "grades": summary["grades"], "results": results,
            "checks": checks, "source_summary": source_summary}


def pct(metric: dict) -> str:
    return "—" if metric["rate"] is None else f"{metric['rate']:.0%}（{metric['passed']}/{metric['total']}）"


def label_for(case_id: str, key: dict[str, str]) -> str:
    q = next((q for q, cid in key.items() if cid == case_id), None)
    return f"第 {q} 题" if q else case_id


def why(grade: dict, result: dict) -> str:
    """一句话原因（自动草稿）：先看硬性规则，再看系统给的第一条理由。"""
    ev = result["evaluation"]
    parts = []
    if grade["missed_rules"] and result.get("config") != "agent_only":
        parts.append(f"没触发 {', '.join(grade['missed_rules'])}")
    if grade["false_rules"]:
        parts.append(f"误触发 {', '.join(grade['false_rules'])}")
    if ev.get("hard_rules") and not parts:
        parts.append("触发 " + ", ".join(h["rule_id"] for h in ev["hard_rules"]))
    if ev.get("total_score") is not None:
        parts.append(f"总分 {ev['total_score']}")
    if ev.get("reasons"):
        parts.append(ev["reasons"][0][:120])
    if result.get("agent", {}).get("status") in ("fallback", "unavailable"):
        parts.append(f"Agent {result['agent']['status']}")
    return "；".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine three eval configs into one report.")
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--hybrid", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "local" / "eval" / "results" / "report.md")
    parser.add_argument("--reasons", type=Path, default=REASONS, help="人工错题原因文件；不存在时用自动草稿")
    args = parser.parse_args()

    cases = load_cases([d for d in CASE_DIRS if d.exists()])
    by_id = {c["case"]["id"]: c for c in cases}
    key = json.loads(KEY.read_text(encoding="utf-8")) if KEY.exists() else {}
    runs = {name: load_run(d.resolve()) for name, d in (("rules_only", args.rules), ("agent_only", args.agent), ("hybrid", args.hybrid))}

    lines = ["# DealCheck 评测报告（M4）", ""]
    lines += [f"- {CONFIG_ZH[n]}：`{r['dir'].relative_to(ROOT)}`，模型 {r['meta'].get('model') or '—'}，"
              f"每个案例 {r['meta']['repeats']} 次" for n, r in runs.items()]
    total_cost = sum(sum(g["cost_cny"] for gs in r["grades"].values() for g in gs) for r in runs.values())
    judge_cost = sum((r["source_summary"] or {}).get("cost_cny", 0) for r in runs.values())
    history = [h for c in cases for h in c["label"].get("history", [])]
    after_run = sum(1 for h in history if h.get("after_seeing_output"))
    lines += [f"- 评测运行花费合计 ¥{total_cost:.2f}，来源检查（LLM 评审）¥{judge_cost:.2f}",
              f"- 标签修改记录：{len(history)} 条（第一次运行之前 {len(history) - after_run} 条，"
              f"看过系统输出之后 {after_run} 条）",
              "- **测试组只有 10 个案例，结果是方向性的。**", ""]

    lines += ["## 1. 三组对比", "",
              "| 指标 | 组 | " + " | ".join(CONFIG_ZH[n] for n in runs) + " |",
              "|---|---|" + "---|" * len(runs)]
    split_summaries = {}
    for name, r in runs.items():
        for split in ("dev", "test"):
            ids = [cid for cid in r["grades"] if by_id[cid]["case"]["split"] == split]
            split_summaries[(name, split)] = summarize([by_id[i] for i in ids], {i: r["grades"][i] for i in ids})
    rows = [
        ("决策一致率", lambda s: pct(s["decision_accuracy"])),
        ("严重误判（接受↔拒绝）", lambda s: f"{s['severe_misjudgments']} 次"),
        ("硬性规则拦截率", lambda s: pct(s["hard_rule_interception"])),
        ("结果稳定性（3 次决策一致）", lambda s: pct(s["stability"])),
        ("平均成本 / 次", lambda s: f"¥{s['cost_cny']['mean']}"),
        ("时延 P50 / P90", lambda s: "—（不调用模型）" if s["latency_seconds"]["p50"] is None
         else f"{s['latency_seconds']['p50']:.1f} / {s['latency_seconds']['p90']:.1f} 秒"),
    ]
    for title, fmt in rows:
        for split, zh in (("dev", "校准组"), ("test", "测试组")):
            lines.append(f"| {title} | {zh} | " + " | ".join(fmt(split_summaries[(n, split)]) for n in runs) + " |")
    support = []
    for name, r in runs.items():
        ss = r["source_summary"]
        support.append("—（没有来源）" if not ss or not ss["claims"] else
                       f"{ss['support_rate']:.0%}（{ss['supported']}/{ss['claims']}；部分支持 {ss['partial']}，"
                       f"不支持 {ss['unsupported']}，编号不存在 {ss['no_source']}）")
    lines.append("| 来源支持率 | 全部 | " + " | ".join(support) + " |")
    lines += ["", "> 硬性规则拦截率按\"决策是否遵守规则效果\"计算（拒绝类必须拒绝，封顶类不能接受），三组可以直接比较。"
              "仅 Agent 没有规则和条款灯，相关自动检查不适用。来源支持率只检查了混合每个案例的第 1 次运行和仅 Agent 的全部运行。", ""]

    lines += ["## 2. 错题清单（混合）", "", "| 题号 | 组 | 我的答案 | 系统答案（3 次） | 一句话原因 |", "|---|---|---|---|---|"]
    hybrid = runs["hybrid"]
    manual = json.loads(args.reasons.read_text(encoding="utf-8")) if args.reasons.exists() else {}
    for cid, gs in hybrid["grades"].items():
        if all(g["decision_ok"] for g in gs):
            continue
        c = by_id[cid]
        wrong = next(i for i, g in enumerate(gs, 1) if not g["decision_ok"])
        q = next((q for q, v in key.items() if v == cid), None)
        reason = manual.get(q) or why(gs[wrong - 1], hybrid["results"][f"{cid}-{wrong}"])
        reason = reason.replace("|", "/")
        lines.append(f"| {label_for(cid, key)} | {c['case']['split']} | {DECISION_ZH[c['expected']['decision']]} | "
                     f"{' / '.join(DECISION_ZH[g['decision']] for g in gs)} | {reason} |")
    for name in ("rules_only", "agent_only"):
        wrong = [label_for(cid, key) + f"（{DECISION_ZH[by_id[cid]['expected']['decision']]}→{DECISION_ZH[gs[0]['decision']]}）"
                 for cid, gs in runs[name]["grades"].items() if not gs[0]["decision_ok"]]
        lines += ["", f"**{CONFIG_ZH[name]}答错的题**：{'、'.join(wrong) or '无'}"]

    lines += ["", "## 3. 随机 10 条来源，请人工核对", "",
              "评审结论：supported = 支持，partial = 部分支持，unsupported = 不支持，no_source = 编号不存在。"
              "请点开链接，在\"你的判断\"一栏填 支持 / 部分 / 不支持。", ""]
    pool = []
    for name in ("hybrid", "agent_only"):
        r = runs[name]
        for stem, claims in sorted(r["checks"].items()):
            sources = {s["id"]: s for s in r["results"][stem].get("sources", [])}
            for c in claims:
                pool.append((name, stem, c, sources))
    random.Random(SEED).shuffle(pool)
    for i, (name, stem, c, sources) in enumerate(pool[:SAMPLE_SOURCES], 1):
        cid = stem.rsplit("-", 1)[0]
        links = "、".join(f"[{sid}]({sources[sid]['url']})" if sid in sources else f"{sid}（不存在）" for sid in c["source_ids"])
        lines += [f"### {i}. {label_for(cid, key)}（{CONFIG_ZH[name]}）",
                  f"- 结论：{c['text']}", f"- 来源：{links}",
                  f"- 评审：{c['verdict']}（{c.get('reason', '')}）", "- 你的判断：", ""]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{args.out}")


if __name__ == "__main__":
    main()
