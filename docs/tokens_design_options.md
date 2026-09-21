# Antigravity CLI (`agym`) — Token Usage Redesign Plan & Options

## 1. Executive Summary & Problem Analysis

The `agym tokens` command provides visibility into token consumption across profiles. However, its current layout suffers from several design and usability issues:

* **Fragmented 3-part layout**: Running `agym tokens --breakdown` prints:
  1. A rectangular summary card (8 lines)
  2. A volume comparison list with horizontal bars (8 lines)
  3. A separate 7-column breakdown table (10 lines)
  * Total: **26+ lines for just 6 accounts**, resulting in disjointed scrolling where you have to look back and forth between the volume bars and the breakdown table.
* **Low-resolution volume bars**: Bars use coarse 1-character full/shade blocks (`████░░░░`), losing precision.
* **Lack of multi-account scalability**: For 15–50+ accounts, the volume list and breakdown table vertically explode to 60–150+ lines.
* **Disconnected metrics**: Volume proportion, composition mix (Input vs Output vs Thinking vs Cache), and cache hit efficiency are separated rather than synthesized into an intuitive, modern dashboard.

### Redesign Objectives
1. **Unified Telemetry**: Merge volume proportion, token mix, and cache efficiency into clean, cohesive components.
2. **High-Resolution Sub-Block Graphs**: Use 1/8th fractional blocks (` ▏▎▍▌▋▊▉█`) for smooth horizontal meters.
3. **Multi-Account Scalability (10 to 50+ Accounts)**: Enable users managing dozens of accounts to see their fleet at a single glance.
4. **Modern Aesthetics**: Sleek rounded borders (`╭─╮╰─╯`), color-coded categories (Input Blue, Output Emerald, Thinking Purple, Cache Amber), and smart tiering.
5. **Zero External Dependencies**: 100% Python standard library.

---

## 2. The 3 Design Options for `agym tokens`

```
                     ┌───────────────────────────────────────────────────┐
                     │            CHOOSE YOUR TOKENS EXPERIENCE          │
                     └───────────────────────────────────────────────────┘
                                               │
             ┌─────────────────────────────────┼─────────────────────────────────┐
             ▼                                 ▼                                 ▼
       [ Option 1 ]                      [ Option 2 ]                      [ Option 3 ]
  Modern Unified Table              Multi-Column Cards & Matrix       Executive Dashboard
   Single-row per account            Card Grid & Volume Heatmap        btop / htop style
   Volume + Mix + Hit % inline       Scales to 50–100+ accounts        Macro gauges & cost insights
```

---

### Option 1: Modern Unified Telemetry Table (Single-Row Volume + Stacked Mix)

#### Concept
Eliminates the disjointed 3-part layout by merging volume comparison, composition breakdown, and cache hit efficiency into **one unified, compact table**. Every profile occupies **exactly 1 row**.

#### Visual Mockup (Simulated Terminal Output)
```text
╭─ Fleet Token Telemetry (6 Accounts · 550.4M Total) ─────────────────────────────────────────╮
│ Total Tokens: 550.4M   ·   Input: 71.2M (12.9%)   ·   Out: 1.8M (0.3%)   ·   Think: 2.0M    │
│ Cache Savings: 475.3M (86.4% of total volume)     ·   Fleet Cache Hit Rate: 87.0% ⚡        │
│ Top Driver: csgotiago (141.2M · 25.7% of fleet)   ·   Status: Cached (3m ago)               │
╰─────────────────────────────────────────────────────────────────────────────────────────────╯

╭───────────┬─────────┬────────┬──────────────────────┬────────────────────────┬────────┬─────╮
│ Profile   │ Total   │ Share  │ Relative Volume      │ Token Composition Mix  │ Cache  │ Hit%│
├───────────┼─────────┼────────┼──────────────────────┼────────────────────────┼────────┼─────┤
│ csgotiago │  141.2M │  25.7% │ [████████████████████] │ [■■ In  ■ Out  ■■■■■ C]│ 124.3M │88.5%│
│ tmg       │  135.4M │  24.6% │ [███████████████████▏] │ [■■ In  ■ Out  ■■■■■ C]│ 119.6M │88.8%│
│ tfg       │   95.3M │  17.3% │ [█████████████▌░░░░░░] │ [■■■ In ■ Out  ■■■■ C] │  79.5M │84.2%│
│ ttb       │   94.9M │  17.2% │ [█████████████▍░░░░░░] │ [■■■ In ■ Out  ■■■■ C] │  78.9M │83.9%│
│ def       │   62.0M │  11.3% │ [████████▋░░░░░░░░░░░] │ [■■ In  ■ Out  ■■■■■ C]│  54.7M │88.8%│
│ tigas666  │   21.6M │   3.9% │ [███░░░░░░░░░░░░░░░░░] │ [■■ In  ■ Out  ■■■■■ C]│  18.3M │85.5%│
├───────────┼─────────┼────────┼──────────────────────┼────────────────────────┼────────┼─────┤
│ Fleet Sum │  550.4M │ 100.0% │ [████████████████████] │ 12.9% In · 0.3% Out · 86.4% Cached │87.0%│
╰───────────┴─────────┴────────┴──────────────────────┴────────────────────────┴────────┴─────╯
Legend: ■ Input (Blue)  ■ Output (Green)  ■ Thinking (Purple)  ■ Cache Read (Amber)
```

#### Key Highlights
* **Maximum Information Density**: Shows total volume, fleet share %, relative volume bar, token composition stacked mix, and cache hit % in a single row.
* **1 Line per Account**: 20 accounts take only 20 rows (+ summary header). Fits completely within a standard terminal window without scrolling.
* **Fractional Precision**: High-resolution sub-block meters (`█`, `▌`, `▎`, `▏`) ensure subtle volume differences (e.g. 95.3M vs 94.9M) are visually distinct.
* **Cohesive Narrative**: Combines macro fleet statistics and micro account telemetry seamlessly.

---

### Option 2: Multi-Column Fleet Token Cards & Heatmap Matrix (Scales to 50–100+ Accounts)

#### Concept
Tailored for users managing dozens to over a hundred accounts. Provides a **Multi-Column Card Dashboard** for standard fleet views, and an **Ultra-Dense Heatmap Matrix** (`--matrix`) that fits 50–100 accounts into a single screen.

#### Visual Mockup: Multi-Column Card Grid (`agym tokens --grid`)
```text
┌── Fleet Token Overview (6 Accounts · 550.4M Total) ────────────────────────────────────────┐
│ Distribution:  [>100M] ██████ (2 accs)   [50-100M] █████████ (3 accs)   [<50M] ███ (1 acc) │
│ Fleet Cache Hit Rate: 87.0% ⚡ (475.3M tokens saved)   ·   Top: csgotiago (141.2M · 25.7%) │
└────────────────────────────────────────────────────────────────────────────────────────────┘

┌─ [1] csgotiago ─ 141.2M (25.7%) ─┐ ┌─ [2] tmg ──────── 135.4M (24.6%) ─┐ ┌─ [3] tfg ───────── 95.3M (17.3%) ─┐
│ Volume: [████████████████████]100%│ │ Volume: [███████████████████▏] 96%│ │ Volume: [█████████████▌░░░░░░] 67%│
│ In: 16.1M · Out: 389k · Thk: 437k │ │ In: 15.1M · Out: 320k · Thk: 404k │ │ In: 14.9M · Out: 439k · Thk: 468k │
│ Cache: 124.3M  ·  Hit: 88.5% ⚡   │ │ Cache: 119.6M  ·  Hit: 88.8% ⚡   │ │ Cache: 79.5M   ·  Hit: 84.2% ⚡   │
│ Mix: [■■ In  ■ Out  ■■■■■■ Cache] │ │ Mix: [■■ In  ■ Out  ■■■■■■ Cache] │ │ Mix: [■■■ In  ■ Out  ■■■■■ Cache]│
└───────────────────────────────────┘ └───────────────────────────────────┘ └───────────────────────────────────┘
┌─ [4] ttb ──────── 94.9M (17.2%) ─┐ ┌─ [5] def ──────── 62.0M (11.3%) ─┐ ┌─ [6] tigas666 ──── 21.6M ( 3.9%) ─┐
│ Volume: [█████████████▍░░░░░░] 67%│ │ Volume: [████████▋░░░░░░░░░░░] 44%│ │ Volume: [███░░░░░░░░░░░░░░░░░] 15%│
│ In: 15.1M · Out: 466k · Thk: 377k │ │ In:  6.9M · Out: 168k · Thk: 231k │ │ In:  3.1M · Out:  62k · Thk:  74k │
│ Cache: 78.9M   ·  Hit: 83.9% ⚡   │ │ Cache: 54.7M   ·  Hit: 88.8% ⚡   │ │ Cache: 18.3M   ·  Hit: 85.5% ⚡   │
│ Mix: [■■■ In  ■ Out  ■■■■■ Cache]│ │ Mix: [■■ In  ■ Out  ■■■■■■ Cache] │ │ Mix: [■■ In  ■ Out  ■■■■■■ Cache] │
└───────────────────────────────────┘ └───────────────────────────────────┘ └───────────────────────────────────┘
```

#### Ultra-Dense Matrix Mode for 30–100+ Accounts (`agym tokens --matrix`)
```text
┌── Fleet Token Matrix (48 Accounts · 3.82B Total Tokens) ───────────────────────────────────┐
│ [01] csgotiago 141.2M ████████  [09] acc-09    88.4M █████░░░  [17] acc-17    42.1M ██▍░░░░░ │
│ [02] tmg       135.4M ███████▋  [10] acc-10    81.2M ████▌░░░  [18] acc-18    38.0M ██▏░░░░░ │
│ [03] tfg        95.3M █████▍░░  [11] acc-11    74.9M ████░░░░  [19] acc-19    29.5M █▋░░░░░░ │
│ [04] ttb        94.9M █████▎░░  [12] acc-12    68.0M ███▋░░░░  [20] acc-20    24.1M █▍░░░░░░ │
│ [05] def        62.0M ███▌░░░░  [13] acc-13    59.4M ███▎░░░░  [21] tigas666  21.6M █▏░░░░░░ │
│ Legend: ██ >100M (Heavy)  ██ 50-99M (Medium)  ██ 20-49M (Light)  █░ <20M (Minimal)          │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### Key Highlights
* **Horizontal Scalability**: 3-column card grid fits 24 accounts into 8 vertical rows.
* **Volume Heatmap Matrix**: Displays 60–100 accounts in 15–20 rows with mini volume sparkbars and tier colors.
* **Volume Distribution Histogram**: Gives immediate macro-level perspective on account volume distribution.

---

### Option 3: Executive Token Dashboard & Cache Efficiency Analyzer (`btop` / `htop` Style)

#### Concept
Focuses on **Token Economics, Driver Tiers, and Cache Optimization**. Organizes profiles into volume tiers (Heavy Drivers, Active Consumers, Light/Dormant) and provides actionable cache savings analytics.

#### Visual Mockup (Simulated Terminal Output)
```text
╔═══════════════════════════════════════════════════════════════════════════════════════════╗
║  AGYM TOKEN FLEET ANALYTICS                                       Fleet Cache: 87.0% ⚡   ║
║  Total Volume: 550.4M Tokens          Cache Savings: 475.3M (~87.0% bandwidth reduction)  ║
║  Composition:  [██ In 12.9% | ▍ Out 0.3% | ▍ Think 0.4% | ████████████████ Cache 86.4%]  ║
╚═══════════════════════════════════════════════════════════════════════════════════════════╝

🚀 HEAVY DRIVERS (>20% of fleet volume · 2 profiles)
   #1 csgotiago  141.2M ▇▇▇▇▇▇▇▇  In: 16.1M · Out: 389k · Cache: 124.3M (Hit: 88.5% ⚡) [25.7%]
   #2 tmg        135.4M ▇▇▇▇▇▇█   In: 15.1M · Out: 320k · Cache: 119.6M (Hit: 88.8% ⚡) [24.6%]

⚡ ACTIVE CONSUMERS (5% - 20% of fleet volume · 3 profiles)
   #3 tfg         95.3M ▇▇▇▇█▍    In: 14.9M · Out: 439k · Cache:  79.5M (Hit: 84.2% ⚡) [17.3%]
   #4 ttb         94.9M ▇▇▇▇█▎    In: 15.1M · Out: 466k · Cache:  78.9M (Hit: 83.9% ⚡) [17.2%]
   #5 def         62.0M ▇▇▇▋      In:  6.9M · Out: 168k · Cache:  54.7M (Hit: 88.8% ⚡) [11.3%]

💤 LIGHT / DORMANT (<5% of fleet volume · 1 profiles)
   #6 tigas666    21.6M █▏        In:  3.1M · Out:  62k · Cache:  18.3M (Hit: 85.5% ⚡) [ 3.9%]
─────────────────────────────────────────────────────────────────────────────────────────────
💡 Optimization Insight: Profiles 'csgotiago' & 'tmg' account for 50.3% of all token traffic.
   High cache efficiency (88.5%+) successfully prevented ~243.9M tokens of redundant prompts.
```

#### Key Highlights
* **Ranked Driver Leaderboard**: Ranks accounts `#1` through `#N` by volume and groups them into operational tiers.
* **Macro Composition Meter**: Full-width segmented color bar showing aggregate fleet token distribution.
* **Actionable Optimization Insights**: Explains token traffic concentration and prompts savings.
* **Sparkline Meters**: Vertical high-resolution sparkline blocks (` ▂▃▄▅▆▇█`).

---

## 3. Comparison Matrix

| Metric / Dimension | Option 1: Modern Unified Table | Option 2: Multi-Column Cards & Matrix | Option 3: Executive Analytics Dashboard |
| :--- | :--- | :--- | :--- |
| **Lines per Account** | **1 line** | 0.3 lines (Matrix) / 2 lines (Cards) | 1.2 lines (Tiered groups) |
| **Max Accounts on 1 Screen (24 lines)**| ~25 accounts | **50–100 accounts** | ~18 accounts |
| **Visual Style** | Sleek unified telemetry table | Multi-column dashboard / Treemap | `btop` / `htop` system monitor |
| **Breakdown Integration** | Inline composition & cache hit | Embedded card mix & matrix tier | Tiered leaderboard with pills |
| **Sorting** | Volume (desc), Hit %, or Name | Rank index / Grid layout | Automatic tiering by volume % |
| **Terminal Width Support** | 80 to 140+ cols responsive | Best on 100+ cols (2–3 cols) | 80 to 140+ cols responsive |
| **Best For** | Daily developer inspection & scripts | Large account fleets (20–100+) | Token cost & traffic management |

---

## 4. Complementary Pairing Guide (Usage + Tokens)

Because you will be choosing one option for `usage` and one for `tokens`, here are the natural aesthetic pairings:

| Style Pairing | Usage Choice | Tokens Choice | Cohesive Theme |
| :--- | :--- | :--- | :--- |
| **Pairing A: Modern Unified** *(Recommended)* | **Option 1 (Table)** | **Option 1 (Table)** | Ultra-clean, single-row high-density tables with fractional sub-block bars across both commands. Compact, fast, fits all screens. |
| **Pairing B: Fleet Grid & Cards** | **Option 2 (Cards/Grid)** | **Option 2 (Cards/Grid)** | Multi-column dashboard cards and matrix heatmaps for managing massive pools (20–100+ accounts). |
| **Pairing C: Executive Telemetry** | **Option 3 (Tiers)** | **Option 3 (Tiers)** | `btop`/`k9s` system monitor aesthetic with smart operational tiers, sparklines, and actionable recommendations. |
| **Pairing D: Hybrid** | **Option 1 (Table)** | **Option 3 (Dashboard)** | Fast everyday quota table for usage + deep analytical telemetry for tokens. |
