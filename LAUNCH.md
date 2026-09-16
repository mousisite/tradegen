# The launch post

A draft, not a script. Post it in your own words — the value of a "I built this"
post is that a person wrote it, and a paragraph that reads like marketing copy
gets downvoted on exactly the subreddits worth reaching.

Every number below was checked against the live app before being written down.
If you change the text, keep that property.

---

## Where to post

| Where | Why | Watch out for |
|---|---|---|
| **r/SecurityAnalysis** | Small, serious, values filings over hype. The best fit for what this actually is | Low traffic. A good post gets tens of readers, not thousands |
| **r/investing** | Much larger, tolerant of tools when they are free and not spammy | Read their self-promotion rules first. Some weeks it is banned outright |
| **r/algotrading** | Will engage seriously with the back-testing and the cost modelling | They will try to break your methodology. That is the point |
| **Value investing Discords** | Highest-quality feedback per person | You have to be a participant first, not arrive selling |

**Not r/wallstreetbets.** That audience wants confirmation, and the whole
character of this app is refusing to give it. You would be downvoted for the
one thing that makes it worth using.

Post to **one** first. Read what comes back. Fix what they find. Then the next.
Posting everywhere at once turns one round of feedback into four rounds of the
same complaint.

---

## Draft

**Title:** I built a stock research tool that back-tests its own advice and
tells you when it has no edge

---

I got tired of trading tools that always have an opinion. So I built one that
counts how often its own setups actually worked, charges realistic costs, and
says "no demonstrated edge" when there isn't one.

It says that a lot. Across the analyses I have run so far, **42 of 54 came back
as Avoid**. That is an awkward thing for a trading tool to admit and it is what
the numbers say.

Three things it does that I had not found elsewhere:

**It separates being right from being lucky.** You can save a thesis, and later
close it out as right, wrong, right-by-luck, or undecided. The app proposes the
answer the price supports rather than the one you remember. The luck column is
the whole point — a two-way right/wrong tally quietly rewards being lucky, and
you end up believing your reasoning works when only your luck did.

**It prices gap risk in cash.** A stop tells you what you intend to lose. It
also shows what a move the size of that instrument's own worst day would cost,
because a stop does not survive a gap. On SPY a 1% risk is really about 1.2x
that. On Bitcoin it is **3.2x**.

**Every figure cites the filing.** Fundamentals come from SEC XBRL data with a
link to the document, not from a vendor's summary. It runs Piotroski, Altman,
Beneish and the accruals ratio over the filed numbers — and refuses where those
models do not apply. Ask it about a bank and it will not give you an Altman
score, because those coefficients were fitted on manufacturers and a bank's
balance sheet reads as distressed however healthy it is.

Free, no ads, no tracking scripts, nothing sold. Sign in with Google so your
journal is yours. You can export everything or delete the account from inside
the app.

**What I would like:** tell me where the methodology is wrong. The back-testing
charges costs and uses walk-forward samples with confidence intervals, but I am
one person and I would rather find the flaw now.

https://tradegen.app

*Not financial advice. By its own measurements most short-horizon trading loses
money after costs, which it will tell you itself.*

---

## If someone asks

**"How is this different from Koyfin / Simply Wall St / GuruFocus?"**

Be honest: on fundamentals display, it is not, and they are better resourced.
The difference is the honesty machinery — the back-test that reports no edge,
the luck column, gap risk. Say that plainly. Claiming to beat them on features
invites a comparison you lose.

**"What data do you use?"**

Yahoo Finance for prices, SEC EDGAR for filings. Say so. Do not imply licensed
institutional data you do not have.

**"Is it profitable / what returns?"**

You do not know, and neither does it. It measures setups on history; it has no
live track record. Say exactly that. This is the question where a small
exaggeration destroys the only thing that makes the product interesting.

**"Why free? What's the catch?"**

No catch yet. You are trying to find out whether it is useful before deciding
whether it is a business. That is true, and it is a good answer.

---

## After posting

Do not refresh the thread. Open `/usage` instead.

The number that matters is **how many people came back on a second day**. Not
sign-ups, not page views. One person returning three times is worth more than
fifty who looked once.

Also watch the interval split. If most analyses are on 5-minute bars, people
came for day-trading signals and will leave when told to avoid. If they are on
daily bars or longer, they came for research and the positioning is right.

Give it a week before concluding anything.
