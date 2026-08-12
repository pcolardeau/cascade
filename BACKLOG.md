# CASCADE — Backlog

Ideas that are worth doing but aren't being built yet. Each entry says what it
is, why it's valuable, and what actually stands in the way — the blockers are
the point, since several of these look cheaper than they are.

**Done since this file was written:**
- The probability/profit-magnitude sort toggle (was item 3) — Balanced /
  Safer / Biggest swing, client-side, on all three boards.
- The minimum reward:risk floor on spreads (was item 0) — framed as a
  validity guard, not a risk preference, and disclosed in the board's note.
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

## 5. Modeled historical backtest for the Weekly board

**What:** Simulate "buy the top-scored setup every Monday for the past N
years" using Yahoo daily history (already fetched for the correlation feature)
plus a stdlib Black-Scholes approximation to price the option at entry and
exit.

**Why:** Years of statistical signal immediately, versus one paper trade per
day from the forward log. It's the only path to a sample size large enough to
actually calibrate the scoring weights rather than guess them.

**Blockers / notes:**
- **This is a model, not a backtest, and must be labeled that way.** The
  README is already explicit that no historical intraday options data exists
  on this free feed — that's why the 0DTE board has a forward log instead.
  Modeled prices inherit every assumption of the pricing model (constant vol,
  no skew, no early exercise) and will systematically disagree with what
  could actually have been filled.
- Needs a historical volatility input per date to price with. Realized vol
  from trailing daily bars is the honest choice, but it is *not* the implied
  vol the contract would really have traded at, and the gap between them is
  exactly the variance risk premium the strategy is exposed to. Overstating
  results here is the default failure mode.
- No bid/ask spread exists in a modeled price. Since spread is 15–25% of
  every live score in this app, a modeled backtest that ignores it will look
  materially better than reality. Apply a modeled spread haircut.
- The realized-vol input this needs now exists (`realized_vol()` in server.py),
  so that dependency is cleared.
