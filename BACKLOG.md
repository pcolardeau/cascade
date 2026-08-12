# CASCADE — Backlog

Ideas that are worth doing but aren't being built yet. Each entry says what it
is, why it's valuable, and what actually stands in the way — the blockers are
the point, since several of these look cheaper than they are.

Items 1–5 of the original Snipe-improvement list are being implemented now and
so aren't repeated here. This file is 6–10 of that list.

---

## 1. Weekly Snipe Log — forward track record for the ~7-DTE board

**What:** The 0DTE Snipe board has a forward paper-trading log (`snipe_log.json`)
— a daemon thread snapshots the top-scored pick ~30 min before the close, and
the next read on a later day settles it against the underlying's real closing
price. The Weekly board (`get_itm_scan_weekly`) has no equivalent, so there's
no honest record of whether its scoring actually picks winners.

**Why:** Without it, the Weekly Score is an untested hypothesis. The 0DTE log is
the only reason the 0DTE score's calibration can be argued about with evidence
instead of taste.

**Blockers / notes:**
- Settlement is genuinely different: the 0DTE log settles against *the next
  day's* close, which works because the option expires the same day it was
  logged. A weekly pick has to be settled against the underlying's close on
  its actual expiry date, which may be 5–9 days after the snapshot — so the
  resolver needs to look up a specific historical date, not just "the most
  recent close."
- The existing scheduler thread fires once per trading day near the close. A
  weekly log needs entries to stay open across many scheduler runs and only
  resolve when their own expiry date has passed.
- Worth reusing `resolve_snipe_log`'s P&L math (intrinsic-at-close minus entry
  ask, times 100) — that part is horizon-independent and already tested.

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
- Depends on backlog item 1 for the weekly horizon (0DTE could use its
  existing log today).

---

## 3. Expose the probability/profit-magnitude split in the UI

**What:** `get_itm_scan_weekly` already returns `abs(delta)` (probability) and
`scenarios.favorable.pnl_pct` (profit magnitude) unscaled, alongside the
blended `weekly_score` — deliberately, because those two axes pull against each
other and a single number can't honestly collapse them. Nothing in the UI
surfaces that yet. Add a "Safer / Balanced / Biggest swing" sort toggle.

**Why:** Cheapest item on this list by a wide margin — the backend data already
exists and is already returned. It's pure front-end work in `renderSnipe()`.

**Blockers / notes:**
- The Weekly board isn't wired into the UI at all yet (only
  `/api/options/itm-scan` is fetched). That has to happen first, and is
  arguably part of implementing item 1 of the active list.
- Design question worth settling: does the toggle re-sort client-side from
  data already fetched (simple, instant) or re-request with a sort param
  (server authoritative, one more round trip)? Client-side re-sort is
  probably right given the row count is capped at ~20.

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
- Depends on active-list item 2 (realized-vol computation) for the vol input.
