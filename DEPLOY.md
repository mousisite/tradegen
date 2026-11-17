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

Without it the app works and says so plainly where a feature is unavailable.
With it you additionally get screenshot reading, LLM sentiment scoring, and the
written reasoning on the research page.

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
| `APPLE_BUNDLE_ID` | no | The iPhone app's bundle id, default `app.orenth.ios` |
| `ANTHROPIC_API_KEY` | no | Screenshot reading, LLM sentiment, written reasoning |
| `SENTRY_DSN` | recommended | Error reporting. Without it a 3am failure is invisible until someone emails you |
| `STOCKBOT_ADMIN_EMAIL` | recommended | Who may see `/usage`. Without it, nobody can on a deployment |
| `STOCKBOT_BACKUP_HOURS` | no | Hours between automatic backups, default 12. Set 0 to switch off |
| `STOCKBOT_BACKUP_DIR` | no | Where they go, default `<database folder>/backups` |
| `STOCKBOT_BACKUP_KEEP` | no | How many to keep, default 8 |
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
disk as the database: see "Back it up" below for the copy you keep elsewhere.

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
is a step for you, the operator, and it is the one that matters most:

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
  defaults, and from any copy you downloaded only when you delete that.

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
```

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
