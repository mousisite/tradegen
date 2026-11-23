"""Configuration loading.

Defaults live here so the bot runs with no config file at all. Anything in
`config.json` next to the project root overrides them, and a few environment
variables override that.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    # --- account and risk -------------------------------------------------
    "account_size": 10000.0,
    "risk_per_trade_pct": 1.0,      # percent of account risked per trade
    "max_position_pct": 25.0,       # cap on capital deployed in one position

    # --- trade construction ----------------------------------------------
    # Daily by default: it is the only interval measured with positive
    # expectancy once real trading costs are charged.
    "interval": "1d",
    "stop_atr_multiple": 1.5,       # stop distance in ATR units
    "target_atr_multiple": 2.25,    # first target in ATR units, sets R:R
    "runner_atr_multiple": 3.5,     # second target for a partial runner
    # "auto" scales the holding window to the interval. A fixed 24 bars means
    # two hours on 5-minute bars but five weeks on daily ones, which silently
    # turns a day trade into a swing trade and measures the wrong thing.
    "horizon_bars": "auto",
    # Minutes between background alert checks while the server is up.
    # 0 switches background checking off entirely.
    "alert_check_minutes": 5,

    # --- decision thresholds ---------------------------------------------
    "min_conviction": 0.20,         # below this, no trade is proposed
    "strong_conviction": 0.45,      # from here a wait's reason calls it strong
    "max_extension_atr": 2.0,       # ATR above VWAP before entries must wait
    "allow_shorts": False,
    "require_positive_expectancy": True,

    # --- sentiment --------------------------------------------------------
    "sentiment_weight": 0.25,       # share of final conviction from news/social
    "use_llm_sentiment": True,

    # --- trading costs ----------------------------------------------------
    # Round-trip cost in basis points (1bp = 0.01%). Charged to every simulated
    # trade. Defaults assume a commission-free equity broker paying only the
    # spread, and a crypto exchange charging taker fees on both sides.
    # Set these to match your actual broker; they change the verdict a lot.
    "cost_bps_equity": 2.0,
    "cost_bps_crypto": 20.0,
    "cost_bps_other": 5.0,
    # Futures (a symbol ending =F) pay their own: one tick of slippage and a
    # commission of about $5 is 0.6bp on ES at 6,000, 0.25bp on NQ and 0.8bp
    # on the micro contracts. Charged the 5bp "other" rate, they paid about
    # eight times what a futures round trip costs.
    "cost_bps_futures": 1.0,

    # --- daily rules for the journal (0 or "" switches each off) ------------
    "daily_loss_limit_r": 0.0,      # stop for the day after losing this many R
    "max_trades_per_day": 0,        # most trades a day
    "last_entry_time": "",          # no new trades after this New York time, "HH:MM"
    "calm_candles": False,          # grey and ink candles instead of green and red

    # --- calibration ------------------------------------------------------
    "calibration_stride": 3,
    "min_bucket_samples": 25,

    # --- strategy weights -------------------------------------------------
    # Left empty on purpose. The per-strategy base weights live in
    # strategies.BASE_WEIGHTS, which is generated from the registry, so there
    # is exactly one source of truth and a renamed strategy cannot leave a dead
    # key here that silently does nothing. Add an entry only to override one.
    "strategy_weights": {},
}

_ENV_MAP = {
    "STOCKBOT_ACCOUNT_SIZE": ("account_size", float),
    "STOCKBOT_RISK_PCT": ("risk_per_trade_pct", float),
    "STOCKBOT_INTERVAL": ("interval", str),
}


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path() -> str:
    """Where settings live unless a caller says otherwise.

    STOCKBOT_CONFIG overrides it, so a second set of settings can be kept
    alongside the first without editing anything.
    """
    override = os.environ.get("STOCKBOT_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(project_root(), "config.json")


# How long a setup is given to work, per interval. Chosen so the real-world
# holding window stays in the same ballpark rather than ballooning with the
# bar size: roughly a couple of hours intraday, a couple of weeks on daily.
_HORIZON = {"1m": 30, "2m": 30, "5m": 24, "15m": 24, "30m": 16,
            "60m": 12, "1h": 12, "1d": 10}


def horizon_for(interval: str, cfg: Dict[str, Any]) -> int:
    """Bars a setup is given to work on this interval.

    A number in config overrides the per-interval default, so anyone who wants
    a fixed window can still have one.
    """
    setting = cfg.get("horizon_bars", "auto")
    if isinstance(setting, (int, float)) and not isinstance(setting, bool):
        return max(3, int(setting))
    if isinstance(setting, str) and setting.strip().isdigit():
        return max(3, int(setting.strip()))
    return _HORIZON.get((interval or "").lower(), 20)


def describe_horizon(interval: str, bars: int, asset_class: str = "",
                     symbol: str = "") -> str:
    """Say what a horizon means in wall-clock terms, for the report.

    How long a day of trading is depends on the instrument: a coin trades
    round the clock every day, a future 23 hours and a currency pair 24, and
    anything else is counted in the 6.5 hours of a stock's session.
    """
    minutes = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
               "60m": 60, "1h": 60}.get((interval or "").lower())
    ticker = (symbol or "").strip().upper()
    coin = asset_class == "crypto"
    day = "day" if coin else "trading day"
    if minutes is None:
        return "%d %s%s" % (bars, day, "" if bars == 1 else "s")
    hours = (24.0 if coin or ticker.endswith("=X")
             else 23.0 if ticker.endswith("=F") else 6.5)
    total = bars * minutes
    if total < 60:
        return "%d minute%s" % (total, "" if total == 1 else "s")
    # Seven hours of a stock is still said in hours.
    if total < 60 * max(hours, 7.0):
        return "%.1f hours" % (total / 60.0)
    return "%.1f %ss" % (total / (60 * hours), day)


def cost_bps_for(asset_class: str, cfg: Dict[str, Any]) -> float:
    """Round-trip cost in basis points for this kind of instrument."""
    return float({
        "equity": cfg.get("cost_bps_equity", 2.0),
        "crypto": cfg.get("cost_bps_crypto", 20.0),
    }.get(asset_class, cfg.get("cost_bps_other", 5.0)))


def cost_bps_for_symbol(symbol: str, asset_class: str, cfg: Dict[str, Any]) -> float:
    """Round-trip cost in basis points for one instrument.

    A futures contract (Yahoo's symbol ends in =F) has its own cost; the data
    source files futures under "other", whose rate is several times what a
    future costs to trade. Anything else is costed by its asset class.
    """
    if (symbol or "").strip().upper().endswith("=F"):
        return float(cfg.get("cost_bps_futures", DEFAULTS["cost_bps_futures"]))
    return cost_bps_for(asset_class, cfg)


def load(path: str | None = None) -> Dict[str, Any]:
    """Merge defaults, config.json and environment overrides."""
    cfg = json.loads(json.dumps(DEFAULTS))          # deep copy
    cfg_path = path or default_config_path()

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if not isinstance(user, dict):
                raise RuntimeError("could not read %s: it must hold settings by name, "
                                   "between { and }." % cfg_path)
            weights = user.pop("strategy_weights", None)
            cfg.update(user)
            if isinstance(weights, dict):
                cfg["strategy_weights"].update(weights)
        except (OSError, ValueError) as exc:
            raise RuntimeError("could not read %s: %s" % (cfg_path, exc))

    for env_key, (cfg_key, cast) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw:
            try:
                cfg[cfg_key] = cast(raw)
            except ValueError:
                pass

    validate(cfg)
    return cfg


# The settings that must be ordinary numbers, and what each is called. One
# kept as text, or as NaN or infinity, passes a comparison or breaks it.
_NUMBERS = {
    "account_size": "Account size", "risk_per_trade_pct": "Risk per trade",
    "max_position_pct": "Largest position", "stop_atr_multiple": "Stop distance",
    "target_atr_multiple": "First target", "min_conviction": "Minimum conviction",
    # Not on the Settings page: only the file sets them.
    "runner_atr_multiple": "Second target", "strong_conviction": "Strong conviction",
    "max_extension_atr": "Furthest from VWAP before a wait",
    "sentiment_weight": "Share of conviction from news",
    "calibration_stride": "Bars between calibration samples",
    "min_bucket_samples": "Fewest past setups to measure from",
}


def validate(cfg: Dict[str, Any]) -> None:
    """Raise RuntimeError if any setting would produce nonsense downstream."""
    for key, label in _NUMBERS.items():
        if not _is_number(cfg.get(key, DEFAULTS[key])):
            raise RuntimeError("%s must be an ordinary number." % label)
    if not isinstance(cfg.get("interval", "1d"), str) \
            or cfg.get("interval", "1d").lower() not in _HORIZON:
        raise RuntimeError("The default interval must be one of: %s." % ", ".join(_HORIZON))
    if cfg.get("max_extension_atr", 2.0) <= 0:
        raise RuntimeError("%s must be more than zero." % _NUMBERS["max_extension_atr"])
    if not (0 <= cfg.get("sentiment_weight", 0.25) <= 1):
        raise RuntimeError("%s must be between 0 and 1." % _NUMBERS["sentiment_weight"])
    for key in ("calibration_stride", "min_bucket_samples"):
        if cfg.get(key, 1) < 1:
            raise RuntimeError("%s must be 1 or more." % _NUMBERS[key])
    if not (0 < cfg["risk_per_trade_pct"] <= 10):
        raise RuntimeError("Risk per trade must be between 0 and 10 percent.")
    if not (0 < cfg["max_position_pct"] <= 100):
        raise RuntimeError("Largest position must be between 0 and 100 percent.")
    if cfg["account_size"] <= 0:
        raise RuntimeError("Account size must be greater than zero.")
    if cfg["stop_atr_multiple"] <= 0 or cfg["target_atr_multiple"] <= 0:
        raise RuntimeError("Stop and target distances must be positive.")
    if cfg["target_atr_multiple"] <= cfg["stop_atr_multiple"] * 0.2:
        raise RuntimeError("The target is far too close to the stop to be tradable.")
    minutes = cfg.get("alert_check_minutes", 5)
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        raise RuntimeError("Alert check interval must be a number of minutes.")
    if minutes and not (1 <= minutes <= 240):
        raise RuntimeError("Alert checks must be between 1 and 240 minutes "
                           "apart, or 0 to switch them off.")

    horizon = cfg.get("horizon_bars", "auto")
    if not (isinstance(horizon, str) and horizon.strip().lower() == "auto"):
        try:
            if not (3 <= int(horizon) <= 400):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError("Holding window must be a number between 3 and "
                               "400 bars, or the word auto.")
    for key in ("min_conviction", "strong_conviction"):
        if not (0 <= cfg[key] <= 1):
            raise RuntimeError("Conviction thresholds must be between 0 and 1.")
    if cfg["min_conviction"] > cfg["strong_conviction"]:
        raise RuntimeError("Strong conviction cannot be below the minimum conviction.")
    for key in ("cost_bps_equity", "cost_bps_crypto", "cost_bps_other", "cost_bps_futures"):
        value = cfg.get(key, 0)
        if not _is_number(value):
            raise RuntimeError("Trading costs must be numbers.")
        if value < 0:
            raise RuntimeError("Trading costs cannot be negative.")

    limit = cfg.get("daily_loss_limit_r", 0.0)
    if not _is_number(limit) or not (0 <= limit <= 20):
        raise RuntimeError("The daily loss limit must be a number from 0 to 20 times "
                           "your risk per trade, or 0 to switch it off.")
    most = cfg.get("max_trades_per_day", 0)
    if not _is_number(most) or float(most) != int(most) or not (0 <= most <= 100):
        raise RuntimeError("The most trades a day must be a whole number from 0 to 100, "
                           "or 0 to switch it off.")
    last = cfg.get("last_entry_time", "")
    if not isinstance(last, str) or (last.strip() and clock_minutes(last) is None):
        raise RuntimeError("The time after which you take no new trades must be a "
                           "time like 10:30, or left empty to switch it off.")
    if not isinstance(cfg.get("calm_candles", False), bool):
        raise RuntimeError("Calm candle colours must be either on or off.")


def lift_strong(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Keep "strong" at or above the least conviction a call is made at.

    The Settings page no longer asks for it, so it must never be why a
    minimum typed there is refused.
    """
    least, strong = cfg.get("min_conviction"), cfg.get("strong_conviction")
    if _is_number(least) and _is_number(strong) and least > strong:
        cfg["strong_conviction"] = least
    return cfg


def with_personal(base: Dict[str, Any], own: Dict[str, Any]) -> Dict[str, Any]:
    """Settings with one person's saved ones on top, leaving out any that
    cannot work.

    They are checked when saved, but some were saved before they were, and a
    negative account or a NaN went straight into position sizes and broke
    that person's pages.
    """
    merged = lift_strong(dict(base, **own))
    try:
        validate(merged)
        return merged
    except RuntimeError:
        pass
    out, waiting = dict(base), dict(own)
    while waiting:
        # Again for those left over: a stop and a target can each need the other.
        kept = []
        for key, value in waiting.items():
            trial = lift_strong(dict(out, **{key: value}))
            try:
                validate(trial)
            except RuntimeError:
                continue
            out = trial
            kept.append(key)
        if not kept:
            break
        for key in kept:
            del waiting[key]
    return out


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def clock_minutes(text: str):
    """'10:30' or '9:30' as minutes after midnight, or None when it is not a
    time. The daily rule last_entry_time is read with this."""
    hours, sep, mins = (text or "").strip().partition(":")
    if not sep or not hours.isdigit() or not mins.isdigit() or len(mins) != 2 \
            or len(hours) > 2:
        return None
    h, m = int(hours), int(mins)
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def save(cfg: Dict[str, Any], path: str | None = None) -> str:
    """Write settings to config.json, keeping any keys we did not manage.

    Reads the file first and merges, so hand-edited keys the UI does not expose
    survive a save from the web form.
    """
    validate(cfg)
    cfg_path = path or default_config_path()

    existing: Dict[str, Any] = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

    existing.update({k: v for k, v in cfg.items() if not k.startswith("_")})
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, cfg_path)          # atomic, so a crash cannot truncate it
    return cfg_path
