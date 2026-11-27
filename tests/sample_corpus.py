"""A small synthetic corpus for the retrieval tests.

None of this is anyone's transcript. It is written here so the retrieval rules — the vocabulary
gate, the refusal reasons, the cache, verbatim quoting — are tested on every clone, without the
private material the calibration scripts use.
"""

from __future__ import annotations

VIDEOS = {
    "vid-rules": "Rules of the method",
    "vid-market": "Market posture",
    "vid-qna": "Viewer questions",
}

PASSAGES: list[dict] = [
    {"video_id": "vid-rules", "start_s": 104, "text":
        "We are going to talk about risk reward charts. That is the cheat code to what I do. "
        "Most people never make risk reward charts at all, and it is the first thing I look at."},
    {"video_id": "vid-rules", "start_s": 1737, "text":
        "I call it the three point rule. If a stock moves three points down from a ten to a nine "
        "to an eight to a seven, you should probably take some profits there. This is more art "
        "than science, honestly."},
    {"video_id": "vid-rules", "start_s": 1799, "text":
        "If your position has doubled you can sell half and put that money in your pocket. You "
        "have guaranteed yourself a win on the trade at that point."},
    {"video_id": "vid-rules", "start_s": 2509, "text":
        "When the indices become too expensive I get very picky about risk reward. I do not know "
        "what is going to cause the market to drop, but something is going to, because it is "
        "too expensive."},
    {"video_id": "vid-rules", "start_s": 3032, "text":
        "The better the setup scores, the more I am willing to put into it, but never more than "
        "about a tenth of the account in one name."},
    {"video_id": "vid-market", "start_s": 214, "text":
        "Over half of my money is in cash right now, earning interest in a money market fund. "
        "Cash is a position too, and it pays to wait."},
    {"video_id": "vid-market", "start_s": 2716, "text":
        "These lines are not technical lines. They are fundamental. If you are at the green "
        "line, that is a ten. The stock is cheap there."},
    {"video_id": "vid-market", "start_s": 2911, "text":
        "The gray lines are technical. The red and green lines are fundamental. I gave you this "
        "stock down here around the green line and it worked."},
    {"video_id": "vid-market", "start_s": 5032, "text":
        "The hedge is just insurance for the portfolio. It was my biggest position during the "
        "decline and my most profitable one, but you do not want to own it for long."},
    {"video_id": "vid-market", "start_s": 6150, "text":
        "I may enter, exit, or reverse a position at any time without notice. Nobody gets a "
        " heads up, not even the people closest to me."},
    {"video_id": "vid-qna", "start_s": 65, "text":
        "Okay, come on in. For those of you who are new here, I have been doing this a long "
        "time and I share my picks for free every week."},
    {"video_id": "vid-qna", "start_s": 420, "text":
        "The semiconductor names were weak all week and I think they should take half of their "
        "energy assets and sell them to fund the other half of the business."},
    {"video_id": "vid-qna", "start_s": 980, "text":
        "Someone asked about quantum computing. I do not have a view on that and I am not going "
        "to pretend that I do, so let us move on to the next ticker."},
    {"video_id": "vid-qna", "start_s": 1460, "text":
        "My dog is doing much better after the surgery, thank you for asking. It was a long week "
        "and I was in Miami visiting some friends of mine."},
    {"video_id": "vid-qna", "start_s": 2100, "text":
        "Yes."},     # a one-short-sentence passage: /api/lens used to crash on this
    {"video_id": "vid-qna", "start_s": 2400, "text":
        "A viewer wants to know about $AAPL. I would size it the same way as anything else, by "
        "the risk reward and the score."},
]

# Topics the sample never discusses, for the vocabulary gate.
UNSPOKEN = ("ichimoku", "zyzzyva", "kombucha")
