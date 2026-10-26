"""提交前检查：拦住真实品牌名、.env / .venv，以及 local/ 和 traces/ 下的文件。

由 .githooks/ 里的 git 钩子自动调用（启用：git config core.hooksPath .githooks）。也可以手动运行：
    python scripts/check_brands.py --all          # 全仓库扫描：往名单里加了新品牌后跑一次

真实品牌名单只在本机，永远不提交（local/ 已被 git 忽略），由两部分合成：
- 自动收集：local/eval/cases/ 和 local/real_deals/ 里每个案例的 brand_name 和官网域名主体
- 手动补充：local/brand_blocklist.txt，一行一个（别名、母公司、当地合作方等）；
  # 开头是注释；写成 case:Name 表示区分大小写，用于本身就是普通英文单词的品牌名

只用 Python 标准库。
"""

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

CASE_DIRS = ("eval/cases", "real_deals")  # 相对 local/
BLOCKLIST = "brand_blocklist.txt"
MIN_TOKEN_CHARS = 3  # 单个词少于 3 个字符的名字太容易误拦，忽略并提示
SEPARATORS = r"[\s\-_.'’]*"  # Brand Name / brand-name / brand_name / brandname / Brand's Name 都算命中
FORBIDDEN_DIRS = ("local/", "traces/", ".venv/")


@dataclass(frozen=True)
class Brand:
    label: str
    pattern: re.Pattern


@dataclass(frozen=True)
class Hit:
    where: str
    line: int | None
    label: str
    text: str


def make_brand(name: str, case_sensitive: bool = False) -> Brand | None:
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not words or (len(words) == 1 and len(words[0]) < MIN_TOKEN_CHARS):
        return None
    body = SEPARATORS.join(re.escape(w) for w in words)
    flags = 0 if case_sensitive else re.IGNORECASE
    # 按整个单词匹配：前后不能紧挨着字母或数字（下划线、横线、点都算分隔）
    return Brand(label=name, pattern=re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", flags))


def domain_stem(url: str) -> str | None:
    host = urlparse(url if "//" in url else f"//{url}").hostname or ""
    parts = host.removeprefix("www.").split(".")
    return parts[-2] if len(parts) >= 2 else None


def load_brands(local_dir: Path) -> tuple[list[Brand], list[str]]:
    """返回 (品牌列表, 提示信息)。"""
    names: dict[tuple[str, bool], None] = {}
    notes = []
    for sub in CASE_DIRS:
        for path in sorted((local_dir / sub).glob("*.json")):
            try:
                deal = json.loads(path.read_text(encoding="utf-8")).get("deal") or {}
            except (ValueError, OSError):
                notes.append(f"读不了 {path.relative_to(local_dir.parent)}，已跳过")
                continue
            if deal.get("brand_name"):
                names[(deal["brand_name"].strip(), False)] = None
            if deal.get("brand_website") and (stem := domain_stem(deal["brand_website"])):
                names[(stem, False)] = None
    blocklist = local_dir / BLOCKLIST
    if blocklist.exists():
        for raw in blocklist.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("case:"):
                names[(line[5:].strip(), True)] = None
            else:
                names[(line, False)] = None
    brands = []
    for name, case_sensitive in names:
        brand = make_brand(name, case_sensitive)
        if brand is None:
            notes.append(f"名字太短，已忽略：{name}")
        else:
            brands.append(brand)
    return brands, notes


def scan_text(text: str, brands: list[Brand], where: str, line: int | None = None) -> list[Hit]:
    return [Hit(where, line, b.label, text.strip()[:120]) for b in brands if b.pattern.search(text)]


def forbidden_path(path: str) -> str | None:
    """返回拦截原因；可以提交时返回 None。"""
    p = PurePosixPath(path)
    if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
        return "密钥文件 .env 不能提交"
    if any(path.startswith(d) or f"/{d}" in path for d in FORBIDDEN_DIRS):
        return "local/、traces/、.venv/ 下的文件不能提交"
    return None


def parse_added_lines(diff: str) -> list[tuple[str, int, str]]:
    """从 git diff -U0 的输出里取出新增的行：(文件, 行号, 内容)。"""
    added, path, lineno = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and path is not None:
            added.append((path, lineno, line[1:]))
            lineno += 1
    return added


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def check_staged(brands: list[Brand]) -> tuple[list[str], list[Hit]]:
    paths = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if p]
    blocked = [f"{p}：{reason}" for p in paths if (reason := forbidden_path(p))]
    hits = [h for p in paths for h in scan_text(p, brands, f"文件名 {p}")]
    diff = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR")
    for path, lineno, text in parse_added_lines(diff):
        hits += scan_text(text, brands, path, lineno)
    return blocked, hits


def check_all(root: Path, brands: list[Brand]) -> list[Hit]:
    hits = []
    for path in (p for p in git("ls-files", "-z").split("\0") if p):
        hits += scan_text(path, brands, f"文件名 {path}")
        try:
            data = (root / path).read_bytes()
        except OSError:
            continue
        if b"\0" in data:  # 二进制文件
            continue
        for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            hits += scan_text(line, brands, path, i)
    return hits


def check_message(message_file: Path, brands: list[Brand]) -> list[Hit]:
    hits = []
    for i, line in enumerate(message_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("#"):  # git 自动加的注释行不算
            hits += scan_text(line, brands, "提交信息", i)
    return hits


def report(blocked: list[str], hits: list[Hit], fix_hint: bool = True) -> None:
    if blocked:
        print("\n✋ 提交被拦住：有不能提交的文件\n", file=sys.stderr)
        for b in blocked:
            print(f"  {b}", file=sys.stderr)
        print("\n怎么处理：git restore --staged <文件> 把它移出暂存区，再重新提交", file=sys.stderr)
    if hits:
        print("\n✋ 提交被拦住：发现真实品牌名（名单来自 local/，不会上传）\n", file=sys.stderr)
        for h in hits:
            where = f"{h.where} 第 {h.line} 行" if h.line else h.where
            print(f"  {where}：命中 \"{h.label}\"\n      {h.text}", file=sys.stderr)
        if fix_hint:
            print("\n怎么处理：\n"
                  "  1. 把品牌名改成匿名描述（例如\"一个酒精饮料品牌\"）或案例编号（例如 R07），重新 git add 后再提交\n"
                  "  2. 如果是误拦（品牌名正好是普通单词），在 local/brand_blocklist.txt 里写成 case:写法", file=sys.stderr)
    print(file=sys.stderr)


def main(argv: list[str]) -> int:
    root = Path(git("rev-parse", "--show-toplevel").strip())
    brands, notes = load_brands(root / "local")
    for note in notes:
        print(f"⚠️  {note}", file=sys.stderr)
    if not brands:
        print("⚠️  品牌名单为空（local/ 里没有案例或名单），本次没有做品牌检查", file=sys.stderr)

    mode = argv[0] if argv else "pre-commit"
    if mode == "pre-commit":
        blocked, hits = check_staged(brands)
    elif mode == "commit-msg":
        blocked, hits = [], check_message(Path(argv[1]), brands)
    elif mode == "--all":
        blocked, hits = [], check_all(root, brands)
        if not hits:
            print(f"✅ 全仓库扫描完成：{len(brands)} 个品牌名，没有发现命中")
    else:
        print(f"未知参数：{mode}（可用：pre-commit / commit-msg <文件> / --all）", file=sys.stderr)
        return 2

    if blocked or hits:
        report(blocked, hits)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
