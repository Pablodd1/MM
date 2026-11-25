# Finding: his 1–10 score is fundamental, not technical

Discovered by retrieval over the corpus after 17 videos were transcribed. It changes the
rubric in `mmdesk/rubric.py`, which was built from a single video and got the anchor wrong.

## What he says

> "These are proprietary to me. They are not technical lines, by the way. They are fundamental.
> That's why nobody else makes these lines… it's basically a one to 10 scale. If you're at the
> green line, that's a 10. This stock is cheap."
> — `RJ8zTCJaJhU` @ 00:27:16

> "The gray lines are technical. The red green lines are fundamental. I gave you this stock down
> here around the green line."
> — `jCS3eoQiXHI` @ 00:48:31

> "My white lines, of course, are preliminary red, green lines. That means it busted above the red
> line. That means no, too expensive. And what did it do? It came all the way back down. The
> gravitational pull pulled Marvell back down to money marks line."
> — `MLydWWVcEDY` @ 02:01:26

## What this means

1. **Green line = fair value (or cheaper) = 10.** Red line = too expensive = 1. Price between the
   two interpolates the score. The lines are drawn from **fundamentals**, not from support and
   resistance.
2. Technical structure — support, resistance, trend — is a *separate* layer he plots in grey. It
   informs entries and exits, not the score itself.
3. Therefore a rubric built on "distance to support" and "reward/risk to the 60-bar high" is
   measuring the wrong thing. It produced a defensible-looking 8/10 for MSFT while the standard
   lens said unfavourable — the app was rewarding a chart pattern, not cheapness.

## What we do and do not know

**Known:** the scale runs green (10, cheap) → red (1, expensive); the lines are fundamental;
price reverting to them is a real force he trades ("gravitational pull").

**Not known, and not to be guessed:** which fundamental metric builds the lines. Candidates
appearing in his language across the corpus — revenue growth, earnings growth, forward multiples,
"fair value for how the company is operating" — but he has not stated a formula in the material
transcribed so far. **We will not invent one.**

## Consequence for the code

- `rubric.fair_value_score(price, green_line, red_line)` implements the mechanic he describes,
  with the lines supplied by the user (or by a future valuation model that must itself be
  documented and cited).
- The technical rubric stays available but is relabelled `technical_proxy_v1` and must never be
  presented as his score.
- When neither bands nor components are supplied, the card says the score is unavailable rather
  than substituting a proxy.

This is the corpus earning its keep: one video gave a plausible story, seventeen gave the actual
mechanic.
