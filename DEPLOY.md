# Putting it online

Everything here has been tested except the parts that need credentials only you
can create. Work top to bottom; the whole thing is about an hour, most of it
waiting for Google.

---

## What you need to bring

| | What | Where |
|---|---|---|
| 1 | A hosting account | [render.com](https://render.com) or [fly.io](https://fly.io). Both terminate HTTPS for you, which Google requires. **Not free — see below** |
| 2 | A Google Cloud project | [console.cloud.google.com](https://console.cloud.google.com) |
| 3 | An email address for support | Google's consent screen asks for one and shows it to users |
| 4 | An Anthropic API key *(optional)* | [console.anthropic.com](https://console.anthropic.com). Unlocks screenshot reading, better sentiment, and written reasoning |

Nothing else. No database to provision, no Redis, no build step.

### What it costs

Be clear-eyed about this before Monday rather than after the first invoice.

`render.yaml` asks for the **`starter`** instance type and a **1 GB disk**.
Neither is on Render's free tier, and that is deliberate, for two reasons:

- **A free instance has no persistent disk at all.** Without one the database
  lives inside the container and every redeploy deletes the journal, the saved
  theses and the accumulated strategy record. That is not a cheaper version of
  this app; it is a broken one.
- **A free instance sleeps when idle.** A sleeping process checks no alerts, so
  the Monitor page would quietly stop doing the one thing it is for.

Fly.io is the same shape: a volume and a machine that does not auto-stop.
`fly.toml` already sets `auto_stop_machines = false` for exactly that reason.

**Check the current price yourself on their pricing page.** I cannot look it up,
and hosting prices change; I am not going to quote you a number I cannot
verify. Expect a small monthly figure for the instance plus a little for the
disk, on either host.

If you want to spend nothing at all on Monday, the honest option is to run it
on your own machine exactly as you do now and share nothing. There is no free
configuration of this that also keeps your data and checks your alerts.

---

## 1. Get the code onto the host

The repository is already initialised, committed, and on a `main` branch.
Create an empty repository on GitHub and push to it:

```
git remote add origin <your empty GitHub repo>
git push -u origin main
```

`.gitignore` excludes `.env`, `config.json`, `data/`, `backups/` and the
bundled runtime, so no credential and no journal leaves your machine. That was
checked against the staged files before the first commit, not merely assumed:
nothing matching an API key, a client secret or a private key is in the
history.

If you would rather the repository were private, make it private on GitHub
before pushing. Both hosts can deploy from a private repository.

---

## 2. Deploy it once, before touching Google

Do this first and on purpose. Google needs a working HTTPS address before you
can finish registering the OAuth client, and the app runs perfectly well with
no sign-in at all, so you can confirm the deployment is healthy before adding
anything that can fail.

### Render

New → Web Service → connect the repo. Render reads `render.yaml` and needs no
further configuration. The one thing to check is that the **disk is mounted at
`/data`**: without it, the database is inside the container and every redeploy
wipes the journal and signs everyone out.

### Fly

```
fly launch --no-deploy      # answer no to a database, it does not need one
fly volumes create stockbot_data --size 1
fly deploy
```

### Confirm it is alive

```
curl https://<your-address>/healthz
```

Expect `{"ok": true, "service": "stockbot"}`. That endpoint touches the
database, so a 200 means the process is genuinely able to serve, not merely
running. Open the address in a browser: you should get the full app with no
sign-in, as a single local account.

**Set this now**, replacing the address with your real one:

```
STOCKBOT_PUBLIC_URL = https://<your-address>
STOCKBOT_CONTACT    = <your support email>
STOCKBOT_SECRET_KEY = <48 random characters>
```

Generate the key with:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Setting it explicitly matters once there is more than one copy of the app
running, because otherwise each generates its own and they reject each other's
sessions.

---

## 3. Register the OAuth client

In the Google Cloud console, on your project:

**APIs & Services → OAuth consent screen**

- User type: **External**
- App name, support email, developer contact: fill in
- **App domain**: your address
- **Privacy policy**: `https://<your-address>/privacy`
- **Terms of service**: `https://<your-address>/terms`
- Scopes: add **`openid`**, **`email`**, **`profile`**, and nothing else

Both of those pages are already live and reachable without signing in, which is
what Google checks. Asking for only those three scopes is also what keeps the
app out of Google's verification queue: request anything more and an external
app needs review, which takes weeks rather than a weekend.

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- Type: **Web application**
- Authorised redirect URI: `https://<your-address>/auth/google/callback`

Type that last one exactly, including `https` and with no trailing slash. A
mismatch here is the single most common reason sign-in fails, and the app
translates Google's error into a message naming the address it actually used,
so if it goes wrong the page will tell you what to paste.

While the consent screen is in **Testing**, only accounts you add under *Test
users* can sign in. That is the right state for Monday. Move it to
*In production* when you want it open to anyone.

---

## 4. Turn sign-in on

Add to the host's environment:

```
GOOGLE_CLIENT_ID     = <from Google>
GOOGLE_CLIENT_SECRET = <from Google>
```

Sign-in switches itself on the moment both are present; there is no separate
setting. Redeploy, open the address, and you should be sent to a sign-in page.

To keep it private to you and a few testers for launch:

```
GOOGLE_ALLOWED_DOMAINS = yourdomain.com
```

Leave it empty to let anyone with a Google account in.

---

## 5. Optional: the AI features

```
ANTHROPIC_API_KEY = <from console.anthropic.com>
```

Without it the app works. On a public deployment the features it would switch
on are simply not offered: no screenshot box, no written reasoning section, and
headlines scored by the built-in word list, with no note to visitors about a
missing key. On your own machine the pages tell you, the owner, what the key
would add. With it you additionally get screenshot reading, LLM sentiment
scoring, and the written reasoning on the research page.

---

## 6. Recommended: copies of the backups somewhere else

The automatic backups sit on the same 1 GB disk as the database, so losing that
disk loses both. With four settings, the server also copies each backup to a
Cloudflare R2 bucket as soon as it is taken, and checks that the copy there is
whole. It keeps the newest three copies, the newest of each of the last 30 days
and the oldest of each of the last 12 months, so 45 at most, and deletes the
older ones it made. Nothing else in the bucket is touched.

**What it costs.** R2's free allowance includes 10 GB of storage a month. 45
copies of a 20 MB database come to 0.9 GB, so a small database stays inside it;
`/usage` shows how big yours is. Past the allowance R2 charges by the gigabyte.
Cloudflare may ask for a payment method before it turns R2 on, even if you
never go past the allowance. Prices change, so check Cloudflare's R2 pricing
page yourself.

**The copies hold personal data.** Each one is the whole database: every
account's email address, journal and notes. The bucket must stay private, which
is how R2 makes it. Do not turn on its public `r2.dev` address or connect a
domain to it, and give the key access to this one bucket only. Once copies are
on, the privacy page names Cloudflare and says how long copies are kept.

1. Sign in at dash.cloudflare.com and open **R2 Object Storage**. Press
   **Create bucket**, name it `orenth-backups`, leave everything else as it is,
   and create it.
2. Go back to the R2 page and open **Manage API tokens**. Press **Create API
   token**. Give it a name, choose the permission **Object Read & Write**, and
   under the buckets choose **Apply to specific buckets only** and pick
   `orenth-backups`. Create it.
3. The next page shows an **Access Key ID**, a **Secret Access Key** and an
   endpoint like `https://<account id>.r2.cloudflarestorage.com` (the one
   marked Default, if two are shown). Copy all three now: the secret is shown
   only once.
4. In Render, open the service, then **Environment**, and add:

   ```
   BACKUP_S3_ENDPOINT          = https://<account id>.r2.cloudflarestorage.com
   BACKUP_S3_BUCKET            = orenth-backups
   BACKUP_S3_ACCESS_KEY_ID     = <the Access Key ID>
   BACKUP_S3_SECRET_ACCESS_KEY = <the Secret Access Key>
   ```

   Save. Render restarts the app, and its log shows
   `off-site  each backup copied to orenth-backups at ...`.
5. The next day, open `/usage`. Under **Copies kept elsewhere** it shows when
   the last copy was made, its size and **Checked**. If it says **Failed**, the
   reason is written next to it.

To copy one now instead of waiting, use the service's **Shell** in Render:

```
python backup.py --offsite /data/backups/<the newest stockbot-....db>
python backup.py --offsite-list
```

To bring a copy back for a restore, fetch it by a name from that list. It is
downloaded, checked against the backup that was uploaded, and opened to check
the database inside is intact:

```
python backup.py --fetch <its name> --out /data/restore
```

Then put it in place of the live database, and restart the service in Render
straight after, so the app brings the restored database up to date and forgets
what it read before:

```
python backup.py --restore /data/restore/<its name>
```

`--restore` checks the copy, backs up the database as it is now into
`/data/backups` (it prints that file's name, so a restore from the wrong copy
can be undone the same way), and then writes the copy into the live database
through SQLite, as one change. It works while the app is running, and if
something holds the database's write lock for more than ten seconds it changes
nothing and says so. Everything written since the copy was taken is lost: new
accounts, journal entries, and plan changes from Stripe's webhooks, which
Stripe does not send again. Accounts deleted since come back, and have to be
deleted again. The same command restores a local backup, such as one in
`/data/backups` or `/data/backups/before-release`.

Never restore by copying a backup over `stockbot.db`. A server that was
stopped or killed leaves `stockbot.db-wal` beside it with its last writes, and
SQLite lays those over the copy the next time the app opens it: the database
opens cleanly, but it is not the backup.

The app only ever deletes copies named the way its backups are named. So to
keep one for good, copy a backup by hand under another name, such as
`keep-2026-11.db`, with `--offsite`; it stays until you delete it in
Cloudflare. If you later remove these settings, the copies already in the
bucket stay too, and nothing prunes them any more: delete them in Cloudflare,
since each holds every account, deleted ones included. Two more settings are optional: `BACKUP_S3_REGION`
(default `auto`, which R2 uses) and `BACKUP_S3_PREFIX` (default `orenth/`, the
folder in the bucket). Any storage that speaks the S3 protocol works, not only
R2.

---

## 6. Optional: Sign in with Apple

The iPhone app needs this before it goes to the App Store. Apple asks for
"Sign in with Apple" wherever Google sign-in is offered, and asks that
deleting an account also ends it. It also adds a "Continue with Apple" button
to the website. You need a paid Apple Developer account. Nothing changes on
the site until the settings below are in.

On developer.apple.com, go to Account, then Certificates, Identifiers &
Profiles:

1. **Make the app's ID.** Identifiers, then **+**, then App IDs, then App.
   Bundle ID: Explicit, `app.orenth.ios`. Tick **Sign In with Apple** (leave
   it as a primary App ID) and **Push Notifications**. Continue, then
   Register.
2. **Tell Apple where to send changes.** Open that App ID, click
   **Configure** next to Sign In with Apple, and type this as the
   server-to-server notification endpoint:
   `https://orenth.app/auth/apple/notifications`. Done, then Save.
3. **Make the website's ID.** Identifiers, then **+**, then **Services IDs**.
   Description "Orenth website", identifier `app.orenth.web` (any name you
   like, as long as it is not the app's). Register. Open it, tick **Sign In
   with Apple**, click **Configure**, and fill in: Primary App ID
   `app.orenth.ios`; Domains and Subdomains `orenth.app`; Return URLs
   `https://orenth.app/auth/apple/callback`. Done, Continue, then Save. There
   is no file to put on the site: Apple stopped asking for one.
4. **Make one key for both jobs.** Keys, then **+**. Name it "Orenth". Tick
   **Sign in with Apple** (click Configure, choose `app.orenth.ios`, Save)
   and **Apple Push Notifications service (APNs)**. Continue, Register, then
   **Download**. Apple lets you download the .p8 file only once, so keep it
   somewhere safe. Write down the **Key ID** shown on that page.
5. **Find your Team ID.** It is at the top right of the developer site, and
   under Membership details: ten letters and numbers.
6. **Let email reach people who hide their address.** Services, then **Sign
   in with Apple for Email Communication**, then Configure. Add the domain of
   `MAIL_FROM` (or that exact address) under Email Sources. Apple checks
   that mail with SPF, so the domain's SPF record must be in place; Resend's
   domain setup adds it. Without this, Apple drops sign-in codes, alerts and
   the weekly email sent to anyone who chose to hide their address.

Then in Render, open the service, then Environment, and add:

```
APPLE_TEAM_ID            = <the Team ID, step 5>
APPLE_SIGNIN_KEY_ID      = <the Key ID, step 4>
APPLE_SIGNIN_PRIVATE_KEY = <the whole .p8 file, BEGIN and END lines included>
APPLE_SERVICES_ID        = app.orenth.web
```

To copy the .p8 file, open it in Notepad, select all, copy, and paste it as
the value. Pasting it on one line with `\n` where the line breaks were also
works. `APPLE_BUNDLE_ID` is `app.orenth.ios` unless you set it.

Save, and Render redeploys. The sign-in page then shows "Continue with
Apple", and the app can use Apple's own sign-in sheet. With only the first
three set, Apple sign-in works in the app only, and the website shows no
Apple button. The start-up lines in Render's log say "Apple" among the ways
in, or name the Apple setting that is missing.

The same key also signs push notifications to the iPhone app. Give it to the
push settings as well: `APNS_KEY_ID` is the same Key ID, and
`APNS_PRIVATE_KEY` is the same .p8 text.

---

## 7. Optional: selling Pro in the iPhone app

Inside the iPhone app, Apple allows Pro to be sold only through its own
in-app purchase. Pro bought in the app works on the website too, and Pro
bought on the website works in the app. Nothing changes on the site until
`APPLE_APP_ID` is set. `mobile/APP_STORE.md` (Part 5, "Sell Pro in the app")
has the App Store Connect half: the agreements, the two subscriptions and
the review. This is the server's half. Pro needs the Stripe settings too,
because they are what switch Free and Pro on.

1. In App Store Connect, open Orenth, then **App Information** (under
   General). Write down the number shown as **Apple ID**: about ten digits.
2. On the same page, under **App Store Server Notifications**, set both the
   **Production Server URL** and the **Sandbox Server URL** to
   `https://orenth.app/apple/notifications`, and choose **Version 2** for
   each. Save.
3. In Render, open the service, then **Environment**, and add:

   ```
   APPLE_APP_ID = <the Apple ID number from step 1>
   ```

   Save. Render redeploys, and the start-up lines in its log say
   `app purchases  on for app.orenth.ios.pro.monthly, app.orenth.ios.pro.yearly`.
4. Do section 8 below, so Cloudflare never stops Apple's server.

These are optional, and right as they are unless you change the products:

```
APPLE_PRODUCT_IDS   = app.orenth.ios.pro.monthly,app.orenth.ios.pro.yearly
APPLE_BUNDLE_ID     = app.orenth.ios
APPLE_SANDBOX_PRO   = all
APPLE_SANDBOX_USERS =
```

**Test purchases.** Builds run from Xcode, TestFlight builds and App Review
all buy in Apple's sandbox, where nothing is charged. App Review uses the
real build with a sandbox account, and rejects an app whose purchase gives
nothing, so by default (`APPLE_SANDBOX_PRO=all`) a test purchase gives Pro,
and the pages call it a test purchase. Because of that, give TestFlight
builds only to your own internal testers: anyone with one could give
themselves Pro for free. `allowlist` gives test purchases Pro only to the
accounts whose email addresses are in `APPLE_SANDBOX_USERS`, and `none` to
nobody. Never use either while a build is in review, unless the review
account is on the list. A change takes effect when Render restarts.

**Never remove `APPLE_APP_ID` while anyone has Pro through Apple.** Apple
keeps billing them, but without it the server stops recording their
renewals, and their Pro would end with the period they had already paid.

**Before each App Review submission**, put the review account back on Free,
in Render's **Shell** tab:

```
python tools/reset_test_purchases.py
```

**How it works.** Apple's server tells this one about every purchase,
renewal, refund and cancellation at `/apple/notifications`. Each one is
signed by Apple and checked against Apple's root certificate, which is built
into the code. A notification is answered with 200 only once it is stored;
anything else and Apple sends it again, for about a week. If one keeps being
refused, the log (and Sentry) says "App Store notifications keep failing
their check", which nearly always means `APPLE_APP_ID` or `APPLE_BUNDLE_ID`
is wrong. The App Store Server API, and an In-App Purchase key for it, are
not used.

---

## 8. Cloudflare: let Apple's and Stripe's servers through

Cloudflare sits in front of orenth.app. Its bot protection can answer a
request with a "checking your browser" page, which another company's server
can never pass. Every message Apple or Stripe sends is then quietly lost:
renewals stop being recorded, refunds stop taking Pro away, and an Apple
Account deleted on Apple's side is never heard of. Three addresses must skip
those checks. It is safe: each of them checks the sender's signature itself
and refuses anything else.

- `/apple/notifications` (purchases in the iPhone app)
- `/auth/apple/notifications` (Sign in with Apple)
- `/billing/webhook` (Stripe)

1. Go to https://dash.cloudflare.com, click **orenth.app**, then
   **Security**, then **WAF**, then the **Custom rules** tab. Click
   **Create rule**.
2. Rule name: `Let Apple and Stripe servers through`.
3. Click **Edit expression**, and paste this:

   ```
   (http.request.method eq "POST" and http.request.uri.path in {"/apple/notifications" "/auth/apple/notifications" "/billing/webhook"})
   ```

4. Under **Then take action**, choose **Skip**. Tick every box it offers:
   **All remaining custom rules**, **All rate limiting rules**, **All managed
   rules**, and **All Super Bot Fight Mode Rules**. Under **More components
   to skip**, tick **Browser Integrity Check**, **Security Level** and
   **User Agent Blocking**.
5. Under **Place at**, choose **First**. Click **Deploy**.
6. Click **Security**, then **Bots**. If **Bot Fight Mode** is on (the free
   plan's version), turn it off: a rule cannot make it skip these
   addresses. **Super Bot Fight Mode** (paid plans) is skipped by the rule.
7. To check it, make a test purchase in the app (`mobile/APP_STORE.md`,
   Part 5), then open **Security**, then **Events**, and filter by the path
   `/apple/notifications`. There should be no **Managed Challenge**,
   **Challenge** or **Block** there.

---

## The whole environment, in one place

| Variable | Needed | What it does |
|---|---|---|
| `STOCKBOT_PUBLIC_URL` | yes | The address browsers use. Builds the OAuth callback and enables the secure cookie flag |
| `STOCKBOT_SECRET_KEY` | yes | Signs session cookies. Generated into `data/secret.key` if unset |
| `STOCKBOT_DB` | yes | `/data/stockbot.db`, on the mounted disk |
| `STOCKBOT_CONFIG` | yes | `/data/config.json`, so settings survive a redeploy |
| `STOCKBOT_BEHIND_PROXY` | yes | Trust the proxy's forwarded scheme and host. Already set in both host configs |
| `STOCKBOT_CONTACT` | yes | Shown on the privacy and terms pages |
| `GOOGLE_CLIENT_ID` | for sign-in | Turns sign-in on, together with the secret |
| `GOOGLE_CLIENT_SECRET` | for sign-in | Never appears in a URL or a config file |
| `GOOGLE_ALLOWED_DOMAINS` | no | Restrict who may sign in |
| `GOOGLE_REDIRECT_URI` | no | Only if the callback differs from `PUBLIC_URL` + path |
| `APPLE_TEAM_ID`, `APPLE_SIGNIN_KEY_ID`, `APPLE_SIGNIN_PRIVATE_KEY` | for Apple sign-in | All three turn on Sign in with Apple in the iPhone app. See section 6 |
| `APPLE_SERVICES_ID` | for Apple on the website | With the three above, adds "Continue with Apple" to the website |
| `APPLE_BUNDLE_ID` | no | The iPhone app's bundle id, default `app.orenth.ios`. Sign in with Apple and in-app purchase both use it |
| `APPLE_APP_ID` | for selling Pro in the app | The app's number (Apple ID) from App Store Connect. Switches on in-app purchase, and Apple's production notifications must name it. See section 7 |
| `APPLE_PRODUCT_IDS` | no | The subscriptions that are Pro, default `app.orenth.ios.pro.monthly,app.orenth.ios.pro.yearly` |
| `APPLE_SANDBOX_PRO`, `APPLE_SANDBOX_USERS` | no | Whether test purchases give Pro: `all` (the default, which App Review needs), `allowlist` (only the email addresses in `APPLE_SANDBOX_USERS`) or `none` |
| `ANTHROPIC_API_KEY` | no | Screenshot reading, LLM sentiment, written reasoning |
| `VAPID_PRIVATE_KEY` | recommended | The key browser notifications are signed with. Made and kept in `/data/vapid.pem` if unset; copy that file's text here so it outlives the disk. See "Notifications" below |
| `VAPID_PUBLIC_KEY` | no | Only checked against the private key; the server works it out itself |
| `APPLE_TEAM_ID`, `APNS_KEY_ID`, `APNS_PRIVATE_KEY` | for iPhone app notifications | All three turn on notifications to the Orenth iPhone app, through Apple. The key is the `.p8` file's text |
| `APNS_BUNDLE_ID` | no | The iPhone app's bundle id, default `app.orenth.ios` |
| `SENTRY_DSN` | recommended | Error reporting. Without it a 3am failure is invisible until someone emails you |
| `STOCKBOT_ADMIN_EMAIL` | recommended | Who may see `/usage`. Without it, nobody can on a deployment |
| `STOCKBOT_BACKUP_HOURS` | no | Hours between automatic backups, default 12. Set 0 to switch off |
| `STOCKBOT_BACKUP_DIR` | no | Where they go, default `<database folder>/backups` |
| `STOCKBOT_BACKUP_KEEP` | no | How many to keep, default 8 |
| `BACKUP_S3_ENDPOINT`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` | recommended | All four copy each backup to a private storage bucket off the server, such as Cloudflare R2. See section 6 |
| `BACKUP_S3_REGION`, `BACKUP_S3_PREFIX` | no | The bucket's region, default `auto` (R2's), and the folder in it, default `orenth/` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | for email sign-in | All five turn on "Continue with email". Port defaults to 587; 465 uses SSL |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SID` | for phone sign-in | All three turn on "Continue with phone", through Twilio Verify |
| `PHONE_ALLOWED_PREFIXES` | no | Country codes allowed to get texts, e.g. `+1,+44`. Limits SMS fraud |
| `PHONE_CODES_PER_HOUR` | no | Texts the whole site may send an hour, default 40 |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET` | for paid plans | All three turn on Free and Pro. Webhook address: `<PUBLIC_URL>/billing/webhook` with `checkout.session.completed` and `customer.subscription.created/updated/deleted` |
| `STRIPE_PRICE_ID_YEARLY` | no | A yearly price, offered beside the monthly one with what it saves |
| `PRO_PRICE_LABEL` | no | Shown only if Stripe cannot be reached; prices are otherwise read from Stripe |
| `FREE_ANALYSES_PER_DAY`, `FREE_FIND_RESULTS`, `FREE_ALERTS` | no | The free allowance, default 3, 3 and 1 |
| `MAIL_POSTAL_ADDRESS` | for the weekly email | A postal address (a PO box works) printed at the foot of the weekly email, as US law requires. With the SMTP settings, it turns the weekly email on |

---

## Two more worth setting

**`SENTRY_DSN`** — sign up at sentry.io, create a Python/Flask project, copy
the DSN. Without it, an error at three in the morning is invisible until a user
bothers to email you, and most will simply leave instead. Personal data and
credentials are stripped before anything is sent, and failures the app already
handles (an unknown ticker, a rate limit) are never reported, so the alerts
stay meaningful.

Without Sentry, errors still reach the server log (the Logs tab on Render):
a background job that failed (alert checks, the popular pages, the weekly
email, backups) with its traceback, the items a job skipped and why, a health
check that could not read the database, and a data disk running low. With
Sentry on, everything logged as an error is reported there too.

**`STOCKBOT_ADMIN_EMAIL`** — your Google address. It unlocks `/usage`, which
shows what people actually do: how many came back on a second day, which
instruments they look at, whether they pick intraday or daily bars. That last
one answers the positioning question better than any opinion can. Without this
set, nobody can see that page on a deployment, including you.

Backups run automatically every 12 hours once deployed, are verified before
being kept, and prune to the newest eight. Each is a copy of the database and
four CSV exports, and pruning deletes them together. A deploy restarts the
server, so when the newest backup is older than the interval, the next one runs
a few minutes after start rather than twelve hours later. They live on the same
disk as the database: section 6 copies each one somewhere else as it is taken.

Each new release also copies the database, verified, before its start-up
changes anything in it, into `/data/backups/before-release`, and writes the
release's name to `/data/release.txt` so a restart takes no second copy. The
last three are kept, none for more than a week: they are pruned at every start
and with every timed backup. They are not copied off the server.

---

## Notifications to phones and browsers

People can turn on "Notifications on this device" on the Monitor or Settings
page, and every alert that fires for them then reaches that phone or computer,
even with Orenth closed.

**Browsers need nothing set.** The first time the server starts it makes a key
pair and keeps it beside the database, at `/data/vapid.pem` on Render. Every
browser that turns notifications on is tied to that key, so if the disk were
lost they would all go quiet until each person turned them on again. To make
the key outlive the disk, print it in Render's Shell tab with
`cat /data/vapid.pem` and paste the text, all of it, into a
`VAPID_PRIVATE_KEY` setting (on one line with `\n` where the line breaks were
is fine). It is a secret: it goes in that setting and nowhere else. `STOCKBOT_CONTACT` is given to the push services as
who to contact about this server. On an iPhone, browser notifications work
once Orenth is added to the Home Screen; the Account page explains how.

**The iPhone app needs an APNs key from Apple**, made once:

1. developer.apple.com, Certificates, Identifiers & Profiles, Identifiers:
   open `app.orenth.ios`, tick Push Notifications, and save.
2. Keys, then +: name it "Orenth push", tick Apple Push Notifications service
   (APNs), choose Sandbox & Production if asked, then Continue, Register and
   Download. The `.p8` file can be downloaded only once, so keep it safe.
   Note the Key ID shown on that page.
3. The Team ID is under Membership details.
4. In Render, Environment: set `APPLE_TEAM_ID`, `APNS_KEY_ID` and
   `APNS_PRIVATE_KEY` (the whole `.p8` file's text, as it is or on one line
   with `\n`). Saving redeploys.

The start-up log then says `notifications  browsers on; iPhone app on`. A build
run from Xcode gets sandbox device tokens and TestFlight and App Store builds
get production ones; the server tries production first and remembers the
sandbox for a token only it knows, so one key serves both.

---

## Rolling back to a version without the offline worker

Every browser that opens Orenth installs its service worker, `/sw.js`: the
small script that shows notifications and keeps the "No connection" page. A
rollback does not remove it. A browser asks for `/sw.js` each time it opens
an Orenth page, and a version from before the worker answers "not found",
which a browser takes as "keep the one you have".

Usually that does no harm, and a rollback needs nothing more. The worker
answers only for files whose address ends in `?v=` and its own version's
fingerprint, which the older pages never ask for, so they get their own style
sheet and scripts from the server; and it fetches every page from the server.

If the worker itself is what went wrong, retire it: the rolled-back version
must answer `/sw.js` with `static/sw-retire.js`. That script deletes the files
Orenth kept in the browser, removes the worker, and reloads the Orenth pages
open there. Render's Rollback button cannot add it, so roll back with a revert
instead. On your computer, in the repository:

1. Undo the release, but keep the retiring script. `<merge>` is the merge
   commit that brought the release in.
   `git log main --first-parent --merges --oneline -5` lists the latest merges
   into `main`, newest first; without `--first-parent` the merges inside the
   release are listed too, and reverting one of those undoes only that part.

   ```
   git switch main
   git pull
   git revert --no-edit -m 1 <merge>
   git checkout <merge> -- static/sw-retire.js
   ```

2. Open `web.py`, find the line `@app.route("/robots.txt")`, and paste this
   just above it, followed by two blank lines:

   ```python
   @app.route("/sw.js")
   def retired_worker():
       """Retires the service worker a later version installed (DEPLOY.md)."""
       return send_from_directory(os.path.join(app.root_path, "static"),
                                  "sw-retire.js", mimetype="application/javascript",
                                  max_age=0)


   PUBLIC_ENDPOINTS.add("retired_worker")
   ```

   The last line matters: without it, a browser where nobody is signed in is
   sent to the sign-in page, and a browser never takes a worker from there.

3. Commit and push, and Render deploys it:

   ```
   git add web.py static/sw-retire.js
   git commit -m "Retire the offline worker"
   git push
   ```

Each browser drops the worker the next time it opens any Orenth page. Browser
notifications stop wherever they were on; once a version with the worker is
back, each person turns them on again. Before you deploy such a version,
delete what step 2 pasted, since that version's own `/sw.js` takes its place.

---

## Before you tell anyone about it

Most of this is a script. Run it against the real address:

```
python preflight.py https://<your-address>
```

It checks roughly two dozen things: that the health check reaches the database,
that the privacy and terms pages are readable by a stranger (which is what
Google verifies), that the private pages are not, that sign-in reaches Google
with PKCE and a state parameter, that the client secret is not in the redirect,
and that the session cookie carries HttpOnly, Secure and SameSite. It also
prints the exact callback address to paste into Google, so there is nothing to
mistype.

It is read-only: every request is a GET any visitor could make, so it is safe
to run against a live deployment whenever you want.

Then the few things no script can check:

- [ ] `/healthz` returns ok
- [ ] Sign in with a second Google account and confirm it sees an empty journal,
      not yours. This is the one that matters, and `tests/test_accounts.py`
      asserts it, but check it by hand once on the real deployment.
- [ ] Log a paper trade, reload, confirm it is still there
- [ ] Redeploy, confirm the trade survived. If it did not, the disk is not
      mounted and every deploy is destroying data
- [ ] Open the address on a phone
- [ ] Read `/terms` and `/privacy` and check you agree with what they say on
      your behalf
- [ ] Confirm the alert loop is running: Settings shows the state and when it
      last looked
- [ ] Take a backup and verify it: `python backup.py --csv`, then
      `python backup.py --verify <the file>`. A backup you have never verified
      is a hope, not a backup

---

## Things that will bite

**Do not downgrade to a free instance to save money.** It costs you the disk,
and without the disk every redeploy destroys the journal. It also sleeps, which
stops alert checking. If the monthly cost is the problem, run it locally
instead; a free deployment that loses your data is worse than no deployment.

**The database is one file.** SQLite on a mounted disk is genuinely fine at this
scale and will hold hundreds of users without complaint. What it will not
survive is two machines mounting the same disk, so keep the instance count at
one. `fly.toml` already sets `min_machines_running = 1` and no autoscaling.

**Back it up, and not by copying the file.** The journal and the accumulated
strategy record are the only things here that cannot be rebuilt.

The automatic backups sit on the same 1 GB disk as the database. They cover a
bad write or a mistake, not losing the disk or the account. Render also keeps
daily snapshots of the disk for a few days (check its disk documentation for the
current terms), but those stay with Render too. So keeping a copy somewhere else
is the step that matters most. Section 6 does it for you: each backup is copied
to a storage bucket and checked there. Without it, it is a step for you, the
operator:

- Every week or so, download the newest `stockbot-<date>.db` from
  `/data/backups` (over the host's SSH access, as in the commands below) and
  keep it off the platform: your own machine plus one cloud drive is enough.
- Run `python backup.py --verify <the file>` on the copy you downloaded.
- Each copy is the size of the database, and eight are kept. At 1 GB that is
  comfortable while the database is under about 100 MB; after that, set
  `STOCKBOT_BACKUP_KEEP` lower or make the disk larger. A backup that would
  leave less than 50 MB free is not taken, and the log says so.
- The CSVs are a convenience, not a full copy: the analysis history holds the
  latest 5,000 runs and the theses the latest 1,000, across all users. The
  `.db` file is the backup.
- Backups hold every account's data. A deleted account is gone from them once
  the last copy taken before the deletion is pruned, about four days at the
  defaults and a week for the copies taken before a release, and from any copy
  you downloaded only when you delete that. With copies kept off the server
  (section 6), the monthly ones keep it for up to 12 months, and the privacy
  page says so.

Do not `cat` or `cp` the database. It runs in WAL mode, so at any moment some
committed data lives in the `-wal` file rather than the main one; a copy taken
while the server is writing can open cleanly and be silently missing the most
recent writes. That is the kind of backup you find out is broken on the day you
need it.

`backup.py` uses SQLite's own backup API, which takes a consistent snapshot of a
live database, then verifies the result before keeping it:

```
python backup.py --csv           snapshot, plus plain-text exports
python backup.py --verify FILE   check an existing backup is intact
python backup.py --restore FILE  put a backup in place of the database; then restart the app
```

Restore with `--restore`, never by copying the file over `stockbot.db`: the
`-wal` file beside it would lay the latest writes back over the backup.
Section 6 says more.

On the server:

```
fly ssh console -C "cd /app && python backup.py --out /data/backups --csv"
fly ssh console -C "cat /data/backups/<the file it named>" > local-copy.db
python backup.py --verify local-copy.db
```

The CSVs are worth taking too. A `.db` file is readable only by this program;
the exports are readable by anything, which is what matters if the worst case
is that this app is gone.

**Yahoo Finance is an unlicensed source.** It works, it is free, and it has no
agreement with you. It may rate-limit or break without notice, and it is not a
foundation to take money on top of. Fine for a launch; plan to replace it before
anyone pays.

**Rate limits are shared now.** One person analysing ten instruments is nothing.
Fifty people hitting Yahoo through one server address is a different thing,
because the free sources count requests per address rather than per person.

`bot/upstream.py` exists for this. It does three things:

- **Caches identical requests for 45 seconds.** A portfolio page that made five
  upstream requests makes none on the next load, and nothing measurable changes
  in that window.
- **Collapses simultaneous identical requests into one.** Twelve threads asking
  for the same bars at the same moment produce one upstream request and all
  twelve get its answer. Caching alone does not do this; the cache is still
  empty when all twelve look.
- **Stops asking when the source refuses.** After three throttling responses in
  a row it opens a circuit breaker for a minute, honouring `Retry-After` where
  one is given, and fails fast with a message saying so. Retrying into a rate
  limit is how a shared address gets blocked rather than throttled.

If you do see throttling, the symptom is `/healthz` staying green while
analyses fail, and the message will say plainly that the limit is shared across
the installation rather than a problem with the user's account.

**Known limitation: another iPhone app can start the app's sign-in.** In the
iPhone app, Google sign-in runs in the system browser sheet, and so does
Apple's when the app's own Apple sheet is not there. It comes back through the
app's own address, `app.orenth.ios://auth/finish`, with a one-time code that
works only with a secret kept by whoever started that sign-in. That stops an
app that catches a code meant for Orenth's app. It does not stop an app that
starts a sign-in of its own, with its own secret: if someone signs in on the
sheet that app shows them, that app is signed in to their Orenth account. They
have to sign in for it to happen, so it is a phishing risk, not a silent one.
Closing it needs a return address only Orenth's app can claim: an https
Universal Link, with an apple-app-site-association file on orenth.app, the
Associated Domains capability in the app, and the sheet sent back to that
https address (iOS 17.4 and later).
