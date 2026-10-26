"""对比同一配置的两轮评测（改 Prompt / 规则前后），输出中文对比报告。

用法：
    python scripts/compare_runs.py --old hybrid=<目录> --new hybrid=<目录> [--old rules_only=<目录> --new rules_only=<目录> ...]
        [--out local/eval/results/compare.md]

逐组列出校准组 / 测试组的核心指标变化，以及每题答对次数的变化（重点：原来对的题有没有被改错）。
报告里有题号对应的案例结论，只写到 local/（不进 GitHub）。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.evalset import load_cases, summarize  # noqa: E402
from eval_report import CASE_DIRS, CONFIG_ZH, DECISION_ZH, KEY, label_for, load_run, pct  # noqa: E402

SPLITS = (("dev", "校准组"), ("test", "测试组"))


def parse_pairs(items: list[str]) -> dict[str, Path]:
    pairs = {}
    for item in items:
        name, _, path = item.partition("=")
        if name not in CONFIG_ZH or not path:
            raise SystemExit(f"格式应为 <配置>=<目录>，配置是 {'/'.join(CONFIG_ZH)}：{item}")
        pairs[name] = Path(path)
    return pairs


def shown(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def correct(grades: list[dict]) -> int:
    return sum(g["decision_ok"] for g in grades)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two eval rounds of the same configs.")
    parser.add_argument("--old", action="append", required=True, help="<配置>=<旧结果目录>，可重复")
    parser.add_argument("--new", action="append", required=True, help="<配置>=<新结果目录>，可重复")
    parser.add_argument("--out", type=Path, default=ROOT / "local" / "eval" / "results" / "compare.md")
    args = parser.parse_args()

    old, new = parse_pairs(args.old), parse_pairs(args.new)
    if old.keys() != new.keys():
        raise SystemExit("--old 和 --new 的配置必须一一对应")
    cases = load_cases([d for d in CASE_DIRS if d.exists()])
    by_id = {c["case"]["id"]: c for c in cases}
    key = json.loads(KEY.read_text(encoding="utf-8")) if KEY.exists() else {}

    lines = ["# DealCheck 评测：两轮对比", ""]
    for name in old:
        o, n = load_run(old[name].resolve()), load_run(new[name].resolve())
        lines += [f"## {CONFIG_ZH[name]}", "",
                  f"- 旧：`{shown(o['dir'])}`（每个案例 {o['meta']['repeats']} 次）",
                  f"- 新：`{shown(n['dir'])}`（每个案例 {n['meta']['repeats']} 次）", "",
                  "| 指标 | 组 | 旧 | 新 |", "|---|---|---|---|"]
        summaries = {}
        for tag, r in (("old", o), ("new", n)):
            for split, _ in SPLITS:
                ids = [cid for cid in r["grades"] if by_id[cid]["case"]["split"] == split]
                summaries[(tag, split)] = summarize([by_id[i] for i in ids], {i: r["grades"][i] for i in ids})
        rows = [("决策一致率", lambda s: pct(s["decision_accuracy"])),
                ("严重误判（接受↔拒绝）", lambda s: f"{s['severe_misjudgments']} 次"),
                ("硬性规则拦截率", lambda s: pct(s["hard_rule_interception"])),
                ("结果稳定性（3 次决策一致）", lambda s: pct(s["stability"]) if s["stability"]["total"] else "—")]
        for title, fmt in rows:
            for split, zh in SPLITS:
                lines.append(f"| {title} | {zh} | {fmt(summaries[('old', split)])} | {fmt(summaries[('new', split)])} |")
        support = [("—" if not r["source_summary"] or not r["source_summary"]["claims"] else
                    f"{r['source_summary']['support_rate']:.0%}（{r['source_summary']['supported']}/{r['source_summary']['claims']}）")
                   for r in (o, n)]
        lines += [f"| 来源支持率（LLM 评审） | 全部 | {support[0]} | {support[1]} |", ""]

        lines += ["**每题答对次数变化**（只列有变化的题；↓ = 原来对的次数变少）：", "",
                  "| 题号 | 组 | 我的答案 | 旧（答对 / 次数） | 新（答对 / 次数） | 新的系统答案 |", "|---|---|---|---|---|---|"]
        changed = 0
        for cid in sorted(n["grades"], key=lambda c: (by_id[c]["case"]["split"], c)):
            og, ng = o["grades"].get(cid, []), n["grades"][cid]
            if not og:
                continue
            o_rate, n_rate = correct(og) / len(og), correct(ng) / len(ng)
            if o_rate == n_rate:
                continue
            changed += 1
            arrow = "↑" if n_rate > o_rate else "↓"
            lines.append(f"| {label_for(cid, key)} {arrow} | {by_id[cid]['case']['split']} | "
                         f"{DECISION_ZH[by_id[cid]['expected']['decision']]} | {correct(og)}/{len(og)} | {correct(ng)}/{len(ng)} | "
                         f"{' / '.join(DECISION_ZH[g['decision']] for g in ng)} |")
        if not changed:
            lines.append("| — | — | — | — | — | 没有变化 |")
        lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"对比报告：{args.out}")


if __name__ == "__main__":
    main()
