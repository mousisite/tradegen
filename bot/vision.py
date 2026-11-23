"""Read a chart screenshot and work out what instrument it shows.

The bot's entry point is an image: a TradingView tab, a broker app, a Binance
screen. Claude's vision model reads the ticker, timeframe and any visible price
off it. Everything after that step uses live market data rather than the pixels,
because a screenshot is a snapshot and may already be stale by the time it is
analysed. The image tells us *what* to analyse, not *what the market is doing*.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_MODEL = "claude-sonnet-5"
MAX_EDGE = 1568           # beyond this the vision encoder gains nothing
# The most pixels an image may claim before it is decoded. A 6K screen is
# about 20 million. A flat image compresses so well that a file of a few
# kilobytes can claim a billion, and decoding that takes gigabytes.
MAX_PIXELS = 25_000_000


class VisionError(RuntimeError):
    """Raised when a screenshot cannot be read."""


@dataclass
class ChartRead:
    """What the model could see in the screenshot."""
    symbol: Optional[str]
    asset_class: Optional[str] = None
    timeframe: Optional[str] = None
    price_seen: Optional[float] = None
    exchange: Optional[str] = None
    confidence: float = 0.0
    observations: List[str] = field(default_factory=list)
    raw: str = ""


def upright(img):
    """The picture turned the way it was taken, for a photo that says it lies
    on its side. One whose note cannot be read is left as it is."""
    from PIL import ImageOps
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _load_image_bytes(path):
    """Load and downscale an image, returning (bytes, media_type).

    path is a file path, or an open binary file such as io.BytesIO. A picture
    held in memory that is already the PNG wanted is passed on as it is: the
    web upload has just made it, and making it again only costs time.
    """
    from PIL import Image

    try:
        img = Image.open(path)
    except Exception as exc:
        raise VisionError("could not open image %s: %s" % (path, exc))

    # Opening reads only the header, so this is checked before any pixel
    # is decoded.
    w, h = img.size
    if w * h > MAX_PIXELS:
        raise VisionError("That image is too large to read. Crop it to just "
                          "the chart and try again.")
    if (isinstance(path, io.BytesIO) and img.format == "PNG"
            and img.mode in ("RGB", "L") and max(w, h) <= MAX_EDGE):
        return path.getvalue(), "image/png"
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(w, h) > MAX_EDGE:
        scale = MAX_EDGE / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    img = upright(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def grab_clipboard(save_to: str) -> str:
    """Save the clipboard image to disk and return its path.

    Lets the workflow be Win+Shift+S then run the bot, which matters because an
    intraday setup can expire while you are hunting for a file path.
    """
    from PIL import ImageGrab

    img = ImageGrab.grabclipboard()
    if img is None or isinstance(img, list):
        raise VisionError("no image found on the clipboard")
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(save_to, format="PNG")
    return save_to


_PROMPT = """You are looking at a screenshot of a financial chart or quote screen.

Identify the instrument and return ONLY a JSON object, no prose, no code fence:

{
  "symbol": "the ticker exactly as shown, e.g. AAPL, TSLA, BTCUSD, ETHUSDT",
  "asset_class": "equity" | "crypto" | "etf" | "forex" | "index" | "unknown",
  "timeframe": "the chart interval if visible, e.g. 1m, 5m, 15m, 1h, 1D",
  "price_seen": the most prominent current price as a number, or null,
  "exchange": "exchange or broker name if visible, else null",
  "confidence": 0.0 to 1.0 for how sure you are of the symbol,
  "observations": ["short factual notes on visible chart structure"]
}

Rules:
- Report the ticker as printed. Do not convert or expand it.
- If you can see only a company name, put that in "symbol".
- If no instrument is identifiable, set "symbol" to null and confidence to 0.
- For "observations", note only what is visibly true: trend direction, obvious
  support or resistance, a visible gap, unusual volume bars, drawn levels.
  Do not speculate about what price will do next. Maximum 4 items."""


def read_chart(image_path, model: Optional[str] = None,
               api_key: Optional[str] = None) -> ChartRead:
    """Send the screenshot to Claude and parse the instrument details.

    image_path is a file path, or an open binary file. The web upload passes
    its bytes in memory, so two uploads at once never share a file on disk.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise VisionError(
            "ANTHROPIC_API_KEY is not set, so the screenshot cannot be read. "
            "Either set the key or pass the ticker directly with --symbol.")
    try:
        import anthropic
    except ImportError:
        raise VisionError("the anthropic package is not installed")

    data, media_type = _load_image_bytes(image_path)
    encoded = base64.standard_b64encode(data).decode("ascii")

    # Bounded, because this runs inside the upload's request: the SDK's own
    # default is ten minutes, three times.
    client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
    chosen = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
    # The default model thinks before it answers, and its thinking counts
    # toward max_tokens: at 700 a picture it thought about could be cut off
    # before the answer. Low effort keeps that short. Another model chosen by
    # the owner may not take the setting, so it is sent to the default only.
    quick = {"output_config": {"effort": "low"}} if chosen == DEFAULT_MODEL else {}
    try:
        resp = client.messages.create(
            model=chosen,
            max_tokens=4000,
            **quick,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": media_type,
                                                 "data": encoded}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
    except Exception as exc:
        raise VisionError("vision request failed: %s" % exc)

    stopped = getattr(resp, "stop_reason", "")
    if stopped == "max_tokens":
        raise VisionError("the reply was cut off before it was finished")
    if stopped == "refusal":
        raise VisionError("the model declined to read that picture")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return _parse(text)


def _parse(text: str) -> ChartRead:
    """Pull the JSON object out of the reply, tolerating stray formatting."""
    payload = None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        raise VisionError("could not parse a chart description from the reply: %s"
                          % text[:200])

    price = payload.get("price_seen")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    symbol = payload.get("symbol")
    if isinstance(symbol, str):
        symbol = symbol.strip() or None

    obs = payload.get("observations") or []
    if not isinstance(obs, list):
        obs = [str(obs)]

    try:
        conf = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    return ChartRead(
        symbol=symbol,
        asset_class=(payload.get("asset_class") or None),
        timeframe=(payload.get("timeframe") or None),
        price_seen=price,
        exchange=(payload.get("exchange") or None),
        confidence=max(0.0, min(1.0, conf)),
        observations=[str(o) for o in obs][:4],
        raw=text,
    )
