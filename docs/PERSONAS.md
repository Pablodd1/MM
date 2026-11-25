# Personas: how many, how long, and what it costs

Answers to three questions with numbers instead of adjectives. Every figure below comes from
`scripts/persona_cost.py` (real token counts on real text) or from the simulation code itself.

## 1. Two different things are called "personas"

**The shipped crowd — six styles, deterministic, free.**

| | |
|---|---|
| Agents | 6 |
| Cost | **$0** |
| Latency | ~2 ms per run |
| Tokens | **0** |
| Time to add a seventh | one function, ~20 lines, no training |
| What it measures | how different *mandates* treat the same facts |

Scaling this is a code decision, not a budget decision. 60 styles would still cost $0 and run in
milliseconds. The limit is honesty, not money: every style is a hand-written rule, and ten styles
that all weight the same three inputs are ten copies of one opinion.

**The MiroFish-style crowd — language models with biographies, posting and reacting.**

| | |
|---|---|
| Agents | 50 – 5,000 |
| Cost | $3 – tens of thousands (tables below) |
| Latency | seconds per agent-day, wall-clock |
| What it measures | how *simulated people* talk themselves into positions |

## 2. Cost, measured

Token counts, from `scripts/persona_cost.py` on real prose: a persona biography is **81 tokens**,
a post is **75**, a reaction is **82**. One agent-day = 1 post + 3 reactions = **321 output
tokens**, plus the bio and instructions re-sent on every call (**201 input tokens** per call,
4 calls per agent-day).

Input tokens (millions), at 4 calls per agent per simulated day:

| agents | 1 day | 1 week | 1 month | 3 months | 6 months | 1 year | 3 years |
|---|---|---|---|---|---|---|---|
| 6 | 0.0 | 0.0 | 0.1 | 0.4 | 0.9 | 1.8 | 5.3 |
| 50 | 0.0 | 0.3 | 1.2 | 3.6 | 7.2 | 14.7 | 44.0 |
| 100 | 0.1 | 0.6 | 2.4 | 7.2 | 14.5 | 29.3 | 88.0 |
| 250 | 0.2 | 1.4 | 6.0 | 18.1 | 36.2 | 73.4 | 220.1 |
| 1,000 | 0.8 | 5.6 | 24.1 | 72.4 | 144.7 | 293.5 | 880.4 |
| 5,000 | 4.0 | 28.1 | 120.6 | 361.8 | 723.6 | 1,467.3 | 4,401.9 |

Output tokens (millions) are roughly 40% of those numbers.

Cost in dollars:

| scenario | cheap hosted ($0.15/$0.60 per M) | mid hosted ($0.30/$1.20) | frontier ($3/$15) |
|---|---|---|---|
| 100 agents, 3 months | **$3** | $6 | $65 |
| 1,000 agents, 1 year | **$114** | $229 | $2,638 |

**The money is not the binding constraint — this is a rounding error.** 1,000 agents simulating a
year of trading days costs about the same as two lunches. Anyone who says the obstacle to this is
API bills is selling you something.

## 3. What actually binds

1. **Simulated days, not calendar days.** 1 year = 365 simulated trading days = ~14,600 calls per
   agent. At 1,000 agents that is **1.46 million model calls**, and at ~1 s each with 8 parallel
   workers that is **~50 hours of wall clock**. The constraint is throughput, not dollars.
2. **A scoring rule.** Without a Brier score and a horizon, "the crowd says 72% likely" is a
   sentence with a number in it and no meaning. This is the piece that decides whether any of it
   is real, and it is ~a day of work.
3. **Correlated agents.** Every persona shares one model. Their errors move together, so the
   crowd's confidence is not evidence. Diversity of *prompt* does not buy independence of *model*.
4. **A forecast horizon nobody has settled.** 3 months, 6 months, 1 year, 3 years: longer horizons
   do not make a better forecast, they make a slower feedback loop. The honest starting point is
   one horizon, scored, before any of them.

## 4. How close to MiroFish can this get — honestly?

MiroFish lets LLM agents post on a simulated feed and reads the resulting sentiment. Copying the
*mechanism* is straightforward; the parts people skip are the expensive ones.

**What we can build without any model at all, today:** social influence. `mmdesk/conversation.py`
runs agents that read their neighbours each round and update. Two rules are implemented and
measured:

| model | behaviour | measured |
|---|---|---|
| averaging (DeGroot) | every agent moves toward its neighbours' mean | **always converges**; never polarises, at any trust weight. A lone dissenter survives at low trust, disappears at high trust |
| bounded confidence | agents ignore opinions more than `tolerance` away | **camps form**: a 3-vs-3 crowd stays at ±1.0 indefinitely; set tolerance above the gap and it collapses to consensus in ~30 rounds |

That behaviour — a crowd that can either converge or split depending on how open it is — is the
interesting part of a simulated debate, and it costs nothing to run. It is also the part that
LLM posts add the least to.

**What an LLM crowd adds, and what it costs:** text you can read. A transcript where "Dana, 52,
Coral Gables" argues with "Marcus, 31, trader". That is engagement, not forecasting, unless a
scoring rule sits on top of it.

**The closest achievable design, in one line:** LLM-generated *biographies and arguments* (text,
cheap) driving a *bounded-confidence network* (arithmetic, free) whose output is *scored against
real forward returns* (the part everyone skips). Cost: ~$10–$200 for a serious run. Time: the
scoring rule is the critical path, not the agents.

## 5. What I would not do

- Launch with 1,000 agents because it sounds impressive. Cost is trivial; the credibility damage
  of an unscored crowd is not.
- Present a crowd distribution as a probability. It is a **sensitivity check**: what happens if
  you look at this through different mandates.
- Let the persona count grow while the fidelity eval stays at 11 questions.

## Reproduce

```
python scripts/persona_cost.py     # token counts and dollar tables
python -m pytest tests/test_conversation.py -q   # the convergence/polarisation behaviour
```
