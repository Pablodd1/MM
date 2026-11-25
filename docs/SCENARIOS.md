# The synthetic crowd

Six investment styles read the same grounded data and disagree on purpose. The output is a
distribution over stances plus a dispersion figure — **not a forecast**.

## Why it is built this way

MiroFish-style engines spin up hundreds of LLM agents that post at each other on a simulated
feed, then read the resulting sentiment. It is a good way to rehearse a *public reaction* to an
event. It is a bad way to price a stock, for three reasons:

1. **The agents share a model.** Same weights, same prompt, same corpus → correlated errors. A
   thousand agents produce one opinion a thousand times, with borrowed confidence.
2. **Nothing scores them.** No Brier score, no paper trail, no horizon. Simulated posts are not
   evidence, and a convincing transcript of a fake argument is the easiest thing to produce.
3. **The output looks like a probability.** It is not one.

So this implementation does the opposite of scale: **six** styles, each a small deterministic
policy, each reading the same `Facts` object. There is no language model in the loop, no
persona biography, and no invented demographics. What carries information is the *spread*: when
value says add and the risk manager says trim, the disagreement is the finding.

## The six

| Persona | Mandate | Reacts to |
|---|---|---|
| Value investor | buy below fair value | the fundamental band score; abstains (`pass`) when no bands are supplied |
| Momentum trader | buy strength | trend −3..3 and recent momentum |
| Risk manager | survive first | 60-day annualised volatility and drawdown from the 60-bar high |
| Cash holder | get paid to wait | cheapness and where price sits in its range |
| Growth buyer | pay for expansion | trend, provided valuation is not stretched |
| Contrarian | buy fear | deep drawdown into a cheap valuation; trims at the top of the range |

Each returns a stance (`add`, `hold`, `trim`, `pass`), a conviction in [0, 1], and a one-line
reason that names the number it acted on.

## What the payload says

```
modal_stance            the weighted plurality (weight, not headcount)
weighted_distribution   conviction-weighted share per stance
dispersion              1 − (modal headcount / agents); a unanimous crowd is suspicious
label                   "synthetic crowd … not a forecast, not a probability"
what_this_is_not        four explicit limits, in the response itself
```

## Measured example (NVDA, 2026-11-24)

```
modal trim · dispersion 0.667
Value      hold  0.40   price is mid-band (score 5.0/10)
Momentum   trim  0.70   trend -3/3: the tape is against the position
Risk       trim  1.00   annualised volatility above the risk budget
Cash       hold  0.40   nothing forces a decision here
Growth     hold  0.35   trend or valuation does not justify adding
Contrarian hold  0.30   no dislocation to exploit
```

Four hold, two trim, dispersion 0.67 — the honest read is "this crowd is split", which is more
useful than a single confident answer.

## Known limits

- **Correlated inputs.** All six read the same bars. If the data is wrong, all six are wrong
  together. The dispersion figure measures disagreement, not independence.
- **No learning.** Styles do not update. There is no memory of being right or wrong.
- **Thresholds are judgements.** 60% volatility and −25% drawdown are conventional, not
  derived. They are in one file and easy to argue with.
- **Not a survey.** These are not Miami investors or any real population; they are rules.
- **Licensing.** No MiroFish code is used. MiroFish is AGPL-3.0, which would force source
  disclosure on a hosted product; the *idea* was borrowed, the code was not.
