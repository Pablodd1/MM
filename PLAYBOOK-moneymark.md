# Money Mark — what he actually teaches

Source: `9PhWx9rzIaU` — *How I Make Money On Stocks (READ THE DESCRIPTION FIRST!)*, 60:43,
9.2K views. Transcript: `transcripts/9PhWx9rzIaU.txt` (1,819 segments, 9,781 words).
Everything below is his claim, quoted or paraphrased, with the timestamp it came from.
My annotations are marked **[CTO]**.

## The five rules he states

**1. Risk/reward is the whole method.**
> "We're going to talk about risk reward charts. That is the cheat code to what I do. Most
> nobody makes risk reward charts." — 01:44

He scores a stock 1–10 on attractiveness: 10 means price is sitting at support with little
downside, 1 means "fully priced, there's no upside in that". Position size follows the score —
a 1 gets "maybe 1%, maybe 0%" of the portfolio (27:51–28:29).

**2. The three-point rule.**
> "If the stock moves three points from a 10 to a nine to an eight to a seven, you should
> probably take some profits. This is more art than science." — 28:57–29:07

**3. Sell half when you double.**
> "If your $1,000 has gone to $2,000 and you sell half and put the thousand in your pocket,
> you have guaranteed yourself a win on that trade." — 29:59–30:07

**4. Market alert levels drive everything.**
- **Green** — normal conditions, stay invested (25:07–25:30)
- **Yellow** — "the indices become too expensive, overvalued… I don't know what's going to
  cause the market to drop, but something's going to, because the market's too expensive"
  (13:59–14:12). Action: no speculative stocks, be "risk reward picky" (41:49–42:00).
  He says most of his alerts are yellow.
- **Orange** — "the market is almost assuredly going to go down". Used once, COVID
  (14:33–15:03). Action: sell everything except your favourites, and hedge —
  "if you have $10,000 in your stocks, make sure you have at least $10,000 in RWM to protect
  those positions" (42:02–42:18).
- **Red** — "when I know exactly what's happening, why it's happening, and how severe it is."
  Twice in his career (15:05–15:15).

**5. Cash is a position.**
> "Over half of my money as this video is being recorded is in cash, actually in money market
> funds earning 4%." — 16:54–17:02

And an explicit anti-day-trading stance: "less than 5% of people are even capable of earning
above market returns as day traders… better to take the slow and steady approach" (17:46–18:06).

## The hedge: RWM

RWM is the inverse Russell 2000 ETF. He used it as his COVID hedge:
> "RWM was my largest position. It was my most profitable position during the COVID decline."
> — 42:44–42:48

He treats it as insurance, not an investment: "you don't want to own RWM… if you buy RWM
[in an orange alert] you're going to make money on RWM" (38:53–39:20).

## What is missing — and it matters

**[CTO] The alert triggers are not defined.** Yellow is "the indices become too expensive" —
no metric, no threshold, no indicator. In his own words he doesn't know what will cause the
drop, only that something will. That is a feeling with a label, and it cannot be reproduced by
anyone else, including a bot.

**[CTO] The 1–10 score is subjective.** He says so: "more art than science" (29:07). Two people
looking at the same chart will score it differently, and he only publishes the score as a line
on a chart, not as a dataset.

**[CTO] RWM is not a hold instrument.** Inverse ETFs reset daily. Over a fast crash like
February–March 2020 they work as advertised; over a slow grind down, volatility drag eats the
thesis. "Make sure you have at least $10,000 in RWM" has no time component, and that omission is
where followers get hurt.

**[CTO] He can exit without telling you.** His own disclaimer: "I may enter, exit, or reverse the
position at any time without notice" (01:04–01:08). So the strategy is not actually copyable —
you inherit his entries but not his exits.

## What can be tested from the corpus

1. **His alerts.** Date every "yellow / orange / red" call across 130 hours of streams, then
   measure the forward 1-, 3-, 6-, 12-month return of SPY/QQQ after each. If his yellow alerts
   precede flat or negative periods more often than chance, that is real information.
2. **His picks.** Every named ticker with a direction and date, measured against SPY over the
   same window. Hit rate alone is meaningless — average excess return and the size of losers is
   what tells you whether following him beats an index fund.
3. **The hedge.** Model a 1:1 RWM overlay from each alert date, net of expense ratios and
   volatility decay, and compare drawdown against simply holding cash.

None of this requires trusting him. That is the point.
