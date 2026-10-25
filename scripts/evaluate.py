"""命令行评估一个合作案例（仅规则引擎，不调用大模型）。

用法（在项目根目录，先激活虚拟环境）：
    python scripts/evaluate.py examples/deals/2_negotiate_maya.json
    python scripts/evaluate.py examples/deals/2_negotiate_maya.json --json   # 输出完整 JSON
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.models import EvaluationRequest  # noqa: E402
from app.rules.engine import evaluate_deal  # noqa: E402

LIGHT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
DECISION = {"accept": "✅ ACCEPT", "negotiate": "🟠 NEGOTIATE", "decline": "⛔ DECLINE"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a brand deal with the rule engine.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    raw = json.loads(args.path.read_text(encoding="utf-8"))
    raw.pop("_description", None)
    result = evaluate_deal(EvaluationRequest.model_validate(raw))

    if args.json:
        print(result.model_dump_json(indent=2))
        return

    s, p = result.score, result.pricing
    print(f"\n{DECISION[result.decision.value]}   score {result.total_score}/100\n")
    for reason in result.reasons:
        print(f"  • {reason}")

    print("\nScore breakdown")
    for name, dim in (("Pricing", s.pricing), ("Contract", s.contract), ("Brand & fit", s.brand_fit)):
        adj = f"  ({dim.base} {dim.adjustment:+d})" if dim.adjustment else ""
        print(f"  {name:<12} {dim.final:>3}/{dim.max}{adj}")
    if s.total_cap_correction:
        print(f"  Cap correction {s.total_cap_correction:+d}")

    print(f"\nPricing: offer ${p.offer_usd:,.0f} vs benchmark P50 ${p.adjusted_benchmark.p50:,.0f}"
          f" → ratio {p.offer_ratio:.2f}, confidence {p.confidence.value}")
    if p.suggested_counter_range:
        print(f"  Suggested counter: ${p.suggested_counter_range.low:,.0f}–${p.suggested_counter_range.high:,.0f}")

    print("\nContract")
    for t in result.contract.terms:
        print(f"  {LIGHT[t.light.value]} {t.term.value}" + (f": {t.risk}" if t.risk else ""))

    if s.adjustments:
        print("\nAgent adjustments")
        for a in s.adjustments:
            print(f"  {a.item.value}: requested {a.requested:+d}, applied {a.applied:+d} ({a.status.value})")

    if result.hard_rules:
        print("\nHard rules")
        for h in result.hard_rules:
            print(f"  {h.rule_id} [{h.kind.value}] {h.message}")
            for url in h.evidence_urls:
                print(f"      ↳ {url}")
    print()


if __name__ == "__main__":
    main()
