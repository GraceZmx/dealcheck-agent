"""命令行评估一个合作案例。

用法（在项目根目录，先激活虚拟环境）：
    python scripts/evaluate.py examples/deals/2_negotiate_maya.json            # 仅规则引擎，不调用大模型
    python scripts/evaluate.py examples/deals/2_negotiate_maya.json --agent    # 完整评估：Agent 调查 + 邮件（会产生费用）
    python scripts/evaluate.py examples/deals/2_negotiate_maya.json --json     # 输出完整 JSON

使用 --agent 时，案例文件里的 agent_findings 会被忽略，由 Agent 实时调查；运行记录保存在 traces/（不上传 GitHub）。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_agent_config  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import create_llm_provider  # noqa: E402
from app.models import EvaluationRequest  # noqa: E402
from app.pipeline import FullEvaluationRequest, run_full_evaluation  # noqa: E402
from app.rules.engine import evaluate_deal  # noqa: E402
from app.search.base import SearchError  # noqa: E402
from app.search.factory import create_search_provider  # noqa: E402
from app.settings import load_settings  # noqa: E402

LIGHT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
DECISION = {"accept": "✅ ACCEPT", "negotiate": "🟠 NEGOTIATE", "decline": "⛔ DECLINE"}


def run_agent_mode(raw: dict, as_json: bool) -> None:
    raw["agent_findings"] = None
    req = FullEvaluationRequest.model_validate(raw)
    settings, config = load_settings(), load_agent_config()
    try:
        llm, search, reason = create_llm_provider(settings, config), create_search_provider(settings, config), None
    except (LLMError, SearchError) as e:
        llm, search, reason = None, None, str(e)

    full, trace = run_full_evaluation(req, llm, search, config, unavailable_reason=reason)

    traces = ROOT / "traces"
    traces.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", req.deal.brand_name.lower()).strip("-")
    trace_path = traces / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.json"
    trace_path.write_text(json.dumps({"result": full.model_dump(mode="json"), "trace": trace},
                                     ensure_ascii=False, indent=2), encoding="utf-8")

    if as_json:
        print(full.model_dump_json(indent=2))
        return

    print_evaluation(full.evaluation)
    a, u = full.agent, full.agent.usage
    print(f"Agent: {a.status}" + (f" ({a.error})" if a.error else "") + (f", finalized: {a.finalize_reason}" if a.finalize_reason else ""))
    print(f"  model {a.model} | {u['model_calls']} model calls, {u['searches']} searches, {u['fetches']} page reads"
          f" | {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens (reasoning {u['reasoning_tokens']:,})")
    print(f"  cost ¥{u['cost_cny']:.4f} (${u['cost_usd']:.5f}) | {u['elapsed_seconds']:.1f}s")
    if full.findings and full.findings.brand_summary:
        print(f"\nBrand summary: {full.findings.brand_summary}")
    if full.sources:
        print("\nSources")
        for src in full.sources:
            print(f"  [{src.id}] {src.title or ''} {src.url}")
    e = full.email
    print(f"\nEmail draft ({e.generated_by}{': ' + e.note if e.note else ''})")
    print(f"  Subject: {e.subject}\n")
    print("  " + e.body.replace("\n", "\n  "))
    print(f"\nTrace saved to {trace_path.relative_to(ROOT)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a brand deal.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--agent", action="store_true", help="run live Agent research and email drafting (costs money)")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    raw = json.loads(args.path.read_text(encoding="utf-8"))
    raw.pop("_description", None)
    if args.agent:
        run_agent_mode(raw, args.json)
        return

    result = evaluate_deal(EvaluationRequest.model_validate(raw))
    if args.json:
        print(result.model_dump_json(indent=2))
        return
    print_evaluation(result)


def print_evaluation(result) -> None:
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
