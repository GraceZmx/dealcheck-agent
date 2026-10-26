"""来源支持度检查：对一次评测运行的结果，用 LLM 评审判断每条结论和引用来源是否一致。

用法（在项目根目录，先激活虚拟环境）：
    python scripts/check_sources.py local/eval/runs/<目录> --yes                  # 会产生费用
    python scripts/check_sources.py local/eval/runs/<目录> --first-repeat --yes   # 每个案例只查第 1 次运行

结果写到 <目录>/source_check/ 和 <目录>/source_check.json。中断后重新运行会跳过已完成的结果。
"""

import argparse
import dataclasses
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.budget import Budget  # noqa: E402
from app.config import load_agent_config  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import create_llm_provider  # noqa: E402
from app.settings import load_settings  # noqa: E402
from app.source_check import (  # noqa: E402
    JUDGE_OPTIONS,
    build_judge_messages,
    extract_claims,
    merge_verdicts,
    parse_verdicts,
    source_texts,
    split_claims,
    summarize_support,
)

JUDGE_MODEL = "deepseek-v4-pro"  # 和被测的 deepseek-flash 不是同一个模型，但同一家公司
WORKERS = 5
WORST_CALL_CNY = 0.25


def check_one(path: Path, llm, config) -> tuple[list[dict], float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    claims = extract_claims(data["result"])
    sources = source_texts(data["trace"])
    judged, no_source = split_claims(claims, sources)
    if not judged:
        return merge_verdicts([], no_source, {}), 0.0
    budget = Budget(config.budget, config.pricing, JUDGE_MODEL)
    messages = build_judge_messages(judged, sources)
    for attempt in range(2):  # 格式不对时重试一次
        response = llm.chat(messages, options=JUDGE_OPTIONS)
        budget.record_llm(response.usage)
        try:
            return merge_verdicts(judged, no_source, parse_verdicts(response.message.content, judged)), budget.cost_cny
        except (ValueError, KeyError) as e:
            last = e
    raise ValueError(f"{path.name}: 评审两次输出格式都不对（{last}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-as-judge source support check for an eval run.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--first-repeat", action="store_true", help="每个案例只检查第 1 次运行")
    parser.add_argument("--max-cost", type=float, default=8.0, help="花费上限（人民币）")
    parser.add_argument("--yes", action="store_true", help="确认会产生费用")
    args = parser.parse_args()
    if not args.yes:
        parser.error("会产生费用，确认后加 --yes")

    out = args.run_dir / "source_check"
    out.mkdir(exist_ok=True)
    paths = sorted((args.run_dir / "results").glob("*.json"))
    if args.first_repeat:
        paths = [p for p in paths if p.stem.endswith("-1")]
    todo = [p for p in paths if not (out / p.name).exists()]

    config = load_agent_config()
    # 开启思考时单次调用可能超过默认的 60 秒
    config = config.model_copy(update={"model": config.model.model_copy(update={"request_timeout_seconds": 300})})
    try:
        llm = create_llm_provider(dataclasses.replace(load_settings(), llm_model=JUDGE_MODEL), config)
    except LLMError as e:
        sys.exit(f"评审模型无法启动：{e}")

    spent = sum(json.loads(p.read_text(encoding="utf-8"))["cost_cny"] for p in out.glob("*.json"))
    lock, reserved = threading.Lock(), 0.0

    def run(path: Path) -> str:
        nonlocal spent, reserved
        with lock:
            if spent + reserved + WORST_CALL_CNY > args.max_cost:
                return f"{path.stem}: 跳过（已花费 ¥{spent:.2f}，接近上限 ¥{args.max_cost}）"
            reserved += WORST_CALL_CNY
        cost = 0.0
        try:
            checked, cost = check_one(path, llm, config)
        except (LLMError, ValueError) as e:
            return f"{path.stem}: 失败（{e}），重新运行会重试"
        finally:
            with lock:
                reserved -= WORST_CALL_CNY
                spent += cost
        (out / path.name).write_text(json.dumps({"claims": checked, "cost_cny": round(cost, 4), "judge": JUDGE_MODEL},
                                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        s = summarize_support([checked])
        return f"{path.stem}: {s['supported']}/{s['claims']} 条有来源支持 ¥{cost:.3f}，累计 ¥{spent:.2f}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for future in as_completed([pool.submit(run, p) for p in todo]):
            print(future.result(), flush=True)

    done = [p for p in paths if (out / p.name).exists()]
    checked = [json.loads((out / p.name).read_text(encoding="utf-8"))["claims"] for p in done]
    summary = {**summarize_support(checked), "runs_checked": len(done), "runs_total": len(paths),
               "judge": JUDGE_MODEL, "cost_cny": round(spent, 4)}
    (args.run_dir / "source_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n来源支持率 {summary['support_rate']}（{summary['supported']}/{summary['claims']}），"
          f"检查了 {len(done)}/{len(paths)} 次运行，合计 ¥{spent:.2f}")
    if len(done) < len(paths):
        sys.exit("还有运行没检查完，重新运行会继续")


if __name__ == "__main__":
    main()
