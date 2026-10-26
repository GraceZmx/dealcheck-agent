"""模型盲标：让另一个模型按标注准则独立判断每个案例，作为标准答案的参考（PRD 14.1）。

模型看到的内容和人工标注时一样：答题卡每题的"创作者 / 邀请 / 市场价参考 / 品牌调查要点"，加标注准则的判断方法。
不给模型看：人工答案、案例 JSON（里面有设计意图、类别、组别）、案例编号（换成新的随机编号）。不联网搜索。

用法（在项目根目录，先激活虚拟环境）：
    python scripts/blind_label.py --show 1        # 免费：打印答题卡第 1 题实际要发给模型的完整内容
    python scripts/blind_label.py --yes           # 正式运行（会产生费用；中断后重新运行会跳过已完成的题）

数据和结果都在 local/eval/（不进 GitHub）。
"""

import argparse
import json
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_agent_config  # noqa: E402
from app.llm.base import ChatOptions, LLMError, Message  # noqa: E402
from app.llm.factory import create_llm_provider  # noqa: E402
from app.settings import load_settings  # noqa: E402
import dataclasses  # noqa: E402

EVAL = ROOT / "local" / "eval"
GUIDE = ROOT / "eval" / "LABELING_GUIDE.md"
MODEL = "deepseek-v4-pro"
COST_CAP_CNY = 6.0
WORKERS = 5  # 开启思考时单题要几分钟，并行运行
WORST_CALL_CNY = 0.25  # 单题最坏情况的保守估计，剩余预算不够一题时停止
OPTIONS = ChatOptions(max_tokens=8000, thinking=True, reasoning_effort="high", json_mode=True)

# 标注准则第三节只取和输出字段有关的几行；其余行讲的是 must_hit、key_risks 等人工字段，模型用不到
FIELD_ROWS = ("| `decision`", "| `boundary`", "| `confidence`", "| `rationale`")

OUTPUT_SPEC = """## 输出格式

只输出一个 JSON 对象，不要输出其他文字：

{
  "decision": "accept" | "negotiate" | "decline",
  "confidence": "high" | "medium" | "low",
  "boundary": true | false,
  "boundary_note": "两个决策都说得通时，写另一个为什么也说得通；否则为空字符串",
  "rationale": "2–4 句话，写成你会对朋友说的话（中文）",
  "risks": ["必须提醒朋友的风险，2–4 条（中文）"]
}"""


def guide_text() -> str:
    """标注准则第一、二节全文，加第三节里和输出字段有关的几行。"""
    text = GUIDE.read_text(encoding="utf-8")
    one_two = text[text.index("## 一、"):text.index("## 三、")].strip().rstrip("-").strip()
    three = text[text.index("## 三、"):text.index("## 四、")]
    rows = [line for line in three.splitlines() if line.startswith(FIELD_ROWS)]
    return one_two + "\n\n## 三、每个字段怎么填\n\n| 字段 | 怎么填 |\n|---|---|\n" + "\n".join(rows)


def system_prompt() -> str:
    return (
        "你是一位帮创作者朋友看品牌合作邀请的顾问。下面是判断方法，请严格按它来判断。\n"
        "市场价参考已经算好写在题目里；品牌调查要点是别人整理好的事实和来源，你不能联网，只根据题目里的信息判断。\n\n"
        f"{guide_text()}\n\n{OUTPUT_SPEC}"
    )


def sheet_questions() -> dict[int, str]:
    """答题卡每题的题面（去掉"你的判断"及之后的部分，也就去掉了人工答案）。"""
    text = (EVAL / "answer_sheet.md").read_text(encoding="utf-8")
    questions = {}
    for m in re.finditer(r"## 第 (\d+) 题\n(.*?)(?=\n## 第 |\Z)", text, re.S):
        questions[int(m.group(1))] = m.group(2).split("### 你的判断", 1)[0].strip()
    return questions


def blind_key(out: Path, questions: dict[int, str]) -> dict[str, int]:
    """盲标编号 B01–B25 → 答题卡题号。用新的随机顺序，第一次运行时生成并保存，之后复用。"""
    path = out / "blind_key.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    order = list(questions)
    random.SystemRandom().shuffle(order)
    key = {f"B{i:02d}": q for i, q in enumerate(order, 1)}
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return key


def user_prompt(blind_id: str, body: str) -> str:
    return f"## 案例 {blind_id}\n\n{body}\n\n请按判断方法给出你的判断，只输出 JSON。"


def call_cost_cny(usage, config) -> float:
    price = config.pricing.models[MODEL]
    miss = usage.input_tokens - usage.cached_input_tokens
    usd = (usage.cached_input_tokens * price.input_cache_hit + miss * price.input_cache_miss
           + usage.output_tokens * price.output) / 1_000_000
    return usd * config.pricing.usd_to_cny


def parse_answer(content: str) -> dict:
    answer = json.loads(content)
    if answer.get("decision") not in ("accept", "negotiate", "decline"):
        raise ValueError(f"decision 不合法：{answer.get('decision')}")
    if answer.get("confidence") not in ("high", "medium", "low"):
        raise ValueError(f"confidence 不合法：{answer.get('confidence')}")
    return answer


def write_second_opinions(out: Path, key: dict[str, int]) -> None:
    """把盲标结果写进案例的 label.second_opinion。"""
    case_key = json.loads((EVAL / "answer_sheet_key.json").read_text(encoding="utf-8"))
    for blind_id, q in key.items():
        result = json.loads((out / f"{blind_id}.json").read_text(encoding="utf-8"))
        answer = result["answer"]
        note = answer["rationale"]
        if answer.get("boundary") and answer.get("boundary_note"):
            note += f"（另一个决策也说得通：{answer['boundary_note']}）"
        note += " 风险：" + "；".join(answer.get("risks", []))
        case_id = case_key[str(q)]
        path = next(p for p in (EVAL / "cases" / f"{case_id}.json", ROOT / "eval" / "cases" / f"{case_id}.json") if p.exists())
        case = json.loads(path.read_text(encoding="utf-8"))
        case["label"]["second_opinion"] = {
            "by": result["model"], "at": result["at"], "decision": answer["decision"],
            "confidence": answer["confidence"], "note": note,
        }
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(out: Path) -> None:
    questions = sheet_questions()
    key = blind_key(out, questions)
    config = load_agent_config()
    # 开启思考时单次调用可能超过默认的 60 秒
    config = config.model_copy(update={"model": config.model.model_copy(update={"request_timeout_seconds": 300})})
    settings = dataclasses.replace(load_settings(), llm_model=MODEL)
    try:
        llm = create_llm_provider(settings, config)
    except LLMError as e:
        sys.exit(f"模型无法启动：{e}")

    system = system_prompt()
    spent = sum(json.loads(p.read_text(encoding="utf-8"))["cost_cny"] for p in out.glob("B*.json"))
    todo = [(b, q) for b, q in key.items() if not (out / f"{b}.json").exists()]
    lock = threading.Lock()
    reserved = 0.0  # 正在进行的调用按最坏情况预留预算，保证并行时也不超上限

    def label_one(blind_id: str, q: int) -> str:
        nonlocal spent, reserved
        with lock:
            if spent + reserved + WORST_CALL_CNY > COST_CAP_CNY:
                return f"{blind_id}: 跳过（已花费 ¥{spent:.2f}，接近上限 ¥{COST_CAP_CNY}）"
            reserved += WORST_CALL_CNY
        prompt = user_prompt(blind_id, questions[q])
        messages = [Message(role="system", content=system), Message(role="user", content=prompt)]
        answer, cost, response = None, 0.0, None
        try:
            for attempt in range(2):  # 格式不对时重试一次
                response = llm.chat(messages, options=OPTIONS)
                cost += call_cost_cny(response.usage, config)
                try:
                    answer = parse_answer(response.message.content)
                    break
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"{blind_id} 第 {attempt + 1} 次输出格式不对：{e}", flush=True)
        except LLMError as e:
            return f"{blind_id}: 模型调用失败（{e}），重新运行会重试这一题"
        finally:
            with lock:
                reserved -= WORST_CALL_CNY
                spent += cost
        if answer is None:
            return f"{blind_id}: 两次输出格式都不对，重新运行会重试这一题"
        (out / f"{blind_id}.json").write_text(json.dumps({
            "blind_id": blind_id, "model": response.model, "at": date.today().isoformat(),
            "options": OPTIONS.model_dump(), "prompt": prompt, "answer": answer,
            "reasoning": response.message.reasoning_content, "usage": response.usage.model_dump(),
            "cost_cny": round(cost, 4),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return f"{blind_id}: {answer['decision']}（{answer['confidence']}）¥{cost:.3f}，累计 ¥{spent:.2f}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for future in as_completed([pool.submit(label_one, b, q) for b, q in todo]):
            print(future.result(), flush=True)

    missing = [b for b in key if not (out / f"{b}.json").exists()]
    if missing:
        sys.exit(f"还有 {len(missing)} 题没完成：{', '.join(missing)}。已花费 ¥{spent:.2f}")
    (out / "system_prompt.md").write_text(system, encoding="utf-8")
    write_second_opinions(out, key)
    print(f"\n完成 {len(key)} 题，合计 ¥{spent:.2f}。结果已写进各案例的 label.second_opinion")


def show(question: int, out: Path) -> None:
    questions = sheet_questions()
    if question not in questions:
        sys.exit(f"答题卡没有第 {question} 题")
    key = blind_key(out, questions)
    blind_id = next(b for b, q in key.items() if q == question)
    system, prompt = system_prompt(), user_prompt(blind_id, questions[question])
    print("=" * 30 + " 系统提示（每题相同） " + "=" * 30 + "\n")
    print(system)
    print("\n" + "=" * 30 + f" 用户消息（答题卡第 {question} 题，盲标编号 {blind_id}） " + "=" * 30 + "\n")
    print(prompt)
    print("\n" + "=" * 80)
    print(f"模型 {MODEL}，开启思考（high），不联网。字数：系统提示 {len(system)}，用户消息 {len(prompt)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blind second-opinion labeling with another model.")
    parser.add_argument("--show", type=int, metavar="题号", help="只打印这一题要发给模型的内容，不调用模型")
    parser.add_argument("--yes", action="store_true", help="确认会产生费用，正式运行")
    parser.add_argument("--out", type=Path, default=EVAL / "second_opinion")
    args = parser.parse_args()
    if args.show is not None:
        show(args.show, args.out)
    elif args.yes:
        run(args.out)
    else:
        parser.error("用 --show <题号> 预览，或加 --yes 正式运行（会产生费用）")


if __name__ == "__main__":
    main()
