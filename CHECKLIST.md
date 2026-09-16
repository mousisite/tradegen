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
