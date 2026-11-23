# Orenth

**Live at [orenth.app](https://orenth.app).** Free, no ads, no tracking scripts.

Give it a screenshot of a chart. It identifies the instrument, pulls live market
data, runs **44 trading strategies**, measures which of them actually work on
that instrument, reads the news and social chatter, and tells you to buy, wait,
or stand aside. Everything it says and every trade you take is recorded in a
database so the record accumulates.

What separates it from most retail trading tools: **it does not invent
probabilities**. It replays its own strategy logic across the instrument's
history, counts how often those setups reached target before stop, charges
realistic trading costs, and reports the result with a confidence interval and a
sample size. When the evidence says a setup loses money, it refuses the trade.

---

## Quick start

**Double-click `Stock Bot.bat`.**

It starts a small local web server and opens your browser at:

```
http://127.0.0.1:8000
```

Leave that window open while you use it. Closing it, or pressing Ctrl+C, stops
the server. Everything needed is bundled in `.runtime\`, so no system Python is
required.

### The web interface

| Page | What it does |
|---|---|
| **Analyse** | Type a ticker or drop in a chart screenshot, get the full call with levels, odds and reasoning |
| **Find** | Pick what you trade and how long you hold; it back-tests a list of instruments and shows only what cleared the bar |
| **Research** | The business behind the ticker: filed financials, valuation, a two-sided thesis, options pricing, recent filings, every claim cited |
| **Screener** | Ready-made and custom screens over a chosen universe, showing the numbers that got each name through |
| **Portfolio** | What you are exposed to: concentration, sector weights, correlation between holdings, a market-shock table |
| **Trades** | Open positions marked to market, close or delete them, log trades by hand, realised performance |
| **Monitor** | Alerts, a watchlist, saved theses, and four routines that do the checking for you |
| **Strategies** | Leaderboard of all 44 strategies, filterable by instrument and market conditions |
| **Settings** | Account size, risk, broker costs and thresholds, editable in the browser with validation |
| **Price action, tested** | `/learn`: the ideas a popular day-trading course teaches, each in plain words beside what the Setup lab measured, and a practice page for calling the next move |

**Screenshots.** Drag a chart onto the page, click to browse, or press Ctrl+V
after snipping with Win+Shift+S. It reads the ticker and the timeframe off the
image and analyses that instrument. This needs an Anthropic API key; without one
the page says so plainly and typing a ticker still works.

The image is only ever used to identify the instrument. Everything after that
runs on live data, because a screenshot can be minutes stale by the time it is
read.

From an analysis you can click **Log this as a paper trade** to record exactly
the plan the bot proposed, correctly sized against your account. The journal
also takes trades entered by hand, for anything the bot did not suggest.

There is also a JSON endpoint if you want to build on it:

```
http://127.0.0.1:8000/api/analyse?symbol=AAPL&interval=1d
```

Options: `Stock Bot.bat --port 9000` to change the port, or
`Stock Bot.bat --host 0.0.0.0` to reach it from your phone on the same
Wi-Fi network. Bound to localhost by default, so nothing is exposed to the
internet and your data never leaves the machine.

### Other ways in

`Stock Bot (terminal menu).bat` gives the same features as a text menu.

Prefer the command line? Open PowerShell in this folder:

```
run.bat --symbol AAPL              analyse a ticker
run.bat --clipboard                snip a chart with Win+Shift+S, analyse it
run.bat chart.png                  analyse a screenshot file

paper.bat take AAPL                open a paper trade from the last plan
paper.bat check                    mark open trades to market
paper.bat close 3 --price 317.5    close a position
paper.bat delete 3                 remove a trade entirely
paper.bat stats                    how your paper trading is going
paper.bat leaderboard              which strategies measure best

paper.bat open AAPL --entry 200 --stop 195 --target 210 --qty 10
```

If `.runtime\` is ever missing, `setup.bat` rebuilds it.

### Optional: an Anthropic API key

Without a key it still works. You pass `--symbol` instead of a screenshot, and
headlines are scored by a built-in finance lexicon.

With a key you also get screenshot reading (ticker, timeframe, visible price)
and better news sentiment, because an LLM reads "beats earnings but guides
lower" correctly where keyword scoring reads it as positive.

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY`.

---

## What it can do

Everything below works without an API key except the two rows that say
otherwise. Each links to where the work happens.

| | Capability | Where | What it actually does |
|---|---|---|---|
| 1 | Real-time market data | `bot/market.py` | OHLCV for equities, ETFs and crypto across seven intervals, with stub bars trimmed and the forming bar separated so signals do not flicker |
| 2 | Technical analysis | `bot/indicators.py`, `bot/strategies.py` | 30+ indicators and 44 strategies, each scored, weighted by regime, and discounted when it duplicates a sibling in the same family |
| 3 | Backtesting | `bot/calibrate.py` | Walk-forward replay with Wilson confidence intervals, striding to decorrelate samples, and a pessimistic tie-break when a bar touches both stop and target |
| 4 | Real-time news and sentiment | `bot/sentiment.py` | Google News and StockTwits, scored by a finance lexicon that handles negation, or by an LLM when a key is present |
| 5 | SEC filings analysis | `bot/sec.py` | Filing history and XBRL company facts, each linked back to the document |
| 6 | Fundamental analysis | `bot/fundamentals.py` | Forty-odd ratios, each carrying its source, with Yahoo reconciled against what was actually filed, and a quality score that refuses to answer on too little data |
| 7 | Financial modeling | `bot/valuation.py` | Discounted cash flow on a three-year average of filed free cash flow, growth fading toward terminal, a 5x5 sensitivity grid, plus a reverse DCF that solves for the growth the price implies |
| 8 | Advanced stock screener | `bot/screener.py` | Two stages: bulk quotes first, full fundamentals only for names that already passed, so a wide screen stays fast |
| 9 | Options analysis | `bot/options.py` | Black-Scholes pricing and Greeks, implied volatility by bisection, chain quality graded, expected move, and concrete ideas |
| 10 | Risk analysis | `bot/risk.py`, `bot/decision.py` | Historical value at risk, expected shortfall, drawdown, beta, and gap risk stated in cash: what the instrument's worst measured day would cost through the stop |
| 11 | Portfolio analysis | `bot/portfolio.py` | Concentration by Herfindahl index, sector weights, a correlation matrix between holdings, and a market-shock table scaled by each holding's beta |
| 12 | Bull and bear thesis | `bot/thesis.py` | Both cases argued from the gathered evidence, each point carrying what would prove it wrong |
| 13 | Source citations | throughout | Every figure carries where it came from; filings link to sec.gov |
| 14 | Investment thesis memory | `bot/thesis.py`, `bot/database.py` | A thesis is saved with the price at the time and closed out later as right, wrong, right-by-luck, or undecided. The outcome offered when you close one is the one the price supports, not the one you remember |
| 15 | Alerts | `bot/alerts.py`, `bot/scheduler.py` | Thirteen conditions covering price, percentage moves, RSI, 52-week highs and lows, volume, moving averages and a change of call, plus the specific ones a given trade plan implies, set in one click. Checked on a timer by the server, not only when you open the page |
| 15b | Scheduled events | `bot/catalysts.py` | Whether earnings or an ex-dividend date falls **inside this plan's own holding window**, which is the only version of that question that changes a decision |
| 15c | Position overlap | `bot/portfolio.py` | Before you add a position, whether it is really a new one or more of what you already hold |
| 15d | Export | `bot/export.py` | The journal, theses, measured strategy record and analysis history as CSV or JSON |
| 16 | Paper trading | `paper.py`, `bot/database.py` | Positions sized against your account, marked to market, closed with the R-multiple computed |
| 17 | Automated workflows | `bot/workflows.py` | Four routines: check open positions, a morning sweep, hunt for setups, re-analyse the watchlist |
| 18 | AI financial reasoning | `bot/reasoning.py` | Reasons over the facts already gathered, never over its own recollection. Needs a key |
| 19 | Published scoring models | `bot/quality.py` | Piotroski F-Score, Altman Z-Score, Beneish M-Score and the accruals ratio, computed from filed figures. Not invented here: each was published and tested against decades of filings |
| 20 | Charts | `bot/charts.py` | Price history and filed financials, drawn as inline SVG with no charting library and no third-party script |
| 21 | Error reporting | `bot/monitoring.py` | Optional Sentry, with personal data and credentials stripped before anything is sent, and expected failures never reported |
| 22 | Scheduled backups | `backup.py`, `serve.py`, `bot/offsite.py` | A verified snapshot on a timer, pruned to the newest few, and copied to a private storage bucket off the server when that is set up |
| 23 | Usage | `bot/usage.py` | What people actually do, read from rows the app already wrote. Operator only |
| 24 | Find something | `bot/ideas.py` | Pick stocks or crypto and how long you hold; it back-tests a list and shows only what cleared the bar. Refuses to pad the list |
| 25 | Plain English | `bot/plain.py` | The same answer without the jargon, above the technical one. A test fails if jargon leaks in |
| 26 | Setup lab | `bot/setuplab.py`, `bot/structure.py`, `bot/liquidity.py`, `bot/labstore.py` | The price-action setups taught in TJR's free day-trading video, found in 5- and 15-minute prices of eight futures, funds, coins and stocks, each traded with the course's own stop and two targets, after costs, beside random entries at the same half hour of other days, with a margin of error that counts one day's trades together. Nightly |
| 27 | Claims tested | `bot/claims.py`, `bot/clock.py` | The video's checkable claims (London takes Asia's range, yesterday's high then its low, the 9:50 window, gaps that fill, related markets that disagree) measured against what happens anyway, with a margin and a verdict, or "too few to say" |
| 28 | Price-structure panel | `bot/levelview.py`, `bot/bias.py` | On an analysis, what price is doing in plain words: the trend, the last break of structure, highs or lows taken, live gaps, the 50% line, yesterday's and the sessions' levels, and the daily lean beside its measured record. Times in New York time |
| 29 | Price-action alerts | `bot/alerts.py` | Yesterday's high or low taken, taken and closed back, today's gap filled, a break of structure, a pullback into a gap, London's range taken. Facts with prices and times, never forecasts |
| 30 | Journal review and rules | `bot/journal.py`, `bot/database.py` | Tags for setup, feelings and the pre-trade checklist, a review after each trade, results broken down by each, and your own daily limits on losses, trades and time |
| 31 | Size and survival tools | `bot/journal.py` | Position size for shares, coins or a futures contract from account, risk and stop; and how often a risk per trade survives a long losing run, from your own trades or typed-in figures |
| 32 | Learn | `bot/learn.py`, `bot/setups.py` | `/learn`: each idea from the video in plain words, what the course teaches with timestamps, the exact rule Orenth codes with its own choices marked, and what the lab measured. Every number comes from the store; the copy states none |
| — | Screenshot reading | `bot/vision.py` | Identifies the instrument and timeframe from a chart image. Needs a key |

---

## What it measured

Read this before trading anything with it. These numbers came from the bot's own
back-tests over ten instruments and four timeframes, more than 200,000 simulated
trades.

**Strategies do have a small directional edge, and costs eat it.**

| Strategy | Samples | Gross expectancy | Net of costs |
|---|---|---|---|
| linreg_channel | 9,126 | +0.044R | -0.007R |
| obv_divergence | 9,470 | +0.042R | -0.121R |
| vwap_band_fade | 24,317 | +0.031R | -0.176R |
| rsi_reversion | 35,211 | +0.026R | -0.192R |

Gross expectancy is skill before costs. Net is what you would have kept. The
gap between those two columns is the whole story of retail day trading.

**Costs dominate short timeframes.** Round-trip cost expressed in units of risk:

| Instrument | 5m | 15m | 1h | 1d |
|---|---|---|---|---|
| BTC-USD | 1.46R | 0.81R | 0.27R | 0.04R |
| ETH-USD | 1.06R | 0.58R | 0.20R | 0.03R |
| SPY | 0.17R | 0.09R | 0.04R | 0.01R |
| AAPL | 0.07R | 0.04R | 0.02R | 0.01R |

At 20bp round-trip on a 5-minute crypto bar, fees exceed the entire stop
distance. Break-even for 5-minute BTC is about 98%. Scalping crypto at retail
fee levels is arithmetically unprofitable before strategy quality even enters
the picture.

Net expectancy per trade:

| Instrument | 5m | 1d |
|---|---|---|
| BTC-USD | -1.462R | -0.192R |
| ETH-USD | -1.074R | **+0.071R** |
| SPY | -0.164R | **+0.012R** |
| AAPL | -0.236R | -0.034R |

The only positive-expectancy configurations were on **daily** bars. Hit rate
barely improves across timeframes; the gain comes from amortising cost over a
larger move.

**Set `cost_bps_equity` and `cost_bps_crypto` in `config.json` to your real
broker costs.** They change the verdict more than any indicator setting.

---

## Price action, measured

TJR's video "How To Start Day Trading In 2026" teaches a set of price-action
setups (sweeps of old highs and lows, breaks of structure, fair value gaps,
the 50% line, and the sweep, break and retest model) and makes claims about
sessions, gaps and timing. The Setup lab (`bot/setuplab.py`) and the claims
(`bot/claims.py`) test them on past prices. This section is the method; the
results are on the site, and change as the data does.

**Universe.** ES and NQ futures, SPY, QQQ, Bitcoin, Ether, Apple and NVIDIA
(`setups.LAB_SYMBOLS`), on 5- and 15-minute bars. The data source keeps about
60 days of those, so every result is one stretch of the market. Stocks and
funds have day-session prices only, so they have no Asia or London range.

**Entries.** Every time a setup appears (no thinning), at the close of that
bar. A setup is found by `bot/liquidity.py`, whose value at each bar uses
only that bar and the ones before it; a test cuts the bars at each entry and
checks the trade is the same.

**Stops.** Where the course puts them: past the high or low that was taken,
past the far end of the move a break ended, past a gap's far edge, past the
bar that turned at the 50% line. A stop closer than a tenth of a usual bar's
range (ATR 14) is not tradable; those events are counted and left out.

**Targets.** Two rules, kept apart: twice the risk, and the nearest old high
or low not yet traded through that is at least the risk away (twice the risk
when there is none, counted).

**Exits.** The stop, the target, or the next 16:00 New York time at that
bar's close. The stop counts first when one bar reaches both, and a bar
opening past the stop fills at its open (the rules of
`calibrate.simulate_trade`, checked trade for trade).

**Costs.** The house round-trip cost for each instrument, in units of the
trade's risk: futures 1 basis point, stocks and funds 2, coins 20.

**Control.** Each trade is set beside up to five random entries on other
trading days in the same New York half hour, the same side, with a stop and
target the same size against that bar's usual range, the same exit and the
same costs. A setup that keeps money but no more than random trades at that
time of day has shown nothing about itself.

**Margin.** Two standard errors, with trades on the same trading day counted
together, and more when the days are few (2.4 on 10 days). A setup needs 30
trades on 10 separate days before it gets a verdict: kept money by more than
its margin, lost money by more than it, or cannot be told apart from luck. About 1 cell in 20 crosses its margin by
luck alone, and many cells are tested, so a single green cell proves little.
ES and SPY (and NQ and QQQ) follow the same indexes: pooled together, they
overstate the evidence.

**Holdout.** The last third of the trading days is checked apart from the
first two thirds; a result whose sign flips there is marked not stable. The
four-step model is also run with wider swings (three bars each side).

**Left out.** Futures roll days and days with under 80% of their bars, and
trades that would carry through a roll. Each is counted.

**Claims.** Each checkable claim is measured against a control of the same
size: London taking one side of the Asia range and reaching the other is set
beside that range mirrored around the 03:00 price; the 9:30-9:50 window beside
the same test an hour later; a close past a swing beside a wick past it; gaps
beside random zones of the same size; the daily lean beside always saying up.
An opening gap is set beside the level the same distance the other way. Coins'
Saturdays and Sundays are left out of the claims about the sessions and the
New York open.

**When.** Nightly on the server after 17:05 New York time, or by hand:

```
python -m bot.setuplab --refresh --symbols SPY --intervals 15m
python -m bot.setuplab              # print what is stored
```

A partial run leaves the claims alone; add `--claims` to measure them anyway.
It does not stand for the night's run, which still goes.

---

## The strategy library

44 strategies in seven families. Each returns a directional score from -1 to
+1 with a plain-language reading, and all of them have a say in the call.

**Trend (10)** — `trend_stack`, `macd_momentum`, `supertrend_follow`,
`adx_di_cross`, `ichimoku_cloud`, `parabolic_sar_flip`, `linreg_channel`,
`aroon_trend`, `tsi_trend`, `roc_momentum`

**Mean reversion (8)** — `rsi_reversion`, `bollinger_fade`,
`keltner_reversion`, `zscore_reversion`, `stochastic_reversal`,
`williams_reversal`, `cci_reversion`, `vwap_band_fade`

**Breakout (5)** — `squeeze_breakout`, `donchian_breakout`,
`opening_range_break`, `volatility_expansion`, `inside_bar_break`

**Volume (4)** — `volume_confirmation`, `obv_divergence`, `mfi_extremes`,
`accumulation_trend`

**Structure (4)** — `vwap_position`, `market_structure`, `pivot_levels`,
`gap_behaviour`

**Pattern (3)** — `engulfing_pattern`, `pin_bar`, `momentum_thrust`

**Liquidity (10)** — `sweep_reclaim`, `prior_day_sweep`,
`equal_levels_sweep`, `session_sweep`, `structure_flip`, `sweep_then_flip`,
`fvg_retest`, `ifvg_flip`, `premium_discount`, `liquidity_model`

The liquidity family is the price-action setups taught in TJR's video "How To
Start Day Trading In 2026": an old high or low taken and closed back past,
yesterday's high or low, equal highs or lows, a session's range, a break of
the last swing, gaps a move left, the halfway line of the last move, and the
course's whole sweep-break-retest model. Their names, plain descriptions and
video timestamps live in `bot/setups.py`; the price structure they read is
`bot/structure.py`, `bot/clock.py` and `bot/liquidity.py`. `session_sweep`
needs intraday bars, like `opening_range_break`.

These ten have voted in the composite since 19 November 2026, at the owner's
choice: each starts at a weight of 0.7, weighted by regime and by its measured
record and discounted inside its family like any other strategy. Before that
they were on trial, scored and measured with no say in the call, and calls
came from the other 34. The switch is `Strategy.votes`: set to False, it puts
a strategy on trial, and the pages then tag it "on trial". If the price
structure cannot be read on some history, the ten sit out and the call is the
one the other 34 make (`tests/test_liquidity_strategies.py` checks both).

The alerts follow the same levels (`bot/alerts.py`, the "Price action"
group): yesterday's high or low traded through, or taken and closed back
inside; the day's opening gap filling; a close past the last swing the
other way; a pullback into the newest gap; London's range taken between
08:30 and 12:00 New York time. Each message is a fact with a price and the
New York time, never a forecast.

### How it decides which to trust

Three layers, in order:

1. **Regime priors.** Market state is classified on two axes: trend strength
   from ADX, and volatility from the instrument's own bandwidth percentile.
   Strategies suited to the current regime start with more weight.

2. **Measured performance.** Every strategy is independently back-tested on the
   instrument in front of it, bucketed by regime. A strategy that lost money
   here is trusted less here, whatever its reputation. Weights are **shrunk**
   toward their prior in proportion to sample size, so a few lucky wins change
   almost nothing.

3. **Family discounting.** Ten trend strategies agreeing is one observation
   about trend, not ten confirmations. Agreement within a family is
   progressively discounted so a crowded family cannot drown out the rest.

---

## The calibration

For each historical bar the backtester computes the composite score using the
**same code** the live call uses, simulates a trade with the configured stop and
target, walks forward to see which came first, and charges round-trip cost.

Design choices that keep it honest:

- **Ambiguous bars count as losses.** If one bar's range contains both stop and
  target, the tick order is unknown, so it is scored as a stop-out.
- **Timed-out trades are marked to market**, not counted as full losses.
- **Samples are strided** so consecutive near-identical setups are not treated
  as independent evidence.
- **Wilson confidence intervals**, which stay sensible at small sample sizes.
- **A holdout check** on the most recent third. If the edge exists only in older
  data, the report says so.
- **Break-even is measured.** It is the hit rate at which the average win and
  the average loss of those same trades, after costs, cancel out:
  `loss / (win + loss)`. Not every losing trade loses the full stop, so
  `(1+c)/(1+R)`, which assumes it, asks for too much.

---

## The database

SQLite at `data/stockbot.db`, created on first run.

| Table | Holds |
|---|---|
| `runs` | Every analysis: price, regime, composite, action, levels, odds |
| `strategy_stats` | Measured performance per symbol, strategy and regime |
| `trades` | The journal: entry, stop, exit, realised R, P&L, and any trailing stop |
| `trade_exits` | Parts of a position closed while the rest stayed open |

Strategy statistics are **replaced** per instrument, not accumulated. Two runs over
overlapping history describe the same bars, and summing them would manufacture
confidence that was never earned.

---

## Paper trading workflow

This is how to test it on your paper account.

```
run.bat --symbol AAPL          analyse; the plan is recorded automatically
paper.bat take AAPL            open a trade from that exact plan
paper.bat check                mark to market, flags stop and target hits
paper.bat close 1 --price 329 --reason target
paper.bat close 1 --price 329 --qty 20   close part of it; the rest stays open
paper.bat trail 1 --pct 5      trail its stop 5% behind the best price since opening
paper.bat stats                expectancy, profit factor, max drawdown
```

A trailing stop (`--pct`, or `--atr` for a multiple of the ATR measured when it
is set) sits that far behind the best price since the trade opened, and only
ever tightens: for a long, the higher of the stop it opened with and the
highest price minus the trail. The journal, the portfolio, `check` and the
background stop notice all read it by the same rule. A trade closed in parts
counts once it is closed in full, at the R of the whole position, each part
weighted by its size; the parts are listed in the "Trade exits" export. After a
share split, a trade stays in the shares it was opened with; `close --today`
takes a price and size in today's shares and converts them.

On the website's Journal, a trade can carry the setup it came from, how you
felt and a pre-trade checklist, and a trade entered by hand can be given the
New York time you really entered (up to 30 days back). Once closed, a short
review keeps whether you followed your plan and what it taught you. **What your
journal says** (`/trades/breakdown`) splits closed trades after costs by setup,
New York session, time of the morning, weekday, feeling, plan followed, rules
broken and checklist, with a margin of error only from ten trades in a group;
it also gives green days, the break-even hit rate for your own average win and
loss, your best day's share of the profit, and whether a fixed or trailing loss
limit would have been hit. Settings holds your **daily rules**: a loss limit in
R, most trades a day, and no new trades after a New York time. When one says
stop, the journal asks before logging; a trade logged anyway is marked, so the
breakdown shows what breaking the rule cost. **See the trade** draws any
journal trade on its own prices (hourly or daily when its bars no longer reach
back). Two tools are open to anyone: a **size calculator** (`/tools/size`) for
shares, coins and futures contracts, with the break-even hit rate after costs
and the fixed-contract view, and a **survival check** (`/tools/survival`) that
reshuffles a hit rate and average win and loss, or your own 20+ closed trades,
to show what risking 0.5% to 10% a trade does over 100 trades. The tags,
review and rule columns are on the trades table and in the journal export.

`take` deliberately refuses plans the bot did not propose, plans marked WAIT
where price never reached the entry, and plans older than two hours. It records
the trade the bot actually recommended rather than a remembered version. Use
`paper.bat open` with explicit numbers to log anything else.

`check` flags a stop or target hit two ways: by scanning bars since entry, and
by comparing the live price directly. The second catches gaps and positions
opened while the market was closed.

Position sizing follows `risk_per_trade_pct` and is capped by
`max_position_pct`, so the cap often binds on small accounts. The reported risk
is always the real one.

**Do not judge anything on fewer than about thirty closed trades.** The tool
says so too. Below that you cannot separate skill from noise.

---

## Options

```
run.bat [image] [options]

  image                 path to a chart screenshot
  --clipboard           read the screenshot from the clipboard
  --symbol, -s TICKER   analyse a ticker directly, no image
  --interval, -i IV     1m, 2m, 5m, 15m, 30m, 1h, 1d
  --account N           account size for position sizing
  --risk N              percent of account risked per trade
  --allow-shorts        permit bearish plans as well as long
  --no-news             skip sentiment, technicals only
  --no-llm              never call Claude for sentiment
  --no-learn            fixed weights, skip per-strategy measurement
  --no-db               do not record this run
  --all-signals         print all 44 strategies, not just top contributors
  --json PATH           write the full analysis as JSON
  --config PATH         use a different config.json
```

```
paper.bat <command> [options]

  take SYMBOL           open from the bot's last plan for that symbol
  open SYMBOL           open with explicit --entry --stop --target --qty
  close ID --price P    close a position, or part of it with --qty
  trail ID --pct P      trail its stop behind the best price (or --atr N)
  delete ID             remove a trade entirely
  check                 mark open positions to market
  list [--all]          show trades
  stats                 realised performance
  leaderboard           rank strategies by measured expectancy
  runs                  recent analyses
  db                    database size and location
```

## Configuration

`config.json`. The settings that matter most:

| Key | Meaning |
|---|---|
| `cost_bps_equity` / `cost_bps_crypto` | Round-trip cost in basis points. Set to your broker. |
| `risk_per_trade_pct` | Percent of account risked per trade |
| `max_position_pct` | Cap on capital in one position |
| `stop_atr_multiple` | Stop distance in ATR units |
| `target_atr_multiple` | First target, sets reward:risk |
| `require_positive_expectancy` | If true, a negative measured record vetoes the trade |
| `strategy_weights` | Per-strategy base weights, before regime and learned adjustment |
| `alert_check_minutes` | Minutes between background alert checks, or 0 to switch them off |

Two environment variables move where things are stored, which is how you keep a
paper journal and a live one side by side without editing anything:

| Variable | Effect |
|---|---|
| `STOCKBOT_DB` | Use this database file instead of `data/stockbot.db` |
| `STOCKBOT_CONFIG` | Use this settings file instead of `config.json` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Turn on Google sign-in. Unset means no login at all |
| `GOOGLE_ALLOWED_DOMAINS` | Restrict sign-in to certain email domains |
| `STOCKBOT_SECRET_KEY` | Signs session cookies. Generated into `data/secret.key` if unset |

---

## Finding something to look at

The Find page answers the question people actually arrive with: *out of
everything, what should I look at today?* Pick stocks, crypto or both, and
whether you hold for days, weeks or months. It runs the whole back-test over up
to 150 stocks, 28 coins, or 178 instruments for both, and ranks by the measured
edge after costs.

Two lists, and the second is why the first can stay honest:

**Ready now** — a setup is firing *and* the approach has made money after costs
on that instrument's own history.

**Worth watching** — it has paid off before and nothing is happening today. The
action offered is to watch it, not to buy it.

Measuring that many typically finds a handful with a positive edge and
one or two with a setup actually firing, because an edge measured over years
says nothing about today. Without the second list the page would be empty
almost every day: honest, and useless.

**Months is not a copy of Weeks.** Holding something for months is a bet on the
business rather than the pattern, so that option reads the filings as well as
the chart. Anything whose accounts do not hold up moves to a list that names
the measure that failed, and a coin is never kept on that basis because it
files no accounts at all.

**It will not pad the list.** Every screener on the internet always has ten
rows, because ten rows feel like the page worked. That is how people end up
trading the least bad of a bad batch.

---

## Where it refuses to answer

A wrong answer delivered confidently costs more than no answer. These are the
places the app declines rather than producing a number that would look fine and
be wrong.

**Mixed currencies.** A foreign private issuer files in its home currency.
Alibaba's filings come back in yuan and dollars, Toyota's in yen and dollars.
Dividing a market value quoted in dollars by liabilities reported in yen gives
a perfectly plausible Altman score that is wrong by the exchange rate, and the
filing contains no rate to correct it with. Every model refuses on those, and
names the currencies it found.

**Models applied outside their domain.** No scores for a bank or an insurer:
Altman's coefficients were fitted on manufacturers, and a balance sheet that is
mostly other people's money by design reads as distressed however healthy it is.
The cash flow and margin tests of the other three do not fit one either.

**Partial evidence.** A Piotroski score computed from seven available tests
says "6 of 7" and names the two it could not measure, rather than grading out
of nine and looking worse than the company is.

**Figures the reasoning could not source.** The language model is given the
gathered evidence and told to use nothing else. That instruction is then
checked: every figure in its answer is matched back against the evidence, and
anything with no relative there is listed on the page as unverified. Rounding
is not treated as invention, years are not treated as measurements, and a
fraction in the evidence matches a percentage in the prose. When the check
passes, the page says how many figures it verified.

**A company that re-registered.** Exxon Mobil's history sits under a
predecessor CIK, so its new one holds two quarterly filings and nothing else.
That is reported as what it is rather than rendering an empty table.

---

## What the published models say

Four models run against the filed figures on every research page. None was
invented here, and that is the point: each was published, tested against
decades of filings, and can be looked up.

| Model | What it asks |
|---|---|
| **Piotroski F-Score** | How many of nine measures of financial strength improved. Piotroski (2000) found the high scorers among cheap stocks went on to beat the low scorers by a wide margin |
| **Altman Z-Score** | How far the balance sheet is from financial distress. From 1968 and still the standard first check |
| **Beneish M-Score** | Eight ratios that moved together in companies later found to have manipulated earnings. A smoke detector, not a verdict |
| **Accruals ratio** | How much of the profit arrived as cash. Sloan (1996): earnings not backed by cash tend to reverse |

Three things they deliberately will not do:

**Score a bank.** Altman's coefficients were fitted on manufacturers, and
a balance sheet that is mostly other people's money by design reads as
distressed however healthy it is. The other three models' cash flow, debt and
margin tests do not fit a lender either. A number there would mislead, so none
is given.

**Rescale a partial score.** If only seven of the nine Piotroski tests can be
measured, it says "6 of 7" and names what was missing, rather than quietly
grading out of nine.

**Accuse anyone.** A Beneish score above the line says the ratios warrant
reading the filing. Fast-growing honest companies trip it as readily as
dishonest ones.

---

## What it warns you about before the trade

Three checks run on the analysis page that most tools only do afterwards, if at
all.

**Earnings inside the holding window.** A ten-bar daily plan expects to be
finished in about a fortnight. If the company reports in three days, that is
not the same trade: earnings gap price to wherever the new information puts it,
and a stop does not survive a gap. The warning fires only when the date falls
inside the window, because a warning that fires every time teaches people to
skip it.

**What a gap would actually cost.** The stop states what you intend to lose.
Alongside it the plan shows what a move the size of that instrument's own worst
measured day would cost, which for Bitcoin is routinely three times the
budgeted risk and for an index fund about 1.2 times.

**Whether it is really a new position.** Before you add a fifth name, the page
measures it against what is already open. Five positions correlated at 0.9 are
one position held five times, and they fall together on the day that matters.
A genuine diversifier produces no warning; another semiconductor does.

---

## Alerts actually check

Background checking runs inside the server process on the interval set in
settings, so an alert is evaluated whether or not you are looking at the page,
and anything that fires appears on whatever page you are on.

It does not ask for a price that cannot have changed. A stock's alerts and
open positions' stops wait while its market is closed, from an hour after the
close, when the day's last prices are in, until the next open: nights,
weekends and NYSE holidays for a US listing, worked out by rule
(`bot/sessions.py`), and the hours its own market data gives for a listing
elsewhere. Coins, mutual funds, indices and anything whose hours are not known
are checked every time, and so is an alert that is new, edited, re-armed or
whose last check failed. "Check them all now" and the routines check
everything. The usage page counts the checks skipped.

**Nothing runs when the window is closed.** The interface says so in those
words rather than letting you believe a level is being watched overnight.
Making that true means hosting the app somewhere that stays up, which this
deliberately does not pretend to be.

---

## Judging your own theses

Saving a thesis is journalling. Closing one out is a record. The Monitor page
keeps score of the closed ones, and the outcome vocabulary is four options
rather than two:

| Outcome | Meaning |
|---|---|
| `right` | The reasoning held and the price followed |
| `wrong` | Worth naming which point failed |
| `luck` | Right for the wrong reason: the price moved, the reasoning did not hold |
| `undecided` | Too little happened either way to judge |

Splitting `right` from `luck` is the point. A two-way right/wrong tally quietly
rewards being lucky, and anyone reading it concludes their reasoning works when
only their luck did. When the luck column outgrows the right column, the
scoreboard says so in those words.

The outcome pre-selected when you close a thesis is whatever the price
supports, computed from the move since you saved it. You can pick a different
one, but you have to do it deliberately, because someone closing a thesis three
months later reliably remembers having been right.

---

## Accounts

**On your own machine there is no login.** Leave the Google credentials unset
and the app opens straight into your own data, exactly as before. That is the
default and nothing needs configuring to get it.

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` and sign-in switches on by
itself. That is for putting this somewhere other people can reach. See
`.env.example` for the steps to get the credentials from Google.

### What is yours and what is shared

| | |
|---|---|
| **Yours alone** | Trades, watchlist, alerts, saved theses, analysis history |
| **Shared** | The measured strategy record |

The split is deliberate. A strategy measurement says how a setup behaved on an
instrument, which is a fact about the market rather than about the person who
happened to run the analysis. Keeping it shared means every analysis anyone
runs improves the record for everyone, which is the one place where more users
make the product smarter rather than merely larger. Deleting an account removes
everything personal and leaves that record, because it was never one person's
to take.

### How the sign-in works

The authorization code flow with PKCE, exchanged server side. No token is
verified by hand: the profile is read from Google's userinfo endpoint over TLS,
so trust comes from the connection to Google rather than from cryptography
written here. Session tokens are opaque and stored, so signing out ends the
session on the server rather than relying on a browser to discard its copy.

Sign in with Apple is the exception, because Apple has no userinfo endpoint
and inside the iPhone app its token comes from the phone. So `bot/apple.py`
checks the token by hand, as narrowly as it can: RS256 only, Apple's
published keys only, and the issuer, audience, expiry and the nonce this
server handed out. Inside the app, Google and Apple's web sign-in run in the
system browser sheet, which hands the result back to the app as a one-time
code that only works with a secret the app kept (`static/signin-app.js`).
Deleting an account asks Apple to end its sign-in. DEPLOY.md has the setup.

Credentials are read from the environment and never from `config.json`, because
`config.json` is a file people edit, copy, and paste into support requests.

---

## Tests

```
.runtime\python\python.exe tests\run_all.py             every suite
.runtime\python\python.exe tests\run_all.py --offline   only those needing no network
.runtime\python\python.exe tests\test_options.py        one suite on its own
```

Each suite is an ordinary script that exits non-zero when something fails, so it
runs on its own as readily as under the runner. They cover indicator maths
against published values, Black-Scholes against textbook figures, put-call
parity, the lexicon's handling of negation, database invariants, every template
compiling, every link resolving, and a full analysis end to end.

Every suite gets a private database and settings file through `tests/harness.py`.
Nothing in the suite can read or write your real journal, and no suite depends
on the order it runs in.

---

## Data sources

| Source | Used for | Notes |
|---|---|---|
| Yahoo Finance charts | OHLCV, symbol resolution, news | Free, no key. Equities, ETFs, crypto. |
| Yahoo Finance quoteSummary | Ratios, profile, balance sheet, cash flow | Free, but needs a cookie and crumb handshake, which `bot/yahoo.py` performs and refreshes |
| Yahoo Finance options | Chains, open interest, implied volatility | Same handshake. Greeks are computed here, not taken on trust |
| SEC EDGAR | Filed financials (XBRL company facts), filing history | Free, no key, requires a real User-Agent and stays inside the 10 requests/second limit |
| Google News RSS | Headlines | Free, no key |
| StockTwits | Retail trader posts | Free public endpoint |

Anything sourced from a filing is labelled as such and linked back to the
document on sec.gov, so a number can be checked rather than believed. Where
Yahoo and the filings disagree, both are shown.

**On "Twitter posts":** the X/Twitter API no longer has a usable free tier, so
StockTwits stands in. It is where retail traders actually post, and many posts
carry explicit Bullish/Bearish tags from the author, which is better evidence
than inferring tone from text. If you have paid X access, add it as another
source in `gather_social` in `bot/sentiment.py`.

---

## Limitations

- **The backtest assumes stops and targets fill at the exact level.** Real fills
  gap through stops, especially on news and at the open. The trade plan now
  states this in cash: alongside what the stop budgets, it shows what a move the
  size of that instrument's worst measured day would actually cost. For Bitcoin
  that is routinely three times the budgeted risk; for an index fund closer to
  1.2 times. The backtest itself still assumes the clean fill.
- **Cost is modelled as a flat spread.** No size-dependent slippage, no partial
  fills.
- **Sixty days of 5-minute history is one market regime.** An edge measured
  there may not survive a change in conditions.
- **The screenshot only identifies the instrument.** All analysis runs on live
  data, because a screenshot may already be stale.
- **Sentiment is shallow.** Headlines and short posts, not filings or earnings
  transcripts.
- **Learned weights are fitted on the same history they are measured on.** The
  holdout check mitigates this but does not eliminate it. Forward paper results
  are the real test.
- **No broker integration.** It never places an order.

## The look

Warm editorial: a cream ground, terracotta accent, serif display over humanist
sans, flat surfaces where depth comes from tone shifts and hairlines rather than
shadows. Nearly every trading product is a cold dark terminal, so a calm warm
one is a real difference rather than a variation.

Three details do most of the work:

- **Every figure is monospaced with tabular numerals**, so columns of prices and
  R multiples line up and stay scannable.
- **Semantic colours are warm.** Gains are olive `#68773b`, losses rust
  `#a53e2a`. A neon green on cream would look violently wrong.
- **Contrast was solved, not copied.** The published warm tokens are tuned for
  large display type and fail AA as small text on cream: muted grey lands at
  3.34:1 and terracotta at 3.51:1. Each was walked down its own lightness ramp
  until it cleared 4.5:1, keeping the hue and gaining legibility.

There is a warm dark theme too, which follows your system setting. The
decisions are recorded in `design-system/stock-bot/MASTER.md`.

---

## Putting it on the internet

Double-clicked, the web app runs on your machine. `http://127.0.0.1:8000` only
works there, which is deliberate: your API key, your trades and your positions
stay local.

The same code serves [orenth.app](https://orenth.app), and DEPLOY.md is the
step-by-step guide to putting it online.

**Hosting.** `serve.py` runs it under waitress, a production server, in the
container the `Dockerfile` builds, behind the host's own proxy.

**Concurrency.** One analysis pins a CPU core for seconds, and the cache is
per-process and in memory, so this is one instance on one machine. Many more
simultaneous users would need a job queue and a shared cache.

**Multi-user data.** With sign-in switched on, each person has their own
trades, watchlist, alerts and theses (see Accounts above), in one SQLite file
on a persistent disk.

There is also licensing to check before running your own: publishing trading
signals for money is regulated in many countries, and the rules differ by
where you and your users are.

---

## Project layout

```
Stock Bot.bat       double-click this: starts the web server
web.py              the Flask web app
templates/          web pages, including the settings screen
static/style.css    warm editorial design system
design-system/      the recorded design decisions
menu.py             terminal menu
analyze.py          analysis CLI
paper.py            paper trading journal CLI
run.bat / paper.bat launchers using the bundled runtime
setup.bat           rebuilds the bundled runtime
config.json         risk, costs, thresholds, strategy weights
data/stockbot.db    created on first run
serve.py            production server (waitress), used by the container
backup.py           consistent snapshots via SQLite's backup API, then verified
preflight.py        checks a live deployment before anyone else sees it
Dockerfile          the deployed image
DEPLOY.md           putting it online, step by step
tests/              every suite, runnable with tests/run_all.py
bot/
  engine.py         the analysis pipeline, shared by web and CLI
  market.py         data fetching, symbol resolution, cleaning
  indicators.py     30+ indicators in pure NumPy
  levels.py         swing pivots, support/resistance, session ranges
  strategies.py     44 strategies, families, regime classification
  learning.py       per-strategy measurement and weight learning
  calibrate.py      walk-forward backtest and probability calibration
  database.py       SQLite persistence
  sentiment.py      news and social scoring
  decision.py       entry, stop, targets, sizing, gap risk, the final call
  report.py         terminal output and JSON
  vision.py         screenshot reading
  config.py         configuration loading

  accounts.py       who owns what, sessions, and the shared/personal split
  auth.py           Google sign-in: authorization code flow with PKCE
  quality.py        Piotroski, Altman, Beneish and accruals, from filed figures
  charts.py         price and filed-financial charts as inline SVG
  monitoring.py     optional error reporting, with personal data stripped
  offsite.py        each backup copied to a storage bucket, checked, history kept
  usage.py          what people actually do, from rows already written
  ideas.py          the Find page: scan a list, rank by measured edge
  plain.py          the same answer in words a beginner can act on
  upstream.py       caching, single-flight and backoff for the shared sources
  yahoo.py          authenticated Yahoo session: cookie, crumb, retry
  catalysts.py      earnings and dividend dates, and whether they fall in range
  scheduler.py      the background loop that actually checks alerts
  sessions.py       market hours, so the loop leaves a closed market alone
  export.py         your journal and measurements as CSV or JSON
  sec.py            EDGAR filings and XBRL company facts
  fundamentals.py   ratios from Yahoo, reconciled against what was filed
  valuation.py      discounted cash flow, reverse DCF, multiples
  options.py        Black-Scholes, Greeks, implied volatility, chain quality
  risk.py           volatility, drawdown, value at risk, beta, correlation
  portfolio.py      exposure, concentration, sector weights, shock tests
  screener.py       two-stage screening, shallow then deep
  thesis.py         the bull and bear cases, with evidence and falsifiers
  alerts.py         thirteen alert conditions and the checker
  reasoning.py      LLM reasoning over gathered facts only
  workflows.py      the four automated routines

  structure.py      swing highs and lows, breaks of structure, sweeps, gaps, the 50% line
  clock.py          New York time, trading days, sessions, yesterday's and the week's levels
  liquidity.py      the course's chain: taken, broken, pulled back, turned
  bias.py           the daily lean, read from hourly and 4-hour bars
  setups.py         every setup, claim and Learn topic from the video, named once
  labstore.py       where the lab's results are kept, and the one verdict rule
  setuplab.py       the Setup lab: every setup on intraday prices, against random entries
  claims.py         the video's claims, each against what happens anyway
  levelview.py      the analysis page's price-structure panel and daily lean
  journal.py        journal tags, breakdowns, daily rules, size and survival
  learn.py          the Learn pages' words, and what they read from the lab
```

---

## This is not financial advice

This is analysis software. Every number it prints is measured from historical
price data and can stop describing the future at any moment. Day trading loses
money for the large majority of retail participants. The bot's own measurements
agree with that. Never risk capital you cannot afford to lose.
