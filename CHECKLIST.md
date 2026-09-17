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
10. **Before typing anything, look at the dropdown at the top left.** It names
    the site you are currently looking at. It has to say `tradegen.app`. If it
    names a different site, click it and switch. Search Console refuses a
    sitemap that does not belong to the site you have open, and the message it
    gives, *Invalid sitemap address*, does not say that is the reason.
11. In the box, type the **whole address**:

    ```
    https://tradegen.app/sitemap.xml
    ```

    A Domain property needs the full address. Typing only `sitemap.xml` works
    for the other kind of property, the URL-prefix kind, and is rejected here.

12. Click **Submit**.

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

## B. Put your code on the internet (2 minutes)

**What GitHub is:** a website where people keep their code. Yours is already
there, but it is set to private, so nobody can see it. Making it public means
anyone can read the code.

**Why bother:** two reasons. It gives Google a real link pointing at your app,
which is the thing you do not have. And when you post on Reddit or Hacker
News, being able to say "here is the code, go check it" is the most trusted
thing you can say. People assume trading apps are lying. Showing the code is
how you prove you are not.

**The risk, said plainly:** anyone can copy your code. That is true. It
matters less than it sounds. The code is not the valuable part. The valuable
part is the record of what the app measured over time, and the people who use
it. Neither of those can be copied.

**It is safe.** I checked every version of the code you have ever saved, not
just the current one. No password, no key, no secret is in there anywhere.

### Do this

1. Open **github.com/mousisite/stockbot**
2. Near the top of the page there is a row of words: Code, Issues, Pull
   requests, and so on. At the end of that row, click **Settings**.
   Careful: this is the Settings for the code, not the Settings for your
   account. The account one is under your picture in the corner. You want the
   one in the row.
3. Scroll all the way down. At the bottom there is a box with a red border
   called **Danger Zone**.
4. In that box, find **Change repository visibility** and click
   **Change visibility**.
5. Choose **Make public**.
6. GitHub makes you prove you mean it. It shows a box and asks you to type the
   name. Type: `mousisite/stockbot`
7. Click the confirm button.

Done. Your code is now public.

### Two small things while you are there

**Rename it.** The code is called "stockbot" but the app is called TradeGen.

1. Still on that same Settings page, scroll back to the top
2. The first box says **Repository name**
3. Change `stockbot` to `tradegen`
4. Click **Rename**

Nothing breaks. GitHub forwards the old name to the new one automatically.

**Add the link. Do not skip this one, it is the whole point.**

1. Click **Code** to go back to the front page of your repository
2. On the right side there is a section called **About**. Next to it is a small
   gear icon. Click it.
3. A box opens. Find the field called **Website**.
4. Type: `https://tradegen.app`
5. Click **Save changes**

That link is the reason you did all of this.

---

## C. Hacker News (10 minutes)

**What it is:** a website where programmers, engineers and people who build
startups read and discuss things. It is plain and ugly and extremely widely
read. If your post reaches the front page, a few thousand people visit your
app that afternoon.

**Why it is worth the most:** these are the people who will actually try to
break your back-testing and tell you where it is wrong. That is worth more
than traffic.

**"Show HN" is a label.** It means "I made this thing myself." Posts with that
label are allowed to be about your own project. Without it, posting your own
work looks like advertising.

### Do this

1. Go to **news.ycombinator.com** and make an account if you do not have one
2. At the top of the page, click **submit**
3. In the **Title** box, paste exactly this:

   ```
   Show HN: TradeGen – stock research that back-tests its own advice and reports no edge
   ```

   Do not make it more exciting than that. This website dislikes excitement
   and will punish it. Flat and factual wins here.

4. In the **URL** box, type: `https://tradegen.app`
5. Leave the **text** box empty
6. Click **submit**
7. Your post appears. Click on it, and write the first comment yourself. This
   is how it is done here: the post is the link, your comment is where you
   explain. Say these things:
   - You built it yourself, one person
   - Out of 54 analyses, 42 came back saying Avoid
   - You want them to tell you where the method is wrong

   Longer wording is in [LAUNCH.md](LAUNCH.md).

**When to post:** a weekday, in the morning, US west coast time. Never a
weekend, the site is quiet.

**Then stay at your computer for three hours.** Answer every comment,
including the rude ones, calmly and honestly. How you handle criticism is
what people are actually judging.

**What to expect:** most posts get no attention at all. That is normal. It is
not a verdict on your app.

---

## D. Reddit (20 minutes, but read the warning first)

**The warning, and it is a real trap:** almost every money and investing
subreddit automatically deletes posts from new accounts. It does not tell you.
Your post looks fine on your screen and is invisible to everybody else. People
sit refreshing a post nobody can see.

**So check first.** If your Reddit account is new, or has no karma (karma is
the points you get from people upvoting you), do not post yet. Spend a week
just commenting normally in those subreddits. Then post.

### Do this

1. Check your account. Old with some karma? Continue. New? Wait a week first.
2. Pick **one** subreddit. Start with **r/SecurityAnalysis**. It is small, but
   the people there care about company filings rather than hype, which is
   exactly what your app is.
3. Read the rules in the sidebar before posting. Some subreddits ban posting
   your own projects completely. Some allow it only on certain days.
4. Post it. The wording is written out in [LAUNCH.md](LAUNCH.md).
5. Answer every comment that day.
6. Wait a few days. Fix whatever they found. Then try the next subreddit.

**Do not post to r/wallstreetbets.** That crowd wants to be told they are
right. Your app's whole personality is refusing to do that. You would get
voted down for the one thing that makes it good.

---

## E. Product Hunt (30 minutes, but not yet)

**What it is:** a website where new apps are shown for one day and people vote
for their favourites. Doing well there sends a lot of visitors.

**Why not yet:** you get one launch day, ever. Spending it before anyone has
used the app wastes it. Do Hacker News and Reddit first, fix what they find,
then come here.

### When you are ready

1. Make an account at **producthunt.com** and use it normally for a week.
   Brand new accounts that immediately launch something get ignored.
2. Click **Submit** and fill in:
   - Name: TradeGen
   - Tagline: one short line
   - Description: a paragraph
   - Link: `https://tradegen.app`
   - At least one picture. The file at `static/preview.png` works to start.
3. Schedule it for a **Tuesday or Wednesday at 12:01am US west coast time**.
   That gives you the whole day on the board instead of half of it.
4. Be at your computer that day answering comments.

---

## What to honestly expect from all four

None of these will make you come up first when someone searches the word
"tradegen." There is an older crypto project with that name and seven years of
history behind it. That fight is not worth having.

What these do is two things:

**They let Google find you.** Right now Google has no path to walk down to
reach your app. These build the first path.

**They put the app in front of real people.** This part is worth more than the
Google part.

### The order

| When | What |
|---|---|
| Today | **A** (Search Console) and **B** (GitHub). 8 minutes total. |
| A free weekday morning | **C** (Hacker News) |
| Once your Reddit account is old enough | **D** (Reddit) |
| After C and D give you feedback and you fix it | **E** (Product Hunt) |
