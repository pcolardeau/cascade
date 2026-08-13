# CASCADE — Backlog

Everything outstanding, plus the findings worth not re-deriving. Written to be
self-contained: a fresh session should be able to pick up from this file alone.

Each entry says what it is, why it matters, and what actually stands in the
way — the blockers are the point, since several of these look cheaper than
they are.

**State as of this writing:** working tree clean, 299 tests passing, no open
PRs (all nine merged to `main`), no TODO/FIXME markers in the source. The
watchlist has been cleared to empty.

---

# 1. Blocked — do not re-investigate

## 1.1 IWM implied volatility

There is **no usable IWM implied-vol history on this feed**. Probed directly:

| ticker | result |
|---|---|
| `^RVX` (Russell 2000 vol) | 404 |
| `^RUTVIX` | returns nothing |
| `^VXD`, `^VIX3M` | a single bar |

IWM therefore falls back to realized vol and **says so** in `vol_source`. Its
simulation column is the optimistic realized-vol figure and is **not
comparable** to SPY/QQQ. Borrowing SPY's volatility would be a different
underlying's risk relabelled, which is why the fallback is explicit rather
than silent.

Only worth revisiting if the data source changes.

---

# 2. Waiting on time, not on code

## 2.1 Kelly from realized trades (~5 months out)

Kelly sizing is implemented and **correctly refuses** below 100 settled
trades. The live 0DTE log holds 3 closed trades, all winners — naive Kelly on
a 100% win rate implies betting essentially the whole bankroll, which is
exactly the failure the gate exists to prevent.

Nothing to build. It starts answering at ~100 settled trades, roughly five
months of daily logging. Even then the estimate is noisy: win-rate error
scales like 1/√n.

## 2.2 First weekly-log settlement

The weekly paper log records daily and settles against the underlying's close
on the contract's **expiry**, so the first real result cannot arrive until a
logged pick reaches expiry (~7–10 days after logging). The settlement path is
already verified end-to-end against real market data — a SPY C732 expiring
2026-08-03 settled against the real 757.67 close for $1,367, matching an
independent calculation exactly. Only the sample is missing.

---

# 3. Open work

## 3.1 No JavaScript tests at all — the largest gap

`index.html` is **3,840 lines** and is now the biggest file in the project,
carrying the force simulation, cascade propagation, radial layout, three
option boards, the index view, the watchlist and all rendering. There is not
a single automated test for any of it — `test_server.py` covers the backend
only. Every bug found in the frontend this session was found by driving a real
browser.

Concretely, bugs that reached `main` and were caught only by manual browser
checks:
- the spreads board returned early and skipped the shared trailer, so the
  gamma section silently vanished on one mode;
- switching boards fetched the scan but not the log, so the weekly log
  rendered "unavailable" while its endpoint was fine.

Options: a headless-browser smoke pass over the render paths, or extracting
the pure logic (cascade propagation, scoring, layout maths, `sortSnipeRows`)
into testable functions. The second is more work and more durable.

## 3.2 Spreads board has no paper log

The 0DTE and weekly boards both record and settle. Spreads doesn't, because
settling a two-leg position needs its own math — max-gain/max-loss resolution
at expiry rather than a single intrinsic value. `SNIPE_LOG_BOARDS` is already
the extension point; the resolver is the work.

## 3.3 QQQ isn't comparable across its own ranges

QQQ's term correction borrows the SPX 9d/30d ratio, which needs `^VIX9D`. At
10y that fetch falls back often enough that QQQ's 10y run uses plain 30-day
`^VXN` while its 2y/5y runs are term-adjusted. So QQQ's ranges aren't
comparable **to each other**, and its 10y figure understates it. Either force
the adjustment or report its absence more loudly per-range.

## 3.4 `^VIX9D` truncation is mitigated, not eliminated

Yahoo answers HTTP 200 with a truncated series. Retries with escalating
backoff took `^VIX9D` selection from **1/5 to 5/6** at the 10y range, and the
fallback is always labelled — but roughly one run in six still quietly uses
30-day vol instead of 9-day and reports a different number. A stronger fix
would cache a known-good series to disk rather than re-fetching.

## 3.5 Static file server has no image content-type

`_send_file`'s ctype chain maps `.html`, `.js` and `.css`, then falls through
to `application/octet-stream`. `screenshot.png` in the repo root would serve
as a download rather than an image. One line, no risk.

## 3.6 Sector factor exposures are hand-chosen, not fitted

The four sectors added with the S&P 500 library (Utilities, Real Estate,
Materials, Communication Services) have `[mkt, sector, rate, oil]` exposure
vectors picked to reflect known behaviour — utilities as a low-beta,
rate-negative bond proxy; real estate the most rate-sensitive; materials the
only one with real oil beta.

They were sanity-checked and behave correctly (NEE comes out negatively
correlated with US10Y at −0.38 and DXY at −0.41, positively with AMT at
+0.44), but they are **judgement, not fitted**. Fitting them to real
historical returns would make the synthetic correlations defensible rather
than merely plausible.

## 3.7 Live correlation still only covers the curated graph

"Sync History" rebuilds correlations from real 6-month history for the
rendered graph only. Extending it to all 503 S&P names would take
**~6 minutes** serialized behind Yahoo's rate limiter (measured), which is why
the index view uses the synthetic factor model and says so in its caption. A
batched or background sync would be the way in.

## 3.8 Per-symbol cost on a cold scan

`realized_vol` adds a Yahoo history call per symbol per cold scan, on top of
chain and quote fetches. Fine at three index ETFs; it is the thing that would
bite first if the scan universe ever widens.

## 3.9 Cosmetic

- `/favicon.ico` 404s on every page load. Pre-existing and harmless, but it
  appears in every console check and adds noise to real debugging.

---

# 4. Accepted trade-offs — context, not tasks

Recorded so they aren't re-opened as though they were oversights.

- **cosmos.gl is only in the index view.** The curated graph keeps SVG because
  cosmos.gl cannot draw per-node text labels or the dashed 1st–5th hop-order
  encoding, and the pinned radial cascade fights its GPU simulation. Measured
  at 42 nodes it was a net loss; at 503 it earns its 693 KB. Both renderers
  coexist deliberately.
- **The vendored bundle is the UMD build (693 KB), not the smaller `+esm`
  (309 KB).** jsDelivr's `+esm` carries absolute imports like
  `/npm/d3-selection@3.0.0/+esm` that the local server would 404 on — it would
  have kept a silent CDN dependency and broken offline rendering. The UMD
  build is self-contained.
- **Watchlist restore only resolves S&P 500 names offline.** A non-S&P ticker
  needs an `/api/lookup` round trip each, which at boot would serialize behind
  the rate limiter. Such tickers stay saved and can be re-added by searching.
- **The index view's correlations are synthetic**, from the same factor model
  as the curated graph — not live. Stated in its caption.
- **The S&P 500 library is a literal in `index.html`**, fetched once. It goes
  stale slowly; that is the price of no build step and offline operation.
- **Scanning is restricted to SPY/QQQ/IWM** (`INDEX_FUNDS`), which correlate
  0.77–0.92 with each other — close to one bet made three ways. Accepted for
  the execution advantage in §5.4. `validate_scan_symbols(..., allow_any=True)`
  is the escape hatch.
- **No build step, stdlib only.** This constrains every dependency decision
  above and is load-bearing, not incidental.

---

# 5. Findings worth not re-deriving

## 5.1 The modeled simulation

Buying a ~1% ITM weekly call, held to expiry, 3% execution haircut. The number
moved four times as inputs got closer to correct; the **direction** of each
move matters more than any single figure.

| input corrected | SPY avg/trade | why it moved |
|---|---|---|
| realized vol, 2y | +21.6% | wrong instrument — options trade at implied |
| implied 30-day (`^VIX`), 2y | +5.7% | right instrument, wrong tenor |
| implied 9-day (`^VIX9D`), 2y | +10.4% | right tenor for ~7-DTE contracts |
| **implied 9-day, 10y** | **+13.3%** | **more than one regime** |

Stable across regimes on a consistent tenor — 2y +10.7% (465 trades, 48.6%
win), 5y +12.3% (1,218, 50.4%), 10y +13.3% (2,476, 52.3%) — spanning the 2018
Q4 selloff, the 2020 COVID crash and the 2022 bear market. The short sample
was, if anything, *understating* it.

**What has not changed and shouldn't be forgotten:** the win rate is barely
above a coin flip (52.3%) and the distribution is violently skewed — best
single trade **+409.8%**, worst **−100%**, with **22% of trades a total
loss**. The positive average is carried by a few large winners, not by
consistency. Execution cost is second-order: removing the 3% haircut entirely
moves +13.3% to only +15.0%.

Sensitivity to the volatility assumption remains the dominant unknown:

| vol premium | 2y | 10y |
|---|---|---|
| ×1.00 | +10.4% | +13.3% |
| ×1.10 | +4.8% | +7.8% |
| ×1.25 | −2.9% | **+0.3%** |

Yahoo returns **monthly** bars for `max`, so 10y is the longest usable daily
window. These series track the INDEX, not the ETF.

## 5.2 Kelly

Sizing maximizes `E[log(1 + f·r)]` over the empirical returns, **not** the
binary `(p·b − q)/b` — that formula assumes two outcomes, and these payoffs
run from −100% to +410%. (A test pins that the general form reproduces the
closed-form answer on a genuinely binary bet.)

On the modelled SPY distribution (n=2,475, 10y): full **15.8%**, half 7.9%,
**quarter 3.9% (headline)**. Quarter-Kelly is the headline rather than a hedge
because full Kelly is growth-optimal only with a *known* distribution — this
one is modelled. Assuming volatility 10% higher moves f\* from 15.8% to 9.9%:
a **37% smaller position from a modest input error**. That stress case ships
as a first-class field, not a footnote.

It reports a **fraction of bankroll and never asks for an account size** — the
fraction is the part derived from data, and keeping money out of the app
avoids storing something sensitive for no analytical gain.

Kelly's 3.9% is small *because* the distribution is violent — it and the
simulation's win rate are the same fact stated twice.

## 5.3 Gamma — the peak strike is empty, per-candidate isn't

Across SPY/QQQ/IWM at 7/30/90 DTE, **8 of 9 peak-gamma strikes sat within
0.5% of spot** (median 0.10%) — the peak restated the spot price already in
the header. The one exception, QQQ at 90 DTE with a real put wall 3.3% below,
never appears on boards that look 0–10 DTE.

Per-candidate concentration does carry signal: **1% to 53% of peak** across
twelve candidates scoring 72–77, uncorrelated with score. SPY P780 (74.0) sat
at 53% while C752 (74.3) sat at 1%. The board rates them equivalent; they are
in completely different gamma environments.

The column's fetch must be **deep** (`top=250`, not 14): with a shallow fetch
a strike outside the slice reads as `0%` rather than "not measured" — a wrong
answer, not a missing one. The chart slices client-side for display.

## 5.4 Index funds vs single names

Measured on live ~7-DTE ITM contracts:

| | median spread | p90 | median OI |
|---|---|---|---|
| index ETFs | **2.78%** | 8.4% | 164 |
| single names | 6.20% | 14.6% | 168 |

~2.2× tighter on spread — the cost that matters, paid on entry *and* exit. The
advantage is in **spread, not depth**: per-contract open interest is
effectively identical, because index chains spread far larger total OI across
many more strikes.

## 5.5 The DTE window is ±3 for a reason

`[target−3, target+3]` spans exactly 7 calendar days, and weekly expiries are
7 days apart, so any 7-day span contains exactly one Friday. That makes 3 the
**smallest** window guaranteeing a hit. At ±2 the window is [5,9] and returns
**zero** candidates for names carrying only Friday weeklies — SMCI, WMT and
COIN all list expiries at 3/10/17 DTE with nothing between. ±4 caught nothing
±3 didn't.

## 5.6 Every node needs at least one edge

`buildGraph` adds at most one statistical edge per node at |r| ≥ 0.5. A saved
stock could land **orphaned**: NEE peaked at 0.438 against its nearest
neighbour, so it had zero edges and a cascade from it reached exactly 1 node.
The model was right — a low-beta utility genuinely correlates weakly with tech
— but a node that can't cascade is the app's core feature silently not working
for it. Every node now gets its strongest correlate, drawn at its true weak
|r| so it renders thin rather than pretending to be strong.
