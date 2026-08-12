# CASCADE — Backlog

Ideas that are worth doing but aren't being built yet. Each entry says what it
is, why it's valuable, and what actually stands in the way — the blockers are
the point, since several of these look cheaper than they are.

**Done since this file was written:**
- The probability/profit-magnitude sort toggle (was item 3) — Balanced /
  Safer / Biggest swing, client-side, on all three boards.
- The weekly forward paper-trading log (was item 1). One resolver now serves
  both boards by settling on the contract's expiry rather than the log date;
  for 0DTE those are the same day, so existing records were unaffected.

---

## 0. Minimum reward:risk floor on the spreads board

**What:** `get_debit_spreads` accepts any pairing where `0 < net_debit <
width`. That admits spreads like SPY 735/740 at a $498 debit for a $2 max
gain — a 0.00× reward:risk. Technically a valid debit spread; economically
pointless, since it needs a ~99.6% win rate just to cover the debit, before
commission.

**Why:** They're invisible under the default Balanced sort (the Spread Score
buries them on the reward term), but "Safer" ranks by short-leg delta alone
and floats them straight to the top — so the sort toggle that just shipped
made a pre-existing wart newly prominent.

**Blockers / notes:**
- Mostly a question of what the floor should be, not how to implement it. A
  fixed minimum (say 0.15×) is crude but honest; scaling it with DTE would be
  better-founded, since a longer hold needs more edge to be worth the capital.
- Alternative to filtering: keep them but flag them, consistent with how
  every other risk signal on these boards behaves. That may be the more
  in-keeping answer — the boards' whole design is "penalize and explain,
  don't hide."

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

## 4. Dealer-positioning / gamma overlay near candidate strikes

**What:** Compute open-interest-weighted gamma exposure across the *full*
chain (not just the ITM subset the Snipe boards filter to) to flag likely
pin levels and support/resistance, then overlay those levels on the candidate
list and/or the existing network visualization.

**Why:** It's positioning context nothing else in the app provides, and it ties
the options work back into CASCADE's actual thesis (cross-asset effects and
propagation) rather than being a bolted-on screener.

**Blockers / notes:**
- The full chain is already fetched per underlying, so the raw data is there
  — but `_parse_chain` currently drops zero-volume contracts, and pin
  analysis specifically cares about high-OI/low-volume strikes. That filter
  would need to become conditional.
- CBOE's feed carries `delta` and `theta` but gamma isn't confirmed present
  in the payload — needs verification before designing around it. If absent,
  it can be approximated from delta across adjacent strikes, with the
  accuracy caveat that implies.
- Genuine interpretation risk: dealer-positioning inference from public OI
  is a heuristic built on assumptions about who's long vs short, which the
  data doesn't actually say. Presenting it as fact would be the most
  overconfident thing in the app. Label accordingly.

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
