"""生成市场价基准表 V0：backend/data/benchmarks_v0.json。

推导方法见 backend/data/BENCHMARK_SOURCES.md。运行方式（在项目根目录）：
    python scripts/build_benchmarks.py
"""

import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "backend" / "data" / "benchmarks_v0.json"

SOURCES = {
    "imh_instagram": {
        "title": "Influencer Marketing Hub – Instagram Influencer Rates 2026",
        "url": "https://influencermarketinghub.com/influencer-rates/instagram-influencer-rates/",
        "updated": "2026-10-07",
    },
    "imh_tiktok": {
        "title": "Influencer Marketing Hub – TikTok Influencer Rates",
        "url": "https://influencermarketinghub.com/influencer-rates/tiktok-influencer-rates/",
        "updated": "2026-08-31",
    },
    "imh_youtube": {
        "title": "Influencer Marketing Hub – YouTube Influencer Rates",
        "url": "https://influencermarketinghub.com/influencer-rates/youtube-influencer-rates/",
        "updated": "2026-08-31",
    },
    "influee_2026": {
        "title": "Influee – Instagram Influencer Pricing in 2026",
        "url": "https://influee.co/blog/instagram-influencer-pricing",
        "updated": "2026-03-26",
    },
    "bizkol_cpm_2026": {
        "title": "Bizkol – Influencer CPM Benchmarks 2026",
        "url": "https://www.bizkol.ai/blog/influencer-cpm-benchmarks",
        "updated": "2026-07-03",
    },
    "hubfluence_tiktok_cpm": {
        "title": "Hubfluence – TikTok CPM Rates 2026",
        "url": "https://www.hubfluence.io/resources/tiktok-cpm-rates",
        "updated": "2026",
    },
    "creaticalc_youtube": {
        "title": "CreatiCalc – YouTube Sponsorship Rate Calculator",
        "url": "https://creaticalc.com/youtube-sponsorship-rate-calculator",
        "updated": "2026-09",
    },
    "oneof10_youtube": {
        "title": "1of10 – YouTube Sponsorship Rates 2026",
        "url": "https://1of10.com/blog/youtube-sponsorship-rates/",
        "updated": "2026-08-24",
    },
}

# 粉丝量级（含下限、不含上限）
TIERS = [
    ("nano", 1_000, 10_000),
    ("micro", 10_000, 50_000),
    ("mid", 50_000, 100_000),
    ("mid_plus", 100_000, 250_000),
    ("upper", 250_000, 500_000),
]

# IMH 原始区间：(粉丝下限, 粉丝上限, 价格下限, 价格上限)
IMH_RANGES = {
    "instagram_feed_post": ("imh_instagram", [(1e3, 1e4, 10, 50), (1e4, 1e5, 150, 500), (1e5, 5e5, 500, 5000)]),
    "tiktok_video": ("imh_tiktok", [(1e3, 1e4, 50, 200), (1e4, 1e5, 200, 800), (1e5, 1e6, 800, 5000)]),
    "youtube_integration": ("imh_youtube", [(1e3, 1e4, 20, 200), (1e4, 1e5, 200, 1000), (1e5, 5e5, 1000, 10000)]),
}

# IMH 未说明 YouTube 价格对应哪种形式，按"植入"处理，因此一律低置信度
FORMAT_ASSUMED = {
    "youtube_integration": "IMH gives a price per sponsored video without naming the format; treated as an integration.",
}

# 由其他形式乘系数得到：(基础形式, 系数, 来源, 置信度, 说明)
FORMAT_MULTIPLIERS = {
    "instagram_reel": ("instagram_feed_post", 2.0, ["imh_instagram", "influee_2026"], "medium",
                       "Reel = 2.0x feed post (IMH nano Reel/post ratio is exactly 2.0; Influee says 2-3x, lower bound)."),
    "instagram_story": ("instagram_feed_post", 0.5, ["imh_instagram", "influee_2026"], "medium",
                        "Story = 0.5x feed post (IMH nano ratio and Influee both give 0.5x)."),
    "youtube_dedicated": ("youtube_integration", 2.0, ["creaticalc_youtube", "oneof10_youtube"], "low",
                          "Dedicated video = 2.0x integration (CreatiCalc 2x; 1of10 says 2-4x with 2x as the floor)."),
}

PLATFORM_OF = {
    "instagram_feed_post": "instagram",
    "instagram_reel": "instagram",
    "instagram_story": "instagram",
    "tiktok_video": "tiktok",
    "youtube_integration": "youtube",
    "youtube_dedicated": "youtube",
}

NICHES = {
    "personal_finance": (2.0, "medium", ["influee_2026", "bizkol_cpm_2026"],
                         "Influee 2-3x and Bizkol 2.0-3.0x vs lifestyle; shared lower bound."),
    "tech": (1.5, "low", ["bizkol_cpm_2026", "hubfluence_tiktok_cpm"],
             "Only Bizkol gives a multiplier (1.5-2.5x, lower bound used); Hubfluence TikTok CPM data ranks tech below beauty, so sources conflict."),
    "beauty": (1.2, "medium", ["influee_2026", "bizkol_cpm_2026"],
               "Influee 1-1.5x and Bizkol 1.2-1.6x; lower bound of the overlap."),
    "fitness_wellness": (1.1, "medium", ["influee_2026", "bizkol_cpm_2026"],
                         "Influee 1-1.5x and Bizkol 1.1-1.4x; lower bound of the overlap."),
    "fashion": (1.0, "low", ["influee_2026"], "Single source (Influee) rates fashion at the lifestyle baseline."),
    "food": (1.0, "low", ["influee_2026"], "Single source (Influee) rates food at the lifestyle baseline."),
    "travel_lifestyle": (1.0, "low", ["influee_2026", "bizkol_cpm_2026"],
                         "Lifestyle is the 1.0 baseline in both sources; no travel-specific data found."),
    "gaming": (1.0, "low", [], "No gaming-specific multiplier found; defaults to 1.0."),
}


def _price_at(ranges, followers, upper=False):
    """在 IMH 区间内按粉丝数做对数插值。upper=True 时边界值归属较低的区间。"""
    for f_lo, f_hi, p_lo, p_hi in ranges:
        inside = (f_lo < followers <= f_hi) if upper else (f_lo <= followers < f_hi)
        if inside:
            t = math.log(followers / f_lo) / math.log(f_hi / f_lo)
            return p_lo * (p_hi / p_lo) ** t
    raise ValueError(followers)


def _round_price(x: float) -> int:
    return int(round(x / 5) * 5) if x >= 20 else int(round(x))


def _is_direct(ranges, lo, hi) -> bool:
    """该量级是否和 IMH 某个原始区间完全重合（无需插值）。"""
    return any(f_lo == lo and f_hi == hi for f_lo, f_hi, _, _ in ranges)


def build() -> dict:
    raw = {}  # (deliverable, tier) -> (p25, p50, p75) 未取整
    rows = []

    for deliverable, (source, ranges) in IMH_RANGES.items():
        for tier, lo, hi in TIERS:
            p25, p75 = _price_at(ranges, lo), _price_at(ranges, hi, upper=True)
            raw[(deliverable, tier)] = (p25, math.sqrt(p25 * p75), p75)
            direct = _is_direct(ranges, lo, hi)
            method = ("IMH range used directly." if direct
                      else "Interpolated (log scale by follower count) from a wider IMH range.")
            if deliverable in FORMAT_ASSUMED:
                method += " " + FORMAT_ASSUMED[deliverable]
            rows.append({
                "deliverable": deliverable,
                "tier": tier,
                "confidence": "medium" if direct and deliverable not in FORMAT_ASSUMED else "low",
                "sources": [source],
                "method": method,
            })

    for deliverable, (base, mult, sources, conf, note) in FORMAT_MULTIPLIERS.items():
        for tier, _, _ in TIERS:
            raw[(deliverable, tier)] = tuple(v * mult for v in raw[(base, tier)])
            base_row = next(r for r in rows if r["deliverable"] == base and r["tier"] == tier)
            confidence = "low" if "low" in (conf, base_row["confidence"]) else "medium"
            rows.append({
                "deliverable": deliverable,
                "tier": tier,
                "confidence": confidence,
                "sources": base_row["sources"] + sources,
                "method": f"{base_row['method']} {note}",
            })

    for row in rows:
        p25, p50, p75 = raw[(row["deliverable"], row["tier"])]
        row["platform"] = PLATFORM_OF[row["deliverable"]]
        row.update(p25=_round_price(p25), p50=_round_price(p50), p75=_round_price(p75))

    order = list(PLATFORM_OF)
    tier_order = [t[0] for t in TIERS]
    rows.sort(key=lambda r: (order.index(r["deliverable"]), tier_order.index(r["tier"])))
    keys = ["platform", "deliverable", "tier", "p25", "p50", "p75", "confidence", "sources", "method"]

    return {
        "version": "v0",
        "currency": "USD",
        "unit": "per deliverable",
        "data_date": "2026-10-25",
        "primary_source": "Influencer Marketing Hub (IMH)",
        "percentile_note": "Sources publish typical ranges, not percentiles. P25 = range low, P75 = range high, P50 = geometric mean.",
        "sources": SOURCES,
        "base_prices": [{k: r[k] for k in keys} for r in rows],
        "niche_coefficients": {
            niche: {"coefficient": c, "confidence": conf, "sources": src, "note": note}
            for niche, (c, conf, src, note) in NICHES.items()
        },
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")
