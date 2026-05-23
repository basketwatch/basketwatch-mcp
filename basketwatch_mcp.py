"""BasketWatch MCP server.

Exposes the BasketWatch Irish grocery data API as Model Context Protocol
tools so AI agents (Claude Desktop, Continue.dev, Cursor, etc.) can query
shelf prices, promotions and changes across Aldi / Tesco / SuperValu /
Dunnes Stores directly.

Each tool is a thin wrapper over an existing BasketWatch endpoint — the
heavy lifting (scraping, parsing, dedup, anti-bot, weekly refresh) is
already done by the Fly pipeline. This server just exposes it as MCP.

Setup:
  1. pip install mcp httpx
  2. Set environment:
       BASKETWATCH_API_BASE   = origin of your BasketWatch API
                                (e.g. https://basketwatch.fly.dev)
       BASKETWATCH_API_KEY    = a valid API key (issued via the API_KEYS
                                Fly secret on your origin, or any future
                                direct-subscriber key)
  3. python -m basketwatch_mcp  (or wire up via Claude Desktop config)

Tool design notes:
  - Each tool returns plain dicts / lists — the MCP layer serialises them.
  - Errors are returned as a single-element dict {"error": "..."} so the
    agent can decide whether to retry, fall back, or surface to the user.
  - Defaults are tuned so a one-shot agent call ("what's the cheapest
    Heinz beans in Ireland?") returns useful data without the agent
    having to learn pagination first.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Config

API_BASE = os.environ.get("BASKETWATCH_API_BASE", "").rstrip("/")
API_KEY = os.environ.get("BASKETWATCH_API_KEY", "")

# Free-tier defaults for unattended MCP use. Anyone wanting more should
# email basketwatchireland@gmail.com for a higher-limit API key. These
# limits are enforced CLIENT-SIDE — a determined user could fork the
# server, but the BasketWatch origin still rejects requests above the
# server-side per-key rate limit, so the upper bound is enforced there.
DAILY_LIMIT = int(os.environ.get("BASKETWATCH_MCP_DAILY_LIMIT", "100"))
RATE_LIMIT_PER_MIN = int(os.environ.get("BASKETWATCH_MCP_RATE_PER_MIN", "20"))

# Where the daily counter is persisted (survives Claude Desktop restarts).
_USAGE_DIR = Path.home() / ".basketwatch-mcp"
_USAGE_FILE = _USAGE_DIR / "usage.json"

if not API_BASE:
    print(
        "ERROR: BASKETWATCH_API_BASE env var is required "
        "(e.g. https://basketwatch.fly.dev). Set it before launching.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Client-side rate limiting
#
# Two layers:
#   1. Per-minute sliding window — stops Claude bursting hundreds of calls
#      to answer a single question. In-memory only; resets when the MCP
#      server restarts (i.e. when Claude Desktop restarts).
#   2. Daily counter persisted to ~/.basketwatch-mcp/usage.json so the cap
#      survives restarts. Resets on UTC date rollover.
#
# When a limit is hit, the tool returns a structured `error` dict with a
# `hint` field that an LLM can surface to the user — driving the lead
# funnel toward a real API key.

_recent_call_times: deque[float] = deque(maxlen=RATE_LIMIT_PER_MIN * 2)


def _load_daily_count(today_iso: str) -> int:
    """Load today's count from the persisted usage file. Returns 0 if the
    file doesn't exist, is corrupt, or is from a previous day."""
    try:
        data = json.loads(_USAGE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    if data.get("date") != today_iso:
        return 0
    return int(data.get("count", 0))


def _save_daily_count(today_iso: str, count: int) -> None:
    try:
        _USAGE_DIR.mkdir(parents=True, exist_ok=True)
        _USAGE_FILE.write_text(json.dumps({"date": today_iso, "count": count}))
    except OSError:
        # Persistence is best-effort. If the home dir isn't writable
        # (sandboxed Claude Desktop on some platforms), fall back to
        # in-memory tracking — the daily cap still applies during the
        # session, just resets on restart.
        pass


_today_iso: str | None = None
_today_count: int = 0


def _check_and_consume() -> tuple[bool, str | None]:
    """Decrement a free-tier slot if available. Returns (ok, error_message)."""
    global _today_iso, _today_count

    now_dt = datetime.now(timezone.utc)
    today_iso = now_dt.date().isoformat()

    # First call of the day — load persisted count.
    if _today_iso != today_iso:
        _today_iso = today_iso
        _today_count = _load_daily_count(today_iso)

    # Per-minute rate limit (sliding window).
    now_ts = now_dt.timestamp()
    cutoff = now_ts - 60.0
    while _recent_call_times and _recent_call_times[0] < cutoff:
        _recent_call_times.popleft()
    if len(_recent_call_times) >= RATE_LIMIT_PER_MIN:
        return False, (
            f"Free-tier rate limit reached: {RATE_LIMIT_PER_MIN} requests "
            f"per minute. Wait ~60 seconds, or get an API key for higher "
            f"limits — email basketwatchireland@gmail.com."
        )

    # Daily cap.
    if _today_count >= DAILY_LIMIT:
        return False, (
            f"Free-tier daily limit reached: {DAILY_LIMIT} requests per day. "
            f"Resets at 00:00 UTC. For unlimited usage email "
            f"basketwatchireland@gmail.com — direct subscription or "
            f"higher-tier API key available."
        )

    _recent_call_times.append(now_ts)
    _today_count += 1
    _save_daily_count(today_iso, _today_count)
    return True, None


# ---------------------------------------------------------------------------
# Shared HTTP client (one TCP connection, kept alive between tool calls)

_client = httpx.Client(
    base_url=API_BASE,
    headers={
        "User-Agent": "basketwatch-mcp/0.1",
        **({"X-API-Key": API_KEY} if API_KEY else {}),
    },
    timeout=20.0,
)


# Stores and the per-store URL prefix. Aldi is the legacy un-prefixed set
# kept around from when it was the only store; the other three are namespaced.
_STORE_PREFIX = {
    "aldi":      "",
    "tesco":     "/tesco",
    "supervalu": "/supervalu",
    "dunnes":    "/dunnes",
}


def _store_path(store: str, dataset: str) -> str:
    """Build the API path for `(store, dataset)`. Raises on unknown store."""
    if store not in _STORE_PREFIX:
        raise ValueError(f"unknown store {store!r} — must be one of {list(_STORE_PREFIX)}")
    return f"/api{_STORE_PREFIX[store]}/{dataset}"


def _get(path: str, params: dict | None = None) -> Any:
    """Wrap GET so every tool returns either parsed JSON or a single-key
    error dict — saves every tool re-implementing try/except. Also enforces
    the client-side free-tier rate + daily limits before making the call."""
    ok, msg = _check_and_consume()
    if not ok:
        return {
            "error": msg,
            "limit_hit": True,
            "hint": "Email basketwatchireland@gmail.com to get an API key "
                    "with higher / unlimited usage.",
        }
    try:
        r = _client.get(path, params=params or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code} from BasketWatch API",
                "detail": e.response.text[:200]}
    except httpx.RequestError as e:
        return {"error": f"network error calling BasketWatch API: {e}"}
    except Exception as e:
        return {"error": f"unexpected error: {e}"}


# ---------------------------------------------------------------------------
# MCP server

mcp = FastMCP(
    "basketwatch",
    instructions=(
        "Tools to query Irish grocery data — shelf prices, promotions, "
        "loyalty-card prices and weekly price changes across Aldi, Tesco, "
        "SuperValu and Dunnes Stores. Data refreshes every Friday at 02:00 "
        "UTC. Use these tools when the user asks about Irish supermarket "
        "prices, comparisons, promotions, or price trends."
    ),
)


@mcp.tool()
def status() -> dict:
    """Get a cross-retailer freshness snapshot — SKU count, products on
    promotion, last scrape date and most-recent-run status per supermarket.

    Use this when the user wants to know what data is available, or to
    confirm the feed is current before answering price questions.
    """
    return _get("/api/status")


@mcp.tool()
def search_products(query: str, store: str | None = None, limit: int = 10) -> Any:
    """Search products by name across one or all Irish supermarkets.

    Args:
        query: substring to match against product names — case-insensitive
            (e.g. "heinz baked beans", "kerrygold butter", "pringles").
        store: optionally filter to one of "aldi", "tesco", "supervalu",
            "dunnes". When None, searches Aldi only (legacy default).
        limit: how many results to return (default 10, max 500).

    Returns the matching product rows: SKU id, name, brand, price,
    unit_price (€/kg or €/L), pack_size, category_path, url.
    """
    store = store or "aldi"
    return _get(
        _store_path(store, "products"),
        params={"q": query, "limit": min(int(limit), 500)},
    )


@mcp.tool()
def compare_price_across_stores(query: str, limit_per_store: int = 5) -> dict:
    """Compare a product's prices across all four supermarkets in one call.

    Use this when the user asks "where is X cheapest?" or "compare X across
    stores". Returns a dict keyed by store, with up to `limit_per_store`
    matching products per store (in case there are several pack sizes).

    Args:
        query: product-name substring (e.g. "heinz baked beans 415g").
        limit_per_store: how many matches per store (default 5).
    """
    out: dict[str, Any] = {}
    for store in ("aldi", "tesco", "supervalu", "dunnes"):
        result = _get(
            _store_path(store, "products"),
            params={"q": query, "limit": limit_per_store},
        )
        out[store] = result if isinstance(result, list) else [result]
    return out


@mcp.tool()
def get_promotions(store: str, limit: int = 25) -> Any:
    """List products currently on promotion at a given supermarket.

    Returns offer label (e.g. "3 for €5", "SAVE €0.75"), was-price,
    validity window, and where applicable Tesco Clubcard prices,
    SuperValu Real Rewards prices or Dunnes member offers.

    Args:
        store: one of "tesco", "supervalu", "dunnes". (Aldi doesn't
            publish multibuy promos so isn't supported here.)
        limit: how many promotional rows to return (default 25).
    """
    if store == "aldi":
        return {"error": "Aldi doesn't publish multibuy promotions; "
                         "no promotions dataset is available for Aldi."}
    return _get(_store_path(store, "promotions"),
                params={"limit": min(int(limit), 500)})


@mcp.tool()
def recent_price_changes(store: str, limit: int = 25) -> Any:
    """Week-over-week price movements for a single supermarket — products
    whose shelf price moved between the latest weekly snapshot and the
    previous one. Returns delta and % change per SKU.

    Use this when the user asks "what got more/less expensive this week?"

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        limit: how many movers to return (default 25).
    """
    return _get(_store_path(store, "changes"),
                params={"limit": min(int(limit), 500)})


@mcp.tool()
def newly_added_products(store: str, days_back: int = 7, limit: int = 25) -> Any:
    """Products newly listed at a supermarket within a configurable lookback
    window — range additions / new launches.

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        days_back: how many days back to look (default 7 = since last scrape).
        limit: how many new products to return.
    """
    return _get(_store_path(store, "new-products"),
                params={"days": int(days_back), "limit": min(int(limit), 500)})


@mcp.tool()
def removed_products(store: str, days_back: int = 7, limit: int = 25) -> Any:
    """Products that have disappeared from a supermarket's catalogue in the
    given lookback window — delistings / range cuts.

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        days_back: how many days back to consider (default 7).
        limit: how many removed products to return.
    """
    return _get(_store_path(store, "removed"),
                params={"days": int(days_back), "limit": min(int(limit), 500)})


@mcp.tool()
def list_products(store: str, limit: int = 100, offset: int = 0) -> Any:
    """Paginated dump of a supermarket's full catalogue — use when an agent
    needs to scan the whole assortment (e.g. to find products matching
    multiple criteria the search-by-name tool can't express).

    For most "find product X" tasks, `search_products` is the better tool —
    use this one only when you need to walk the catalogue.

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        limit: page size (default 100, max 500).
        offset: pagination offset.
    """
    return _get(_store_path(store, "products"),
                params={"limit": min(int(limit), 500), "offset": int(offset)})


# ---------------------------------------------------------------------------
# Entry point

def main() -> None:
    """Launch the MCP server over stdio (the standard transport for
    Claude Desktop / Continue / Cursor)."""
    mcp.run()


if __name__ == "__main__":
    main()
