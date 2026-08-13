# CASCADE — Backlog

Ideas that are worth doing but aren't being built yet. Each entry says what it
is, why it's valuable, and what actually stands in the way — the blockers are
the point, since several of these look cheaper than they are.

**Done since this file was written:**
- The probability/profit-magnitude sort toggle (was item 3) — Balanced /
  Safer / Biggest swing, client-side, on all three boards.
- The minimum reward:risk floor on spreads (was item 0) — framed as a
  validity guard, not a risk preference, and disclosed in the board's note.
- Gamma concentration by strike, backend only (was half of item 4).
- The modeled historical simulation (was item 5). Its headline number is
  not the finding; the vol-premium sensitivity is. See below.
- The weekly forward paper-trading log (was item 1). One resolver now serves
  both boards by settling on the contract's expiry rather than the log date;
  for 0DTE those are the same day, so existing records were unaffected.

---

## 2. Kelly sizing — shipped, and mostly it refuses

Implemented, but the useful behaviour is the refusal. Recorded here because
the numbers are easy to misread.

**It reports a FRACTION of bankroll, and never asks for an account size.**
The fraction is the part actually derived from data; keeping the money out
of the app avoids storing something sensitive for no analytical gain.

**The binary formula doesn't apply.** `(p*b - q)/b` assumes two outcomes.
These payoffs run from −100% to +410%, with **22% of trades a total loss**,
so sizing maximizes `E[log(1 + f·r)]` over the empirical returns — the
general form the textbook one is a special case of. (A test pins that the
general form reproduces the closed-form answer on a genuinely binary bet.)

On the modelled SPY distribution (n=2,475, 10y):

| | fraction of bankroll |
|---|---|
| full Kelly | 15.8% |
| half | 7.9% |
| **quarter (headline)** | **3.9%** |

**Why fractional is the headline, not a hedge.** Full Kelly is growth-optimal
only if the distribution is known exactly. It isn't — it's modelled. On the
same trades, assuming volatility 10% higher moves f\* from 15.8% to 9.9%: a
**37% smaller position from a modest input error**. Kelly is far more
sensitive to its inputs than to the trades themselves, so that stress case
ships as a first-class field rather than a footnote.

**The gate.** Below 100 settled trades it returns a refusal instead of a
number. The live 0DTE log currently holds **3 closed trades, all winners** —
naive Kelly on a 100% win rate implies betting essentially the whole
bankroll. That is precisely the failure the gate exists to prevent, and it
will keep refusing for months, which is correct.

**What would make the realized figure trustworthy:** roughly 100 settled
trades, i.e. ~5 months of daily logging. Even then the estimate is noisy —
win-rate error scales like 1/sqrt(n).

---

## 4. Gamma overlay — UI half

**What:** `/api/options/gamma` and `get_gamma_exposure()` now exist: gamma
concentration per strike, calls and puts separate, in dollars of delta per 1%
move. Nothing in the UI shows it yet. Options: a panel on the Snipe tab, or
markers on the existing network/price visualizations at the high-concentration
strikes.

**Why:** It's positioning context nothing else in the app provides, and it's
the piece that ties the options work back to CASCADE's actual thesis rather
than being a bolted-on screener.

**Blockers / notes:**
- Both original blockers are resolved. Gamma is present in CBOE's payload
  (all 14,672 SPY rows; non-zero on 11,074), so no delta-difference
  approximation is needed. And `_parse_chain` now takes `require_volume`,
  so the untraded strikes holding 17% of open interest are reachable.
- The remaining risk is presentational, and it's the important one. The
  backend deliberately refuses to net calls against puts or infer dealer
  positioning, because open interest never says who holds which side. A UI
  that draws a single "gamma flip level" or labels a strike as a magnet
  would smuggle that claim back in through the visualization after the API
  carefully avoided making it. Show the two sides separately.
- Worth deciding: is the peak strike genuinely useful to a user screening
  single contracts, or is this a separate analytical view? On SPY it lands
  near the money and mostly restates "the most open interest is near the
  money", which may not earn its screen space on the candidate board.

---

## 5. What the modeled simulation actually said

Not a to-do — a result worth not losing. It has moved four times as the
inputs got closer to correct, and the direction of each move matters more
than any single number.

Buying a ~1% ITM weekly call, held to expiry, 3% execution haircut:

| input corrected | SPY avg/trade | why it moved |
|---|---|---|
| realized vol, 2y | +21.6% | wrong instrument — options trade at implied |
| implied 30-day (`^VIX`), 2y | +5.7% | right instrument, wrong tenor |
| implied 9-day (`^VIX9D`), 2y | +10.4% | right tenor for ~7-DTE contracts |
| **implied 9-day, 10y** | **+13.3%** | **more than one regime** |

**The regime caveat is now largely retired.** 2y (2024–2026) was a single
benign stretch, which was the biggest remaining unknown. 10y spans the 2018
Q4 selloff, the 2020 COVID crash and the 2022 bear market, and takes the
sample from 465 trades to **2,476**. Held on the same 9-day tenor throughout:

| range | trades | win rate | avg/trade |
|---|---|---|---|
| 2y | 465 | 48.6% | +10.7% |
| 5y | 1,218 | 50.4% | +12.3% |
| 10y | 2,476 | 52.3% | +13.3% |

The short sample was, if anything, *understating* it. Yahoo returns monthly
bars for `max`, so 10y is the longest usable daily window.

Sensitivity to the volatility assumption, still the dominant unknown, is
also more robust on the longer sample — a 25% vol error is now roughly
breakeven rather than a loss:

| vol premium | 2y | 10y |
|---|---|---|
| ×1.00 | +10.4% | +13.3% |
| ×1.10 | +4.8% | +7.8% |
| ×1.25 | −2.9% | **+0.3%** |

**What has NOT changed, and shouldn't be forgotten:** win rate is barely
above a coin flip (52.3%), and the trade distribution is violently skewed —
best single trade +409.8%, worst **−100%**. A positive average here is
carried by a few large winners, not by consistency, and a total loss on any
given week is an ordinary outcome rather than a tail. Execution cost is
second-order: removing the 3% haircut entirely moves +13.3% to only +15.0%.

**Caveats that remain, in order of how much they'd move the number:**

- IWM still has no implied-vol source at all (below) and is on the
  optimistic realized-vol figure — **not comparable** to the other two.
- QQQ's term correction is modelled (SPX curve borrowed), and at 10y it
  falls back to plain 30-day `^VXN`, so its 10y figure understates it
  relative to its own 2y/5y numbers. QQQ is not comparable across ranges.
- These indices track the INDEX, not the ETF.
- Still one market structure: no pre-2016 data at daily resolution here.

---

## 6. IWM implied vol — blocked, do not re-investigate

Probed directly on this feed: `^RVX` 404s, `^RUTVIX` returns nothing, and
`^VXD` / `^VIX3M` return a single bar. **There is no usable IWM implied-vol
history available**, so IWM falls back to realized vol and says so in
`vol_source`.

Recorded here so it isn't repeatedly rediscovered. Borrowing SPY's vol would be
a different underlying's risk relabelled, which is why the fallback is explicit
rather than silent.
