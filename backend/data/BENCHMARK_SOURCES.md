# 市场价基准表 V0：数据来源与设计原则

| 项目 | 内容 |
|---|---|
| 数据文件 | `backend/data/benchmarks_v0.json`（由 `scripts/build_benchmarks.py` 生成） |
| 相关配置 | `backend/config/pricing.yaml`（量级、得分档位、修正系数） |
| 数据日期 | 2026-10-25 |
| 币种 / 单位 | USD / 单条交付内容 |
| 主来源 | Influencer Marketing Hub（IMH） |

> 这份表是**种子数据**，用于给报价一个参考区间，不是权威市场价。M2 起由 Agent 搜索同类博主的真实报价做时效补充（见 PRD 5.7 节）。

---

## 1. 设计原则

### 原则 1：绝不编数据

每个数字都必须能追溯到公开来源，或者由公开来源按明确、可复现的方法推导出来（推导代码见 `scripts/build_benchmarks.py`）。查不到的不猜：价格类数据标"低置信度"，领域系数按 1.0 处理并标"低置信度"。

### 原则 2：领域系数取下限，宁可保守

**规则：**
- 只采用明确写了"相对于生活方式类（= 1.0）"的来源；
- 两个来源一致时，取**重叠区间的下限**；
- 只有一个来源时，取**这个来源区间的下限**；
- 查不到时取 **1.0**。

**理由：**
1. **避免重复计算**。基础价格来自 IMH 的全市场区间，本身已经包含了一部分高价领域的报价。如果在这个基础上再乘满系数，高价领域的溢价就被算了两次，基准会被系统性抬高。
2. **两种错误的代价不一样**。基准偏高时，产品会告诉博主"你被压价了"，博主据此还价，可能把一个本来合理的合作谈崩，或者让博主对品牌产生不必要的不信任。基准偏低时，最坏情况是少提醒一次还价，而且 M2 的 Agent 会搜索同类真实报价来纠正偏差。第一种错误的代价更高，也更难被用户察觉，所以宁可偏低。
3. **来源本身是区间**。来源给的是"2–3 倍"这类区间，取中点或上限都意味着我们比来源更确定，但我们没有这样的依据。

### 原则 3：区间转分位数是建模假设

来源公布的是"典型区间"，不是分位数。V0 统一按下面的方式换算：

- **P25** = 区间下限；
- **P75** = 区间上限；
- **P50** = 两者的几何平均 √(P25 × P75)。

用几何平均是因为价格区间跨度很大（例如 $500–5,000），普通中点会明显偏向高价一端。

### 原则 4：置信度如实标注，并随结果输出

每格价格和每个领域系数都标注置信度（中 / 低）。报价评估结果里，只要用到的任何一项是低置信度，整体就标为低置信度，同时输出具体原因（`confidence_notes`）和一句给用户的提示（`confidence_message`）。

V0 没有"高"置信度：IMH 没有公开数据收集方法，所以直接引用 IMH 的数据最高只算"中"。

---

## 2. 基础价格表（USD / 单条，P25 / P50 / P75）

| 平台 · 形式 | Nano 1K–10K | Micro 10K–50K | Mid 50K–100K | Mid+ 100K–250K | Upper 250K–500K |
|---|---|---|---|---|---|
| IG 图文帖 | 10 / 20 / 50 ⓜ | 150 / 230 / 350 | 350 / 415 / 500 | 500 / 965 / 1,855 | 1,855 / 3,045 / 5,000 |
| IG Reel | 20 / 45 / 100 ⓜ | 300 / 455 / 695 | 695 / 835 / 1,000 | 1,000 / 1,925 / 3,710 | 3,710 / 6,090 / 10,000 |
| IG Story | 5 / 11 / 25 ⓜ | 75 / 115 / 175 | 175 / 210 / 250 | 250 / 480 / 925 | 925 / 1,525 / 2,500 |
| TikTok 视频 | 50 / 100 / 200 ⓜ | 200 / 325 / 525 | 525 / 650 / 800 | 800 / 1,150 / 1,660 | 1,660 / 2,185 / 2,880 |
| YouTube 植入 | 20 / 65 / 200 | 200 / 350 / 615 | 615 / 785 / 1,000 | 1,000 / 1,925 / 3,710 | 3,710 / 6,090 / 10,000 |
| YouTube 专属视频 | 40 / 125 / 400 | 400 / 700 / 1,230 | 1,230 / 1,570 / 2,000 | 2,000 / 3,850 / 7,420 | 7,420 / 12,180 / 20,000 |

ⓜ = 中置信度；其余均为低置信度。

### 推导方法

| 数据 | 方法 | 置信度 |
|---|---|---|
| IG 图文帖、TikTok 视频的 Nano 列 | IMH 原始区间直接使用 | 中 |
| 其余量级 | IMH 的量级比我们粗（10K–100K、100K–500K，TikTok 是 100K–1M）。按粉丝数在 IMH 区间内做**对数插值**：价格 = 下限 × (上限 / 下限)^t，t = log(粉丝数 / 区间粉丝下限) / log(区间粉丝上限 / 区间粉丝下限) | 低 |
| IG Reel | 图文帖 × 2.0。IMH Nano 级 Reel 和图文帖的比值正好是 2.0；Influee 写 2–3 倍，取下限 | 中（Nano）/ 低（其余，因为基础价是插值得到的） |
| IG Story | 图文帖 × 0.5。IMH Nano 级比值和 Influee 都是 0.5 | 同上 |
| YouTube 植入 | IMH 只写"每条赞助视频"，没说明形式，按植入处理 | 低（形式是假设） |
| YouTube 专属视频 | 植入 × 2.0。CreatiCalc 写 2 倍；1of10 写 2–4 倍，2 倍为底线 | 低 |

取整规则：≥ $20 的价格四舍五入到 $5，< $20 的取整到 $1。

---

## 3. 领域系数

| 领域 | 系数 | 置信度 | 依据 |
|---|---|---|---|
| Personal Finance | 2.0 | 中 | Influee 2–3 倍；Bizkol 2.0–3.0 倍；共同下限 |
| Tech | 1.5 | 低 | 只有 Bizkol 给了 1.5–2.5 倍，取下限。Hubfluence 的 TikTok 千次曝光价格数据把科技类排在美妆之后，两个来源有矛盾 |
| Beauty | 1.2 | 中 | Influee 1–1.5 倍；Bizkol 1.2–1.6 倍；重叠区间下限 |
| Fitness & Wellness | 1.1 | 中 | Influee 1–1.5 倍；Bizkol 1.1–1.4 倍；重叠区间下限 |
| Fashion | 1.0 | 低 | 只有 Influee 写了"和生活方式持平" |
| Food | 1.0 | 低 | 只有 Influee 写了"和生活方式持平" |
| Travel & Lifestyle | 1.0 | 低 | 生活方式是两个来源共同的 1.0 基准；没找到旅行类数据 |
| Gaming | 1.0 | 低 | 没找到游戏类的直接数据，按原则 1 取 1.0。间接证据（Hubfluence 的游戏类千次曝光价格最低；Bizkol 把"娱乐类"定为 0.8–1.0 倍）显示游戏类可能偏低，但不是直接数据，没有采用 |

---

## 4. 修正系数（`pricing.yaml`）

| 项目 | 系数 | 置信度 | 依据 |
|---|---|---|---|
| 付费投放 / 白名单，没有单独计费，≤ 180 天 | +20% | 低 | Influee：使用权加价 20–50%，取下限 |
| 永久使用权、未写明期限或 > 180 天，没有单独计费 | +50% | 低 | 同一来源，取上限 |
| 付费使用单独计费 | 不加价 | — | 使用费本来就不在现金报价里 |
| 独家期 1–30 天 | +25% | 低 | Influee：30 天独家加价 25–50%，取下限 |
| 独家期 31–89 天 | +50% | 低 | 同一来源，取上限 |
| 独家期 ≥ 90 天 | +100% | 低 | Influee："90 天以上可能翻倍" |
| 全行业独家 | 同上 | 低 | 没有单独数据，沿用同一档位 |
| 互动率修正 | 不启用（1.0） | — | 没找到可靠的分平台、分量级平均互动率数据；配置里保留 `enabled` 开关，但打开会直接报错，直到实现为止 |

多个系数同时存在时相乘：调整后基准 = 基准 × (1 + 使用权加价) × (1 + 独家期加价) × 互动率系数。

---

## 5. 交叉对照：各来源差异

| 来源 | IG Micro 一条帖子 | 说明 |
|---|---|---|
| Collabstr 2026 | 平均成交价约 $193（所有量级混合） | 平台真实成交数据，以小博主为主 |
| DesignRush（2025-12） | $35–450（10K–50K） | 数据转引自 Shopify 的旧版文章 |
| **IMH 2026（主来源）** | **$150–500（10K–100K）** | |
| Shopify（2026-06） | $250–5,000（10K–100K） | 汇总多家机构的数据，数字偏高 |
| Influee（2026-03） | $250–5,000（10K–100K） | 与 Shopify 相同 |

选 IMH 作为主来源的理由：最新（2026 年 8–10 月更新）；同一家机构覆盖三个平台、量级划分一致；数字处在各来源的中间位置。如果换成 Shopify，基准价会高 2–5 倍。

---

## 6. 已知问题

1. **Nano 到 Micro 有跳档**：IG 图文帖 Nano 最高 $50，Micro 最低 $150。这是 IMH 原表的分档方式，没有人为平滑。
2. **Tech 系数的来源有矛盾**（见第 3 节）。
3. **YouTube 植入和 IG Reel 在 Mid+ / Upper 的数字相同**：两者用的 IMH 区间恰好一样，是巧合，不是错误。
4. **IMH 没有公开数据方法**，这也是 V0 最高只标"中"置信度的原因。

## 7. 更新机制

- 每季度复核一次来源，更新 `scripts/build_benchmarks.py` 里的原始区间后重新生成 JSON；
- 改了数字要同步更新 `backend/tests/test_benchmark_data.py` 里的抽查值和本文档；
- M4 评测后，根据评测集结果校准。

---

## 8. 来源列表

| ID | 来源 | 更新日期 |
|---|---|---|
| `imh_instagram` | [Influencer Marketing Hub – Instagram Influencer Rates 2026](https://influencermarketinghub.com/influencer-rates/instagram-influencer-rates/) | 2026-10-07 |
| `imh_tiktok` | [Influencer Marketing Hub – TikTok Influencer Rates](https://influencermarketinghub.com/influencer-rates/tiktok-influencer-rates/) | 2026-08-31 |
| `imh_youtube` | [Influencer Marketing Hub – YouTube Influencer Rates](https://influencermarketinghub.com/influencer-rates/youtube-influencer-rates/) | 2026-08-31 |
| `influee_2026` | [Influee – Instagram Influencer Pricing in 2026](https://influee.co/blog/instagram-influencer-pricing) | 2026-03-26 |
| `bizkol_cpm_2026` | [Bizkol – Influencer CPM Benchmarks 2026](https://www.bizkol.ai/blog/influencer-cpm-benchmarks) | 2026-07-03 |
| `hubfluence_tiktok_cpm` | [Hubfluence – TikTok CPM Rates 2026](https://www.hubfluence.io/resources/tiktok-cpm-rates) | 2026（未标具体日期） |
| `creaticalc_youtube` | [CreatiCalc – YouTube Sponsorship Rate Calculator](https://creaticalc.com/youtube-sponsorship-rate-calculator) | 2026-09 |
| `oneof10_youtube` | [1of10 – YouTube Sponsorship Rates 2026](https://1of10.com/blog/youtube-sponsorship-rates/) | 2026-08-24 |
| 交叉对照 | [Shopify – Influencer Pricing 2026](https://www.shopify.com/blog/influencer-pricing) | 2026-06-22 |
| 交叉对照 | [DesignRush – Influencer Rates](https://www.designrush.com/agency/social-media-marketing/instagram/trends/influencer-rates) | 2025-12-11 |
| 交叉对照 | [Collabstr – Instagram influencer marketing cost 2026](https://collabstr.com/blog/instagram-influencer-marketing) | 2026 |
