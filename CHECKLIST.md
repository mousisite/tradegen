# What is left to do, one step at a time

Seven things. Two of them matter enough to do before telling anyone the app
exists. The rest can happen after.

Do them in this order. Steps 1 and 3 each restart the server, so getting them
out of the way first means the later checks are testing the finished thing.

---

## 1. Turn on error alerts and unlock your own stats

**Time:** 3 minutes. **Why:** right now, if the app breaks at 3am, nobody
finds out until a stranger emails you. And `/usage`, the page that tells you
whether anyone came back a second day, refuses to show itself to anybody at
all on a deployment until you say who you are.

1. Go to **dashboard.render.com** and click your **stockbot** service.
2. In the left menu click **Environment**.
3. Click **Add Environment Variable**. Two boxes appear: *Key* and *Value*.
4. First one:
   - Key: `SENTRY_DSN`
   - Value: the address from your Sentry setup page. It starts with
     `https://` and has an `@` in the middle. Paste the whole thing on one
     line with no spaces before or after.
5. Click **Add Environment Variable** again for the second one:
   - Key: `STOCKBOT_ADMIN_EMAIL`
   - Value: your Google email address, the same one you sign into the app with.
6. Click **Save Changes**. Render restarts the app by itself. Wait for the
   status at the top to say **Live** again, about two minutes.

**How you know it worked:** sign in at https://tradegen.app and go to
https://tradegen.app/usage . Before this step it showed a refusal. Now it
shows numbers.

**If it still refuses:** the email has a typo, or it is a different Google
account than the one you signed in with. The app compares them exactly.

---

## 2. Check that a stranger cannot see your data

**Time:** 4 minutes. **Why:** this is the one that would genuinely embarrass
you in public. If accounts are not separated, the first person who signs up
sees your trades, and you would find out from them.

1. Open a **private / incognito window** (Ctrl+Shift+N in Chrome). This is
   important: it makes the browser forget you, so you are testing as a
   stranger would, not as yourself with a different hat on.
2. Go to https://tradegen.app and sign in with a **different Google account**.
   Any second account you have. If you do not have one, making a throwaway
   Gmail takes two minutes and is worth it.
3. Click through: **Trades**, **Portfolio**, **Monitor**, **Strategies**.

**What you should see:** every one of them empty. No trades. No watchlist. No
saved theses.

**What would be a disaster:** seeing your own trades. If that happens, stop,
do not tell anyone about the app, and say so. Do not post the link.

4. Close the private window. Your normal window is still signed in as you.

---

## 3. Check that data survives a deploy

**Time:** 5 minutes, mostly waiting. **Why:** your database lives on a disk
attached to the server. If that disk is not attached properly, every single
time you deploy an update, every user's data is erased and nobody tells you.
You would find out from angry users months in.

Now is the cheapest possible moment to test this, because the only data that
can be lost is yours.

1. Signed in as yourself, go to **Trades**.
2. Log a fake trade. Anything: symbol `AAPL`, any price, any size. Give it a
   note like "delete me" so you recognise it.
3. Confirm it shows in the list.
4. Go to **dashboard.render.com**, your **stockbot** service.
5. Top right, click **Manual Deploy** then **Deploy latest commit**.
6. Wait for **Live**. About two minutes.
7. Go back to https://tradegen.app/trades and refresh.

**What you should see:** the fake trade still there.

**What would be a disaster:** the list is empty. That means the disk is not
mounted and the database is being rebuilt from nothing on every deploy. Say
so, and do not launch until it is fixed.

8. Delete the fake trade.

---

## 4. Open it on your phone

**Time:** 2 minutes. **Why:** nearly everyone who taps a link from TikTok is
on a phone, holding it in one hand. Something that looks fine on a laptop can
be unusable there.

1. On your phone, open https://tradegen.app and sign in.
2. Check these specifically, because they are the ones that break on small
   screens:
   - Can you read the numbers in the tables without zooming?
   - Do the wide tables scroll sideways, or do they push the whole page
     sideways? Sideways-scrolling tables are fine. A sideways-scrolling
     *page* is broken.
   - Does the menu at the top work?
   - Run one analysis. Does the chart fit?
3. Also paste the link into a text message to yourself. You should see a
   picture card, not bare blue text.

If anything looks wrong, a screenshot says it faster than a description.

---

## 5. Tell Google the site exists

**Time:** 6 minutes. **Why:** Google has never heard of your site. Nobody has
ever linked to it. Without an introduction you are waiting for Google to trip
over you by accident, which can take months.

Be aware before you start: **even after doing this, searching "tradegen" will
not reliably find you for two to six weeks.** There is no trick that speeds
this up. This step is for month three, not for launch day.

1. Go to **search.google.com/search-console**.
2. Sign in with your Google account.
3. You get a box asking for a property. There are two kinds. Pick the **left**
   one, **Domain**.
4. Type `tradegen.app` — no `https://`, no `www`, just that.
5. It gives you a long line of text starting with `google-site-verification=`
   and asks you to add it as a TXT record at your DNS provider.
6. Open **dash.cloudflare.com** in another tab, click **tradegen.app**, then
   **DNS** in the left menu. This is the same page where you added the A
   record earlier.
7. Click **Add record**:
   - Type: **TXT**
   - Name: `@`
   - Content: paste the whole `google-site-verification=...` line
   - Click **Save**
8. Go back to Search Console and click **Verify**. If it says it cannot find
   it, wait five minutes and click Verify again. DNS changes are not instant.
9. Once verified, click **Sitemaps** in the left menu.
10. In the box, type `sitemap.xml` and click **Submit**.

**How you know it worked:** the sitemap row says **Success** and shows
**4 discovered pages**. Four is correct. That is every page a stranger can
read; the rest are behind the sign-in on purpose.

---

## 6. Remove the old address from Google sign-in

**Time:** 2 minutes. **Why:** the app has two addresses right now,
`tradegen.app` and the old `stockbot-je8m.onrender.com`. Sign-in is allowed
from both. Cleaning that up means one front door instead of two.

**Careful with this one.** Deleting the wrong line breaks sign-in for
everybody, including you.

1. Go to **console.cloud.google.com**, then **APIs & Services**, then
   **Credentials**.
2. Click your OAuth 2.0 Client ID.
3. Find the section **Authorised redirect URIs**.
4. **Before deleting anything**, confirm this exact line is in the list:
   `https://tradegen.app/auth/google/callback`
   If it is not there, stop. Deleting the other one would lock you out.
5. Delete only the line containing `onrender.com`. Click the bin icon beside it.
6. Click **Save**.
7. **Test immediately:** open a private window, go to https://tradegen.app,
   and sign in. If it works, this is done. If you get an error mentioning
   `redirect_uri_mismatch`, add the onrender line back.

---

## 7. Post it

**Time:** as long as you want to spend. **Why:** this is the actual
bottleneck. The app has been finished for a while. Nobody has seen it.

The draft post, where to put it, and what to say when people push back is
written out in [LAUNCH.md](LAUNCH.md). Read that file, not this one, for this
step.

Two things to hold onto:

**Post to one place first.** Read what comes back. Fix what they find. Then
the next place. Posting everywhere at once turns one round of useful feedback
into four copies of the same complaint.

**Do not refresh the thread.** Open `/usage` instead. The number that matters
is how many people came back on a **second day**. One person returning three
times is worth more than fifty who looked once.

---

## The two that are not optional

Steps 2 and 3. Everything else can wait until after you post. Those two
cannot, because both fail silently, and both fail in a way that is much worse
to discover in public than in private.

---

# Getting found: your first links

Google has never visited tradegen.app. Not ranked low, not there at all. The
reason is that no website anywhere links to it, so Google has no path to walk
down to reach you.

These four are how you build that path. Do them in this order. The first one
is the only one that is truly blocking.

---

## A. Search Console (6 minutes, do this first)

Written out in full as **Step 5** above. It is the letter that says "I exist."
Everything below works better once this is done, because Google will already
be watching for your address when the links appear.

---

## B. Make the GitHub repository public (2 minutes)

The easiest one, and it never expires.

**Before you do:** the code has been checked. No password, key or secret was
ever committed, `.env.example` is all blanks, and `.gitignore` keeps the
database, the session key and your settings out. It is safe to publish.

**The honest trade-off:** anyone can read the code and copy it. That is real.
It matters less than it sounds, because the code is not the hard part — the
measured history and the people using it are. And "here is the source" is the
single most credible thing you can say on Reddit or Hacker News.

1. Go to **github.com/mousisite/stockbot**
2. Click **Settings** (top right of the repo, not your profile settings)
3. Scroll to the very bottom, to the red **Danger Zone** box
4. Click **Change visibility** then **Change to public**
5. It makes you type the repository name to confirm. Type `mousisite/stockbot`
6. Confirm

Then, while you are there, rename it so it matches the product:

7. Still in **Settings**, at the top, the **Repository name** box
8. Change `stockbot` to `tradegen`, click **Rename**
9. GitHub forwards the old address automatically, so nothing breaks

**Last step, and do not skip it:** on the repo's front page, click the gear
icon beside **About** on the right, and put `https://tradegen.app` in the
**Website** box. That is the actual link.

---

## C. Hacker News, "Show HN" (10 minutes)

The highest ceiling of anything on this list. A post that lands brings a few
thousand technical readers in an afternoon, and they are exactly the people
who will find the flaws in the back-testing.

**Be warned:** most Show HN posts get no attention at all. That is normal and
not a verdict on the app.

1. Make an account at **news.ycombinator.com** if you do not have one
2. Click **submit** at the top
3. **Title** — must start with `Show HN:`. Keep it flat and factual. Hacker
   News actively dislikes excitement. Use:
   `Show HN: TradeGen – stock research that back-tests its own advice and reports no edge`
4. **URL** — `https://tradegen.app`
5. Leave the text box empty, then **immediately** add a comment on your own
   post. This is the convention: the comment is where you explain yourself.
   Use the draft in [LAUNCH.md](LAUNCH.md), shortened. Say plainly that you
   are one person, that 42 of 54 analyses came back Avoid, and that you want
   the methodology attacked.
6. **When to post:** a weekday, around 8–10am US Pacific. Never a weekend.

**Then:** stay at your computer for three hours and answer every single
comment. Honestly, including the harsh ones. Engaging well with criticism is
worth more than the post itself.

---

## D. Reddit (20 minutes, and read this warning first)

**The trap:** almost every finance subreddit auto-deletes posts from new
accounts, and you will not be told. Your post will simply not appear. If your
Reddit account is new or has no karma, spend a week commenting normally in
those subs before you post anything of your own.

Where to post, and what to watch for, is written out in [LAUNCH.md](LAUNCH.md).
Read that file, not this one, for the wording.

The short version:

1. Check your account age and karma. New account? Wait. Comment first.
2. Read the subreddit rules in the sidebar. Some ban self-promotion outright,
   some allow it only on certain days.
3. Post to **one** subreddit. Start with **r/SecurityAnalysis** — small, but
   it is the audience that actually values filings over hype.
4. Answer every comment for the rest of the day.
5. Wait a few days. Fix what they found. Then the next subreddit.

**Do not post to r/wallstreetbets.** That audience wants confirmation, and the
whole character of this app is refusing to give it.

---

## E. Product Hunt (30 minutes, and not yet)

Worth doing, but **you only get one launch day** and it is wasted on a product
nobody has used yet. Do this after Reddit and Hacker News have given you a
round of feedback and you have fixed what they found.

When you are ready:

1. Make an account at **producthunt.com** and use it normally for a week
   first. Brand new accounts launching products get ignored.
2. Click **Submit** and fill in: name, a one-line tagline, description,
   the link, and at least one image. The preview card at
   `static/preview.png` works as a starting point.
3. Schedule it for a **Tuesday or Wednesday**, 12:01am US Pacific. That gives
   the post a full day on the leaderboard.
4. Be present all day answering comments.

---

## What to honestly expect

None of these four make you rank for the word "tradegen." There is an older
crypto project with that name and seven years of history behind it.

What they do is get Google to **discover** you — to walk down a path to your
address for the first time — and put the app in front of real people. The
second part is worth more than the first.

The order that matters: **A** today, **B** today, **C** when you have a free
weekday morning, **D** once your Reddit account is old enough, **E** after
the feedback from C and D is folded in.
