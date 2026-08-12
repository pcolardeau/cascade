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

Not a to-do — a result worth not losing, and one that has now moved twice as
the volatility input got closer to correct.

Buying a ~1% ITM weekly call over 2 years of daily bars, held to expiry, 3%
execution haircut. Same trades throughout; only the volatility used to price
entry changes:

| priced off | SPY avg/trade | why |
|---|---|---|
| realized vol (30d trailing) | +21.6% | wrong instrument — options trade at implied |
| implied, 30-day (`^VIX`) | +5.7% | right instrument, wrong tenor |
| **implied, 9-day (`^VIX9D`)** | **+10.4%** | right instrument, right tenor for ~7-DTE |

**A prediction I got wrong, recorded because the reasoning matters more than
the number.** The plan said switching to short-dated vol should make results
*worse*, on the assumption that short-dated vol is pricier. It's the opposite:
measured over 484 overlapping sessions, `^VIX9D` sits **below** `^VIX` on 83%
of days (median ratio 0.912). The VIX term structure is normally in contango
and only inverts under stress (observed range 0.68–1.33). So 30-day vol was
*overstating* entry cost for 7-day contracts by ~9%, and correcting the tenor
legitimately improved the estimate.

Current best estimate, SPY on 9-day implied vol:

| vol premium | win rate | avg / trade | total |
|---|---|---|---|
| ×1.00 | 48.5% | +10.4% | +$60,657 |
| ×1.10 | 46.8% | +4.8% | +$34,733 |
| ×1.25 | 43.5% | −2.9% | −$4,929 |

So it survives a 10% vol error now, where on 30-day vol it did not — but still
turns negative by 25%, and **win rate stays below 50% at every level**, meaning
the profit rides on a few large winners rather than consistency. Execution
costs matter less than the vol input: removing the 3% spread haircut entirely
only moves it +10.4% → +12.1%.

**Caveats that remain, in order of how much they'd move the number:**

- IWM has no implied-vol source at all (below) and is still on the optimistic
  realized-vol figure — **its column is not comparable** to SPY/QQQ.
- QQQ is still on 30-day `^VXN`; no short-dated NDX index is published, so it
  carries the same tenor error `^VIX9D` just fixed for SPY (i.e. QQQ's +14.5%
  is probably understated).
- `^VIX`/`^VIX9D` track the INDEX, not the ETF.
- Two years is one regime, with no sustained bear market.

---

## 6. IWM implied vol — blocked, do not re-investigate

Probed directly on this feed: `^RVX` 404s, `^RUTVIX` returns nothing, and
`^VXD` / `^VIX3M` return a single bar. **There is no usable IWM implied-vol
history available**, so IWM falls back to realized vol and says so in
`vol_source`.

Recorded here so it isn't repeatedly rediscovered. Borrowing SPY's vol would be
a different underlying's risk relabelled, which is why the fallback is explicit
rather than silent.
