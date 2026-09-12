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
    "strong_conviction": 0.45,      # above this, a market entry is allowed
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


def describe_horizon(interval: str, bars: int) -> str:
    """Say what a horizon means in wall-clock terms, for the report."""
    minutes = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
               "60m": 60, "1h": 60}.get((interval or "").lower())
    if minutes is None:
        return "%d trading days" % bars
    total = bars * minutes
    if total < 60:
        return "%d minutes" % total
    if total < 60 * 7:
        return "%.1f hours" % (total / 60.0)
    return "%.1f trading days" % (total / (60 * 6.5))


def cost_bps_for(asset_class: str, cfg: Dict[str, Any]) -> float:
    """Round-trip cost in basis points for this kind of instrument."""
    return float({
        "equity": cfg.get("cost_bps_equity", 2.0),
        "crypto": cfg.get("cost_bps_crypto", 20.0),
    }.get(asset_class, cfg.get("cost_bps_other", 5.0)))


def load(path: str | None = None) -> Dict[str, Any]:
    """Merge defaults, config.json and environment overrides."""
    cfg = json.loads(json.dumps(DEFAULTS))          # deep copy
    cfg_path = path or default_config_path()

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            weights = user.pop("strategy_weights", None)
            cfg.update(user)
            if isinstance(weights, dict):
                cfg["strategy_weights"].update(weights)
        except (OSError, json.JSONDecodeError) as exc:
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


def validate(cfg: Dict[str, Any]) -> None:
    """Raise if any setting would produce nonsense downstream."""
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
        except (TypeError, ValueError):
            raise RuntimeError("Holding window must be a number between 3 and "
                               "400 bars, or the word auto.")
    for key in ("min_conviction", "strong_conviction"):
        if not (0 <= cfg[key] <= 1):
            raise RuntimeError("Conviction thresholds must be between 0 and 1.")
    if cfg["min_conviction"] > cfg["strong_conviction"]:
        raise RuntimeError("The market-entry threshold cannot be below the "
                           "minimum conviction threshold.")
    for key in ("cost_bps_equity", "cost_bps_crypto", "cost_bps_other"):
        if cfg.get(key, 0) < 0:
            raise RuntimeError("Trading costs cannot be negative.")


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
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing.update({k: v for k, v in cfg.items() if not k.startswith("_")})
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, cfg_path)          # atomic, so a crash cannot truncate it
    return cfg_path
