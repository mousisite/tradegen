# Putting Orenth on the App Store

This is everything for the iPhone app, in order. Do the parts in order.
Each step is one small thing. Words in `grey boxes` are typed or pasted
exactly as written.

The app is a native shell around the live site, https://orenth.app. When you
change the site, the app changes too. You only send Apple a new app when the
native parts change (this folder).

**Part 1** says who signs up with Apple. **Part 2** is the Mac. **Part 3** is
Apple's developer website and the Render settings. **Part 4** is the review
account. **Part 5** is App Store Connect, with every answer ready to paste.
**Part 6** is the list to check before you press Submit.

---

## Part 1. Before you start

1. **Who signs up.** Apple's developer program is only for adults (18 or
   older in most places). If you are under 18, a parent or guardian signs up
   with their own Apple Account and lets you help. Their legal name is shown
   on the App Store as the seller, and money and tax forms go to them.
2. **The cost.** 99 US dollars a year. Sign up at
   https://developer.apple.com/programs/enroll/ as an **Individual**.
3. **Where to sell at first.** Pick **United States only** when App Store
   Connect asks (Part 5). That keeps the rules simple: in the EU, Apple
   publishes an individual seller's home address and phone number.
4. **What you need.** A Mac that can run Xcode 26 or newer (macOS Sequoia
   15.6 or newer for Xcode 26.0 to 26.3; macOS Tahoe for anything newer), an
   iPhone with its cable, and about two hours. Apple has said that from
   April 2027 uploads must be built with Xcode 27, which needs macOS Tahoe
   26.6 or newer, so a Mac that cannot run Tahoe will not do for long.

---

## Part 2. On the Mac

### Install the tools (once)

1. Open the **App Store** app on the Mac. Search for **Xcode**. Click Get.
   It is big; it can take an hour.
2. Open Xcode once. Click **Install** when it offers more parts. When it
   asks which platforms, tick **iOS**.
3. Go to https://nodejs.org and download the **LTS** version (Node 22 or
   newer; Node 24 is fine). Open the file and click through the installer.
4. In Xcode, open **Xcode > Settings > Accounts**. Click **+**, choose
   **Apple Account**, and sign in with the Apple Account that joined the
   developer program.

### Get the code

5. On the GitHub page of this project, click **Code**, then
   **Download ZIP**. Make sure it is the version that has the `mobile`
   folder. Double-click the ZIP to unpack it. Move the folder to your
   **Desktop**.
6. Open the **Terminal** app (press Command and Space, type Terminal,
   press Return).
7. Type `cd ` (with a space after it), then drag the **mobile** folder from
   inside the project folder onto the Terminal window, then press Return.

### Build the app

8. Type this and press Return. It downloads the exact parts the app uses:

   ```
   npm ci
   ```

9. Type this and press Return. It copies the app's settings into the
   iPhone project:

   ```
   npx cap sync ios
   ```

10. Type this and press Return. It opens the project in Xcode:

    ```
    npx cap open ios
    ```

11. Wait until the bar at the top of Xcode stops saying **Resolving
    packages** (it downloads Capacitor from GitHub, so you need internet).

Run steps 9 and 10 again any time you download a newer copy of the code.

### Sign the app and add the three capabilities

12. In Xcode's left column, click the blue **App** icon at the very top.
13. In the middle, under **TARGETS**, click **App**. Then click the
    **Signing & Capabilities** tab.
14. Tick **Automatically manage signing**. Next to **Team**, pick your team
    (the parent's name, if they signed up).
15. Check that **Bundle Identifier** says `app.orenth.ios`.
16. Click **+ Capability** (top left of that tab). Type `Push`. Double-click
    **Push Notifications**.
17. Click **+ Capability** again. Type `Apple`. Double-click **Sign in with
    Apple**.
18. Click **+ Capability** again. Type `Purchase`. Double-click **In-App
    Purchase**.

Xcode writes these into the project itself. Do not edit any other file.

### Run it on your iPhone

19. Plug the iPhone into the Mac. On the iPhone, tap **Trust** and type
    your passcode.
20. On the iPhone, open **Settings > Privacy & Security > Developer Mode**,
    turn it on, and let the iPhone restart.
21. At the top of Xcode, click the device name and choose your iPhone.
22. Press the **Play** button (top left). The first time, it can take a few
    minutes. Orenth opens on the iPhone.
23. Try these on the iPhone:
    - Pull a page down: it reloads.
    - Swipe in from the left edge: it goes back.
    - Tap a link to another website (the research page has filing links):
      it opens in a sheet over the app, with **Done** to close it.
    - Tap **Share** on a popular stock's page: the iPhone's share sheet opens.
    - Turn on **Airplane Mode** and pull a page down: the **No connection**
      page shows. Turn Airplane Mode off and tap **Try again**.
    - In dark mode (Settings > Display & Brightness), the launch screen and
      the status bar are dark too.

### Send the app to Apple (each new version)

24. Click the blue **App** icon, then **App** under TARGETS, then the
    **General** tab. Set **Version** to `1.0` the first time. Set **Build**
    to `1`. Every upload needs a bigger Build number: `2`, then `3`, and so
    on.
25. At the top of Xcode, click the device name and choose **Any iOS Device
    (arm64)**.
26. In the menu bar, click **Product > Archive**. Wait.
27. A window called **Organizer** opens. Click **Distribute App**, then
    **App Store Connect**, then **Distribute**. Keep the choices it offers.
28. After 10 to 30 minutes, the build shows in App Store Connect under your
    app's **TestFlight** tab. Apple emails you if something is wrong.

---

## Part 3. On Apple's developer website, then on Render

Go to https://developer.apple.com/account, then **Certificates, Identifiers
& Profiles**.

1. **The app's ID.** Click **Identifiers**. If `app.orenth.ios` is already
   there (Xcode often makes it in Part 2), click it. If not, click **+**,
   choose **App IDs**, then **App**, type the description `Orenth` and the
   Bundle ID (Explicit) `app.orenth.ios`. Either way, make sure **Push
   Notifications** and **Sign In with Apple** are ticked. Leave Sign In with
   Apple as **Enable as a primary App ID**. Save.
2. **Tell Apple where account changes go.** On that same App ID, click
   **Configure** (or **Edit**) next to Sign In with Apple. Server-to-Server
   Notification Endpoint: `https://orenth.app/auth/apple/notifications`.
   Save.
3. **The website's ID (the Services ID).** Click **Identifiers**, then
   **+**, then **Services IDs**. Description `Orenth website`, identifier
   `app.orenth.web`. Register. Click it, tick **Sign In with Apple**, click
   **Configure**. Primary App ID: `app.orenth.ios`. Domains and Subdomains:
   `orenth.app`. Return URLs: `https://orenth.app/auth/apple/callback`.
   Click Done, Continue, Save.
4. **Key one: Sign in with Apple.** Click **Keys**, then **+**. Name it
   `Orenth Sign in with Apple`. Tick **Sign in with Apple**, click
   **Configure**, choose `app.orenth.ios`, Save. Continue, Register. Click
   **Download**. You can download it **only once**, so keep the `.p8` file
   somewhere safe (not in the project folder). Write down its **Key ID** (ten
   letters and numbers).
5. **Key two: push notifications.** Click **Keys**, then **+** again. Name
   it `Orenth push`. Tick **Apple Push Notifications service (APNs)**. If it
   asks which environment, choose **Sandbox & Production**. Continue,
   Register, **Download** (again, only once), and write down this Key ID
   too.

   (DEPLOY.md's Sign in with Apple section suggests one key for both jobs.
   That works too. Two keys mean you can replace one without breaking the
   other. If you already made one key for both, use its Key ID and file for
   both pairs of settings below.)
6. **Your Team ID.** It is at the top right of the developer website and
   under **Membership details**: ten letters and numbers.
7. **Email to people who hide their address.** Click **Services**, then
   **Sign in with Apple for Email Communication**, then **Configure**. Add
   the domain your emails are sent from (the part after @ in `MAIL_FROM`).
   Without this, sign-in codes, alerts and the weekly email never reach
   anyone who chose "Hide My Email".

### The Render settings

On https://dashboard.render.com, open the Orenth service, then
**Environment**, then **Add Environment Variable** for each line. To copy a
`.p8` file, open it in TextEdit, press Command-A, Command-C, and paste the
whole thing (the BEGIN and END lines too) as the value.

| Setting | What to put in it | Where it comes from |
| --- | --- | --- |
| `APPLE_TEAM_ID` | your Team ID | step 6 |
| `APPLE_SIGNIN_KEY_ID` | the Key ID of key one | step 4 |
| `APPLE_SIGNIN_PRIVATE_KEY` | the whole text of key one's `.p8` file | step 4 |
| `APPLE_SERVICES_ID` | `app.orenth.web` | step 3 |
| `APPLE_BUNDLE_ID` | `app.orenth.ios` | step 1 |
| `APNS_KEY_ID` | the Key ID of key two | step 5 |
| `APNS_PRIVATE_KEY` | the whole text of key two's `.p8` file | step 5 |
| `REVIEW_LOGIN_EMAIL` | the review account's email | Part 4 |
| `REVIEW_LOGIN_CODE` | the review account's code | Part 4 |
| `STOCKBOT_CONTACT` | an email address people can write to | you |

Click **Save Changes**. Render restarts the site. Its log then lists the ways
to sign in that are set up.

Never put a `.p8` file, a key, or the review code into the project or on
GitHub. The project is public.

---

## Part 4. The review account

Apple's reviewers cannot get the codes Orenth emails or texts. So one account,
used only for App Review, signs in with a fixed code instead.

1. Pick an email address you own and use for nothing else, for example a new
   free email account. This is `REVIEW_LOGIN_EMAIL`.
2. Make a long random code: in Terminal, type `openssl rand -hex 12` and
   press Return. Copy the 24 letters and numbers it prints. This is
   `REVIEW_LOGIN_CODE`. It must be at least 16 characters long, or the
   review sign-in stays switched off.
3. Put both in Render (Part 3).
4. Test it on your iPhone: open **Safari**, type
   `app.orenth.ios://open?path=/signin/review` in the address bar, tap Go,
   then tap **Open**. Orenth opens on **Review sign-in**. Type the email and
   the code. You are signed in.
5. While signed in as the review account, add one stock to the watchlist and
   one alert, so the reviewer sees the app in use. Leave the account on the
   **Free** plan: the reviewer must be able to try buying Pro.

The review sign-in is linked from nowhere on the site, works only for that
one account, never makes it Pro, and stops a network after 15 wrong tries an
hour. Change the code if it ever leaks: change it in Render and in App Store
Connect's review notes.

---

## Part 5. App Store Connect

Go to https://appstoreconnect.apple.com.

### Make the app's page (once)

1. Click **Apps**, then **+**, then **New App**.
2. Platforms: **iOS**. Name: `Orenth`. Primary language: **English (U.S.)**.
   Bundle ID: `app.orenth.ios`. SKU: `orenth-ios`. User Access: **Full
   Access**. Click Create.

### App Information

| Field | Answer |
| --- | --- |
| Name | `Orenth` |
| Subtitle (30 characters at most) | `Stock & crypto research` |
| Category | Primary: **Finance**. Secondary: leave empty, or **Education** |
| Content Rights | See "Content rights" below |
| Age Rating | See "Age rating" below |

### Pricing and Availability

- Price: **Free** (Pro is sold inside the app, through Apple).
- Availability: **United States** only, to begin with.

### Version page (the "1.0 Prepare for Submission" page)

**Promotional Text** (can be changed any time without a review):

```
See how often a setup worked in tests on past prices, after real trading costs, and get a straight answer when there is no edge. Free to start.
```

**Description:**

```
Orenth is a research and learning tool for stocks and crypto: charts, historical back-tests, a practice (paper-trading) journal that uses virtual money only, and price alerts. Orenth is not a broker, exchange or wallet. You cannot buy, sell, deposit or hold real securities, crypto or money in Orenth, and nothing in the app is personalized investment advice.

WHAT IT DOES
- Analyse any stock or coin. Orenth back-tests 34 strategies against that instrument's own price history, takes off realistic trading costs, and says buy, wait or avoid, with the count behind it: how often this setup came up in tests on past prices, and how often it reached its target first.
- Says so when there is no edge. Often the honest answer is to wait, and Orenth says it plainly.
- Find: choose stocks or crypto and a risk level, and Orenth ranks what is worth a closer look.
- Screener: filter companies by the numbers in their SEC filings. Company figures on the research page link to the filing they came from.
- Journal: log practice trades with an entry, a stop and a target, then find out whether you were right or just lucky.
- Portfolio: see what your logged positions add up to, and what a bad day could cost.
- Alerts: pick a price or watch a stock, and get a notification when something happens.
- Track record: every day Orenth writes down its call on popular stocks and coins, and scores every one of them, wins and losses alike.
- Strategy tests: see which strategies did best in tests on past prices of each stock and coin, after costs.

LOOK AROUND FIRST
Explore a full example account without signing up. Create a free account to keep your own journal, watchlist and alerts.

FREE AND PRO
Orenth is free, with a daily limit on how many different stocks and coins you can analyse. Pro removes the limits.

PRIVACY
No ads and no tracking. Your journal, watchlist and alerts belong to your account. Your journal and saved theses can be downloaded, and your account deleted, at any time from the Account page.

Orenth is analysis software, not financial advice. Every figure is measured from past prices and can stop describing the future at any moment. Never risk money you cannot afford to lose.

Terms of use: https://orenth.app/terms
Privacy: https://orenth.app/privacy
```

When Pro goes on sale in the app, add one line before "Terms of use" with
each plan's name, length and price, as App Store Connect shows them.

**Keywords** (100 characters at most; the words in the name and subtitle
already count, so they are not repeated):

```
backtest,alerts,screener,watchlist,bitcoin,ethereum,charts,paper trading,journal,SEC filings,ETF
```

| Field | Answer |
| --- | --- |
| Support URL | `https://orenth.app/terms` (set `STOCKBOT_CONTACT` first, so the page shows an email address) |
| Marketing URL | `https://orenth.app/` |
| Copyright | `2026` and the account holder's full legal name, for example `2026 Jane Doe` |
| Version | `1.0` |

**Screenshots.** In this folder, `store/screenshots/`:

- `6.9-inch/` (1320 x 2868): drag all six into **iPhone 6.9" Display**.
- `6.3-inch/` (1206 x 2622): if App Store Connect also asks for **iPhone
  6.3" Display**, drag these six there.

To take them again after the site changes, see
`scripts/take_screenshots.py`.

**Build.** Click **+** next to Build and pick the build from Part 2.

### App Privacy

Privacy Policy URL: `https://orenth.app/privacy`

Click **Get Started** next to Data Types. "Do you or your third-party
partners collect data from this app?" **Yes.** Apple counts what the website
keeps, because the app shows the website.

Tick these. For every one, answer: **Linked to the user: Yes**, **Used for
tracking: No**, **Purpose: App Functionality** (and nothing else).

| Data type | Why Orenth keeps it |
| --- | --- |
| Contact Info: **Email Address** | It is the account's identity; sign-in codes and the optional weekly email go to it |
| Contact Info: **Name** | From Google or Apple sign-in, to show who is signed in |
| Contact Info: **Phone Number** | Only if text-message sign-in is switched on (the Twilio settings). Skip it otherwise |
| Financial Info: **Other Financial Info** | The account size, positions and prices people type into the journal and settings. No bank, card or brokerage details, ever |
| User Content: **Other User Content** | Journal notes, saved theses, the watchlist and alerts |
| Identifiers: **User ID** | The account number, and the Google or Apple account number |
| Identifiers: **Device ID** | The notification address of each iPhone that turns notifications on |
| Purchases: **Purchase History** | Whether the account has Pro, and its subscription record |
| Search History | The history of stocks and coins each person analysed, shown back to them |
| Usage Data: **Product Interaction** | Which stocks each free account opened today (the daily allowance), and its alert history |
| Other Data: **Other Data Types** | The network address of each sign-in code request (kept 30 days, to stop abuse), and the browser description saved with each sign-in |

Add these only if the setting is switched on in Render:

| If this is set | Add | Linked | Why |
| --- | --- | --- | --- |
| `SENTRY_DSN` | Diagnostics: **Other Diagnostic Data** | Yes | Error reports can include the email of the person whose page failed |
| `SENTRY_TRACES_RATE`, above 0 | Diagnostics: **Performance Data** | No | A sample of page loads that worked: the address without its query, and how long it took. Off unless this is set |
| `ANTHROPIC_API_KEY` | User Content: **Photos or Videos** | No | A chart screenshot someone uploads goes to Anthropic to be read. Orenth does not keep it |

Do **not** tick: Payment Info (Apple and Stripe take cards; Orenth never sees
them), Location, Contacts, Health, Browsing History, Audio, Sensitive Info,
Advertising Data, Crash Data. The app has no ads and no tracking, so it
never asks the "Allow tracking?" question.

Only daily totals with nothing about who did what are kept about visits (see
the privacy page), so they are not listed.

### Age rating

Answer the questionnaire like this. The result should be **4+**.

| Question | Answer | Why |
| --- | --- | --- |
| Parental Controls | No | |
| Age Assurance | No | |
| Unrestricted Web Access | **No** | The app shows only orenth.app. Links to any other site open in a sheet with a Done button, and the app's own view never leaves orenth.app |
| User-Generated Content | No | Shared calls are pages Orenth makes (stock, call, date), with no text from people |
| Messaging and Chat | No | |
| Advertising | No | |
| Social media features | No | |
| Profanity or Crude Humor | None | |
| Horror/Fear Themes | None | |
| Alcohol, Tobacco or Drug Use or References | None | |
| Mature or Suggestive Themes | None | |
| Medical or Treatment Information | None | |
| Health or Wellness Topics | No | |
| Sexual Content or Nudity | None | |
| Violence (cartoon, realistic or graphic), weapons | None | |
| Simulated Gambling | None | Practice trades are a journal of virtual trades, not a game of chance |
| Gambling, Contests, Loot Boxes | No / None | |
| Shared Location | No | |
| Dating and Relationships | None | |

### Export compliance (encryption)

The app only uses the standard encryption built into iOS (https). The
project already says so (`ITSAppUsesNonExemptEncryption` is NO), so App Store
Connect does not ask. If it ever does: "Does your app use encryption?" Yes.
"Does it qualify for an exemption?" Yes: only standard encryption.

### Content rights

"Does your app contain, show, or access third-party content?" **Yes**: market
prices, company filings and news headlines.

- Company figures come from SEC EDGAR, which is public US government data.
- **Prices come from Yahoo Finance without a licence.** That is fine for a
  free hobby project, but not for an app that charges money. Before Pro goes
  on sale, move to a price provider whose licence covers a paid app. Apple
  can ask for proof that you may show the data (rule 5.2.2). Until then,
  answer the "rights" question honestly.

### App Review Information

- **Sign-in required**: tick it. User name: the `REVIEW_LOGIN_EMAIL`.
  Password: the `REVIEW_LOGIN_CODE`.
- **Contact information**: the account holder's name, phone and email.
- **Notes**: paste this, then fill in the four `[square brackets]`. It
  must stay under 4,000 characters (it is about 2,400).

```
ABOUT ORENTH
Orenth is a research and learning tool for stocks and crypto: historical back-tests, charts, price alerts and a practice journal that uses virtual money only. It is not a broker, exchange or wallet. There is no trading, no real money, no deposits or withdrawals, no wallets, and no linking of bank or brokerage accounts. Nothing in it is personalized investment advice, so guidelines 3.2.1(viii), 3.1.5 and 5.1.1(ix) do not apply.

LOOK AROUND WITHOUT SIGNING IN
On the first screen, tap "Look around first". It opens a full example account (journal, alerts, watchlist) with no sign-in.

THE REVIEW ACCOUNT
Orenth signs people in with a code sent by email or text, which App Review cannot receive. This one account uses a fixed code instead:
1. In Safari on the device, type app.orenth.ios://open?path=/signin/review in the address bar and tap Go, then tap Open. Orenth opens on "Review sign-in".
2. Email: [REVIEW_LOGIN_EMAIL]
   Code: [REVIEW_LOGIN_CODE]
The account is on the free plan, so buying Pro can be tested. Sign in with Apple also works with your own Apple Account.

WHAT IS NATIVE
- Sign in with Apple: Sign in (top right) > Continue with Apple. Apple's own sheet, with Face ID.
- Google sign-in opens in a secure Safari sheet, never inside the app's own web view.
- Notifications: sign in, open Menu > Monitor, find "Notifications on this device", tap Turn on and allow notifications, then tap Send a test. A real alert: add a "Price rises above" alert for any stock at a price below today's, then tap "Check them all now". Tapping a notification opens the page it is about.
- Share sheet: front page > "Today's checks on popular stocks and coins" > any stock > Share.
- Pull any page down to reload it. Swipe in from the left edge to go back.
- Offline screen: turn on Airplane Mode and pull a page down. Tap Try again once back online.
- Links to other websites open in a sheet over the app with a Done button.
- In-app purchase: [where Pro is bought in the app, for example Menu > Account > See Pro].

DELETING AN ACCOUNT
Signed in: tap the round account picture at the top right > Account > "Leaving" at the bottom of the page > type delete > Delete my account. Everything is deleted straight away, not deactivated.

DATA
Prices: [the price provider and its licence]. Company figures: SEC EDGAR (public). No ads, no tracking, no third-party analytics in the app.
```

### Submit

1. Click **Add for Review** at the top right, then **Submit for Review**.
2. Apple usually answers within one to three days, by email. If they reject
   it, the message says which rule and why. Reply in App Store Connect's
   **Resolution Center**; most answers need no new build.

---

## Part 6. Check these before you press Submit

Apple rejects apps for these, so check each one on your own iPhone, in the
app, signed in as the review account:

1. **Google sign-in works in the app** and leaves you signed in to the app,
   not to Safari. **Sign in with Apple** works too.
2. **Pro can be bought in the app with Apple's in-app purchase**, and there
   is a **Restore** button. Apple will not accept an app where Pro is bought
   only on the website (rule 3.1.1). Inside the app, the site already hides
   every Stripe button, web price and discount box.
3. **No page says "not set up"**, or shows an error about a missing key. If
   the chart-screenshot reader is on, the app must say that the picture is
   sent to Anthropic and ask before the first one (rule 5.1.2).
4. **Nothing mentions Android, Google Play or installing an app**, and there
   is no "add to Home Screen" box inside the app.
5. **The site answers quickly.** If Render is asleep or slow, App Review sees
   a blank screen. Make sure Cloudflare is not showing a "checking your
   browser" page to phones.
6. **Account deletion** works from the Account page and confirms it is done.
7. The **review sign-in** works (Part 4, step 4), and the review account is
   on the **Free** plan.

---

## When you change something

- **Only the website changed:** nothing to do here. The app shows the new
  site at once.
- **Something in this `mobile` folder changed:** on the Mac, get the new
  code, run `npm ci`, `npx cap sync ios` and `npx cap open ios` in the
  `mobile` folder, raise the **Build** number (Part 2, step 24), then
  Archive and Distribute again (steps 25 to 27). In App Store Connect, make
  a new version, pick the new build, and submit.

## For whoever works on the code next

- `capacitor.config.json`: the site the app loads, its offline page, and
  the launch screen, status bar and notification settings. Run
  `npx cap sync ios` after any change.
- `www/offline.html`: the page shown when orenth.app cannot be reached.
- `ios/App/App/MainViewController.swift`: the screen (swipe back, pull to
  refresh). `ios/App/App/WebAuthPlugin.swift`: Apple's sign-in sheet for
  Google sign-in (JavaScript name `WebAuth`).
- `scripts/add-native-files.js`: lists new native files in the Xcode project
  and keeps the app iPhone-only. `scripts/check-project.js` checks it.
- `scripts/make_assets.py`, then `npx @capacitor/assets generate --ios`:
  the icon and launch screens, drawn from `tools/make_logo.py`.
- On the website, `static/native.js` connects pages to the phone, and
  `in_app` in the templates says a page is drawn inside the app.
