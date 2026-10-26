"""运行评测集并生成报告（PRD 第 14 节；案例格式见 eval/README.md）。

用法（在项目根目录，先激活虚拟环境）：
    python scripts/run_eval.py --config rules_only                    # 仅规则，不调用大模型，不花钱
    python scripts/run_eval.py --config hybrid --split dev --yes      # 混合架构，真实 Agent（会产生费用）
    python scripts/run_eval.py --config agent_only --yes              # 仅 Agent：调查后直接给决策，不经过规则（消融实验）
    python scripts/run_eval.py --config hybrid --cases R01,F02 --repeats 3 --yes
    python scripts/run_eval.py --rescore local/eval/runs/<目录>       # 不重新运行，用保存的结果重新打分

案例从 eval/cases/ 和 local/eval/cases/ 读取。运行结果里有真实品牌名，只写到 local/eval/runs/（不进 GitHub）。
模型只收到 profile 和 deal，看不到标准答案和标注信息。
"""

import argparse
import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.agent_only import make_context, run_agent_only  # noqa: E402
from app.agent.budget import Budget  # noqa: E402
from app.agent.tools import tiers_for  # noqa: E402
from app.config import load_agent_config, load_pricing_config  # noqa: E402
from app.evalset import (  # noqa: E402
    CaseError,
    grade_run,
    group_grades,
    load_cases,
    render_report,
    request_body,
    select_cases,
    summarize,
)
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import create_llm_provider  # noqa: E402
from app.models import EvaluationRequest  # noqa: E402
from app.pipeline import FullEvaluationRequest, run_full_evaluation  # noqa: E402
from app.rules.engine import evaluate_deal  # noqa: E402
from app.search.base import SearchError  # noqa: E402
from app.search.factory import create_search_provider  # noqa: E402
from app.settings import load_settings  # noqa: E402

CASE_DIRS = [ROOT / "eval" / "cases", ROOT / "local" / "eval" / "cases"]
RUNS_DIR = ROOT / "local" / "eval" / "runs"
CONFIGS = ("rules_only", "agent_only", "hybrid")
EST_COST_PER_RUN_CNY = 0.05  # M2 实测 deepseek-flash 约 ¥0.043/次，只用于运行前提示


def run_agent_only_case(req, llm, search, agent_config) -> tuple[dict, list[dict]]:
    pre = evaluate_deal(EvaluationRequest(profile=req.profile, deal=req.deal))  # 只取基准表区间，不把规则结果给 Agent
    budget = Budget(agent_config.budget, agent_config.pricing, llm.model)
    ctx = make_context(req, pre, search, agent_config, budget, tiers_for(req.profile, load_pricing_config()))
    return run_agent_only(req, pre.pricing, ctx, llm, agent_config)


def run_case(case: dict, config_name: str, providers: tuple) -> tuple[dict, list[dict]]:
    req = FullEvaluationRequest.model_validate(request_body(case))
    llm, search, agent_config, reason = providers
    if config_name == "agent_only":
        return run_agent_only_case(req, llm, search, agent_config)
    if config_name == "rules_only":
        result, trace = run_full_evaluation(req, None, None, agent_config, unavailable_reason="rules-only eval config")
    else:
        result, trace = run_full_evaluation(req, llm, search, agent_config, unavailable_reason=reason)
    return {"config": config_name, **result.model_dump(mode="json")}, trace


def make_providers(config_name: str, model: str | None) -> tuple:
    agent_config = load_agent_config()
    if config_name == "rules_only":
        return None, None, agent_config, None
    settings = load_settings()
    if model:
        settings = dataclasses.replace(settings, llm_model=model)
    try:
        return create_llm_provider(settings, agent_config), create_search_provider(settings, agent_config), agent_config, None
    except (LLMError, SearchError) as e:
        sys.exit(f"Agent 无法启动：{e}。混合架构评测需要可用的 Agent，否则结果等于仅规则。")


def write_outputs(out: Path, meta: dict, cases: list[dict], grades: dict) -> dict:
    summary = summarize(cases, grades)
    (out / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "grades": grades}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(render_report(meta, summary, cases, grades), encoding="utf-8")
    return summary


def run(args, cases: list[dict]) -> Path:
    selected = select_cases(cases, args.split, args.cases)
    n_runs = len(selected) * args.repeats
    if args.config != "rules_only" and not args.yes:
        sys.exit(f"将运行 {n_runs} 次真实 Agent，预计约 ¥{n_runs * EST_COST_PER_RUN_CNY:.2f}"
                 f"（按 deepseek-flash 估算，v4-pro 或开启思考会更贵）。确认后加 --yes 重新运行。")

    started = datetime.now()
    out = args.out or RUNS_DIR / f"{started:%Y%m%d-%H%M%S}-{args.config}"
    (out / "results").mkdir(parents=True, exist_ok=False)
    providers = make_providers(args.config, args.model)
    meta = {
        "config": args.config,
        "model": providers[0].model if providers[0] else None,
        "started_at": started.isoformat(timespec="seconds"),
        "repeats": args.repeats,
        "selection": ",".join(args.cases) if args.cases else f"split={args.split}",
    }

    items = []
    for case in selected:
        cid = case["case"]["id"]
        for n in range(1, args.repeats + 1):
            result, trace = run_case(case, args.config, providers)
            (out / "results" / f"{cid}-{n}.json").write_text(
                json.dumps({"result": result, "trace": trace}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            grade = grade_run(case, result)
            items.append((cid, grade))
            mark = "✓" if grade["decision_ok"] else "✗"
            print(f"{cid} #{n}: {mark} {grade['decision']}（标准答案 {case['expected']['decision']}）"
                  f" ¥{grade['cost_cny']:.4f} {grade['elapsed_seconds']:.1f}s")

    meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = write_outputs(out, meta, selected, group_grades(items))
    print_summary(summary, out)
    return out


def rescore(run_dir: Path, cases: list[dict]) -> None:
    """标准答案改了之后，不用重新花钱运行，直接用保存的结果重新打分。"""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    by_id = {c["case"]["id"]: c for c in cases}
    items = []
    for path in sorted((run_dir / "results").glob("*.json")):
        cid = path.stem.rsplit("-", 1)[0]
        if cid not in by_id:
            sys.exit(f"结果 {path.name} 对应的案例不存在")
        items.append((cid, grade_run(by_id[cid], json.loads(path.read_text(encoding="utf-8"))["result"])))
    grades = group_grades(items)
    meta["rescored_at"] = datetime.now().isoformat(timespec="seconds")
    summary = write_outputs(run_dir, meta, [by_id[cid] for cid in grades], grades)
    print_summary(summary, run_dir)


def print_summary(s: dict, out: Path) -> None:
    acc = s["decision_accuracy"]
    print(f"\n决策一致率 {acc['passed']}/{acc['total']}，严重误判 {s['severe_misjudgments']} 次，"
          f"硬性规则拦截 {s['hard_rule_interception']['passed']}/{s['hard_rule_interception']['total']}，"
          f"成本合计 ¥{s['cost_cny']['total']}")
    print(f"报告：{out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}/report.md")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DealCheck eval set.")
    parser.add_argument("--config", choices=CONFIGS, default="rules_only")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="all")
    parser.add_argument("--cases", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        help="只跑这些案例，逗号分隔，例如 R01,F02")
    parser.add_argument("--repeats", type=int, default=1, help="每个案例运行几次（稳定性指标用 3）")
    parser.add_argument("--model", help="覆盖模型名，例如 deepseek-v4-pro")
    parser.add_argument("--out", type=Path, help="结果目录（默认 local/eval/runs/<时间>-<配置>）")
    parser.add_argument("--yes", action="store_true", help="确认会产生费用（agent_only、hybrid 配置必须加）")
    parser.add_argument("--rescore", type=Path, help="用已保存的结果重新打分，不重新运行")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 至少为 1")

    try:
        cases = load_cases([d for d in CASE_DIRS if d.exists()])
        if args.rescore:
            rescore(args.rescore, cases)
        else:
            run(args, cases)
    except CaseError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
