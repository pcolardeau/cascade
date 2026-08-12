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

## 2. Kelly-fraction position sizing

**What:** Every board currently models exactly 1 contract (`contract_cost`,
`max_loss` both assume it). Once a paper-trading log has enough closed trades
to establish a real win rate and average win/loss, use those to suggest a
fractional-Kelly position size instead.

**Why:** Turns the track record from a scorecard into an input. "This setup has
historically hit 62% with a 1.4:1 payoff, so risk X% of the account" is the
question a screener naturally raises and currently can't answer.

**Blockers / notes:**
- Needs a meaningful sample first. Full Kelly on a handful of trades is
  actively dangerous — the estimate's variance swamps the signal. Gate this
  behind a minimum closed-trade count and default to fractional (½ or ¼)
  Kelly, never full.
- Requires an account-size input, which the app has never had. That's a new
  piece of user state to persist and a new (small) responsibility — worth
  being deliberate about rather than sneaking in a number field.
- The weekly log this depended on now exists, so both boards can feed it —
  but neither has a settled sample yet. The weekly board's first result
  can't arrive until a logged pick reaches its expiry.

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

Not a to-do — a result worth not losing, and one that inverted once the
volatility input was fixed.

Buying a ~1% ITM weekly call over 2 years of daily bars, held to expiry,
3% execution haircut. The two columns are the SAME trades priced off
different volatility:

| underlying | trades | win rate | priced off REALIZED vol | priced off REAL implied vol |
|---|---|---|---|---|
| SPY | 464 | 46.1% | +21.6% | **+5.8%** (^VIX) |
| QQQ | 464 | 47.6% | +26.8% | **+14.5%** (^VXN) |
| IWM | 464 | 46.8% | +21.5% | *no proxy — realized only* |

**The realized-vol number was an artifact, and it was most of the result.**
Options trade at implied, not realized; implied normally sits above it, so
pricing entries off realized bought them too cheaply. Correcting that
removed ~73% of SPY's apparent edge.

What survives is thin. On SPY, at real implied vol:

| vol premium | avg / trade | total |
|---|---|---|
| ×1.00 | +5.8% | +$41,334 |
| ×1.10 | −0.0% | +$12,899 |
| ×1.25 | −7.7% | −$30,466 |

So a 10% error in the volatility assumption — or equivalently, execution
slightly worse than the 3% modeled — erases it entirely, and a 25% error
makes it a losing strategy. Win rate is below 50% throughout, meaning
what profit exists rides on a few large winners rather than consistency.

**Caveats that remain, in order of how much they'd move the number:**

- IWM still has no implied-vol proxy (^RVX 404s on this feed, ^VXD returns
  one bar), so its column is still the optimistic realized-vol figure and
  is not comparable to the other two.
- ^VIX/^VXN track the INDEX, not the ETF, and quote 30-day vol against
  ~7-day contracts — term structure is ignored, and short-dated vol is
  usually the more expensive end.
- Two years is one regime, with no sustained bear market.

**If anyone picks this up next:** a real IWM vol source, and short-dated
rather than 30-day implied vol. Both push the same direction — toward the
strategy looking worse, which is the direction worth being sure about.
