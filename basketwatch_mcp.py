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
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

# stdout carries the MCP protocol on the stdio transport, so every diagnostic
# must go to stderr or it corrupts the session.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("basketwatch-mcp")


# ---------------------------------------------------------------------------
# Config

API_BASE = os.environ.get("BASKETWATCH_API_BASE", "").rstrip("/")
API_KEY = os.environ.get("BASKETWATCH_API_KEY", "")

# Client-side rate limits — DEFAULT OFF.
#
# v0.1.0 enforced 100/day + 20/min in the MCP server itself. That was a
# design mistake: paying subscribers (who paid for unlimited via their
# key) would have been throttled by the very thin client they were using.
# Rate limiting belongs on the ORIGIN, keyed per-API-key, where the
# server-side `api_keys` table can express "this key is trial / unlimited /
# enterprise" once and have every channel (direct, MCP, RapidAPI, Apify)
# enforce it consistently.
#
# These env vars stay as escape valves — set them if you want extra
# client-side caps on top of the origin's enforcement (e.g. you're
# handing a Claude Desktop install to someone you don't fully trust).
# A value of 0 (or unset) disables that layer entirely, which is the
# default behaviour.
# Ceiling on rows any single tool call may return.
#
# THIS IS NOT THE SECURITY BOUNDARY. The origin is: it caps anonymous callers
# at 50 rows per request and will not serve more however this client asks, so
# lowering this number protects nothing. What it does control is the paying
# customer's experience, and a cap of 50 made a funded key worth exactly as
# much as no key at all through MCP, which is the opposite of the point.
#
# 200 matches the API's own analytics page size: enough for a real answer,
# small enough that a model looping over tool calls cannot quietly spend a
# fortune. Every row is metered against the caller's key either way.
MAX_ROWS = max(1, int(os.environ.get("BASKETWATCH_MCP_MAX_ROWS", "200")))


def _opt_in_int(name: str) -> int:
    raw = os.environ.get(name, "0").strip()
    try:
        v = int(raw)
        return v if v > 0 else 0
    except ValueError:
        return 0


DAILY_LIMIT = _opt_in_int("BASKETWATCH_MCP_DAILY_LIMIT")
RATE_LIMIT_PER_MIN = _opt_in_int("BASKETWATCH_MCP_RATE_PER_MIN")

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

# maxlen falls back to 64 when no per-minute limit is set so the deque
# stays bounded even in the (unused) no-limit code path.
_recent_call_times: deque[float] = deque(maxlen=max(RATE_LIMIT_PER_MIN, 32) * 2)


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
    """Decrement a client-side limit slot if any limit is configured.

    Returns (ok, error_message). If neither DAILY_LIMIT nor RATE_LIMIT_PER_MIN
    is set (both default 0), this is a no-op — the request is allowed through
    and the origin's per-API-key rate limit is the only enforcement. That's
    the default and recommended config for paying subscribers.
    """
    if DAILY_LIMIT == 0 and RATE_LIMIT_PER_MIN == 0:
        return True, None

    global _today_iso, _today_count

    now_dt = datetime.now(timezone.utc)
    today_iso = now_dt.date().isoformat()

    if _today_iso != today_iso:
        _today_iso = today_iso
        _today_count = _load_daily_count(today_iso)

    # Per-minute rate limit (sliding window) — only if configured.
    if RATE_LIMIT_PER_MIN > 0:
        now_ts = now_dt.timestamp()
        cutoff = now_ts - 60.0
        while _recent_call_times and _recent_call_times[0] < cutoff:
            _recent_call_times.popleft()
        if len(_recent_call_times) >= RATE_LIMIT_PER_MIN:
            return False, (
                f"Client-side rate limit reached: {RATE_LIMIT_PER_MIN} "
                f"requests per minute (configured via BASKETWATCH_MCP_RATE_PER_MIN). "
                f"Wait ~60 seconds or remove the env var to defer to your "
                f"API key's server-side limit instead."
            )
        _recent_call_times.append(now_ts)

    # Daily cap — only if configured.
    if DAILY_LIMIT > 0:
        if _today_count >= DAILY_LIMIT:
            return False, (
                f"Client-side daily limit reached: {DAILY_LIMIT} requests/day "
                f"(configured via BASKETWATCH_MCP_DAILY_LIMIT). Resets at 00:00 "
                f"UTC. For unlimited usage, unset the env var and rely on your "
                f"API key's server-side limit — or email info@basketwatchireland.com "
                f"for a higher-tier key."
            )
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
            "hint": "Email info@basketwatchireland.com to get an API key "
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
# Result shapes
#
# Declaring these gives every tool an `outputSchema` and makes the SDK emit
# `structuredContent` beside the human-readable text, so a client can validate
# and address fields instead of re-parsing JSON out of a string.
#
# `extra="allow"` is deliberate. The API gains fields over time (barcodes,
# regulated names, match ids) and a closed model would drop them silently, or
# fail validation on data that is perfectly good. The schema documents what is
# always present; anything extra still reaches the caller.

class _Row(BaseModel):
    model_config = ConfigDict(extra="allow")


class Product(_Row):
    id: str = Field(description="Stable per-retailer product id, e.g. tesco:12345")
    name: str = Field(description="Product name as the retailer lists it")
    price: float | None = Field(default=None, description="Shelf price in euro")
    brand: str | None = None
    unit_price: float | None = Field(default=None, description="Price per kg or per litre")
    unit_basis: str | None = Field(default=None, description="The unit unit_price is measured in")
    category_path: str | None = None
    scrape_date: str | None = Field(default=None, description="Snapshot date, YYYY-MM-DD")


class Promotion(_Row):
    id: str
    name: str
    price: float | None = Field(default=None, description="Price while on promotion")
    was_price: float | None = Field(default=None, description="Price before the promotion")
    offer_text: str | None = Field(default=None, description="Offer label as displayed")
    offer_valid: str | None = Field(default=None,
                                    description="Validity window in the retailer's own wording")


class PriceChange(_Row):
    id: str
    name: str
    price_now: float | None = None
    prev_price: float | None = None
    pct: float | None = Field(default=None, description="Percentage change, signed")


class PriceChanges(_Row):
    """The /changes payload.

    Shelf moves, promotions and loyalty-card changes are deliberately SEPARATE
    buckets, not one blended list: an expiring offer is not a price rise, and
    merging them turns a price report into a promotions calendar.
    """
    date: str | None = Field(default=None, description="Snapshot being reported")
    prev_date: str | None = Field(default=None, description="Snapshot compared against")
    shelf_increases: list[PriceChange] | None = None
    shelf_decreases: list[PriceChange] | None = None
    promotions: list[PriceChange] | None = Field(
        default=None, description="Offers starting or ending, not shelf moves")
    clubcard_changes: list[PriceChange] | None = Field(
        default=None, description="Loyalty-card price changes, Tesco only")
    shelf_increases_total: int | None = None
    shelf_decreases_total: int | None = None
    promotions_total: int | None = None
    clubcard_changes_total: int | None = None


class StoreFreshness(_Row):
    products: int | None = None
    on_promotion: int | None = None
    last_scrape_date: str | None = None
    last_run_status: str | None = None


class Freshness(_Row):
    status: str | None = None
    as_of: str | None = None
    stores: dict[str, StoreFreshness] | None = None


def _as(model: type[BaseModel], data: Any) -> Any:
    """Coerce an API payload into the declared result model.

    Error payloads pass through untouched: `_get` returns {"error": ...} or
    {"limit_hit": ...} dicts that are not rows and must reach the caller as-is,
    so the model can see what went wrong and self-correct.
    """
    if isinstance(data, dict) and ("error" in data or "limit_hit" in data):
        return data
    try:
        if isinstance(data, list):
            return [model.model_validate(r) for r in data]
        return model.model_validate(data)
    except Exception:
        # Never fail a call over a schema mismatch; the data is still useful.
        log.warning("result did not match %s, returning it unvalidated", model.__name__)
        return data


# ---------------------------------------------------------------------------
# MCP server

mcp = FastMCP(
    "basketwatch",
    instructions=(
        "Tools to query Irish grocery data — shelf prices, promotions, "
        "loyalty-card prices and day-over-day price changes across Aldi, Tesco, "
        "SuperValu and Dunnes Stores. Data refreshes every night across all "
        "four retailers. Use these tools when the user asks about Irish "
        "supermarket prices, comparisons, promotions, or price trends."
    ),
)


# Every tool here is a READ against a live external dataset, and says so.
#
# Without annotations a client has to assume the worst and prompt the user
# before each call, which for a data API means a confirmation dialog on every
# question asked. Declaring these lets a client auto-approve reads, which is
# the difference between a usable assistant and an irritating one.
#
#   read_only    nothing is ever written or deleted
#   idempotent   asking twice returns the same answer, barring a new scrape
#   open_world   the answer comes from live retailer data, not a closed set
def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


@mcp.tool(title="Data freshness and coverage", annotations=_read_only("Data freshness and coverage"))
def status() -> Freshness:
    """Get a cross-retailer freshness snapshot — SKU count, products on
    promotion, last scrape date and most-recent-run status per supermarket.

    Use this when the user wants to know what data is available, or to
    confirm the feed is current before answering price questions.
    """
    return _as(Freshness, _get("/api/status"))


@mcp.tool(title="Search Irish supermarket products", annotations=_read_only("Search Irish supermarket products"))
def search_products(query: str, store: str | None = None, limit: int = 10) -> list[Product]:
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
    return _as(Product, _get(
        _store_path(store, "products"),
        params={"q": query, "limit": min(int(limit), MAX_ROWS)},
    ))


@mcp.tool(title="Compare a product across supermarkets", annotations=_read_only("Compare a product across supermarkets"))
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
            params={"q": query, "limit": min(int(limit_per_store), MAX_ROWS)},
        )
        out[store] = result if isinstance(result, list) else [result]
    return out


@mcp.tool(title="Current promotions at a supermarket", annotations=_read_only("Current promotions at a supermarket"))
def get_promotions(store: str, limit: int = 25) -> list[Promotion]:
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
    return _as(Promotion, _get(_store_path(store, "promotions"),
                params={"limit": min(int(limit), MAX_ROWS)}))


@mcp.tool(title="Recent price changes", annotations=_read_only("Recent price changes"))
def recent_price_changes(store: str, limit: int = 25) -> PriceChanges:
    """Week-over-week price movements for a single supermarket — products
    whose shelf price moved between the latest weekly snapshot and the
    previous one. Returns delta and % change per SKU.

    Use this when the user asks "what got more/less expensive this week?"

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        limit: how many movers to return (default 25).
    """
    return _as(PriceChanges, _get(_store_path(store, "changes"),
                params={"limit": min(int(limit), MAX_ROWS)}))


@mcp.tool(title="Newly stocked products", annotations=_read_only("Newly stocked products"))
def newly_added_products(store: str, days_back: int = 7, limit: int = 25) -> list[Product]:
    """Products newly listed at a supermarket within a configurable lookback
    window — range additions / new launches.

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        days_back: how many days back to look (default 7 = since last scrape).
        limit: how many new products to return.
    """
    return _as(Product, _get(_store_path(store, "new-products"),
                params={"days": int(days_back), "limit": min(int(limit), MAX_ROWS)}))


@mcp.tool(title="Delisted products", annotations=_read_only("Delisted products"))
def removed_products(store: str, days_back: int = 7, limit: int = 25) -> list[Product]:
    """Products that have disappeared from a supermarket's catalogue in the
    given lookback window — delistings / range cuts.

    Args:
        store: "aldi" | "tesco" | "supervalu" | "dunnes".
        days_back: how many days back to consider (default 7).
        limit: how many removed products to return.
    """
    return _as(Product, _get(_store_path(store, "removed"),
                params={"days": int(days_back), "limit": min(int(limit), MAX_ROWS)}))


@mcp.tool(title="Browse the catalogue", annotations=_read_only("Browse the catalogue"))
def list_products(store: str, limit: int = 100, offset: int = 0) -> list[Product]:
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
    return _as(Product, _get(_store_path(store, "products"),
                params={"limit": min(int(limit), MAX_ROWS), "offset": int(offset)}))


# ---------------------------------------------------------------------------
# Resources
#
# Tools answer questions; resources give the model the context it needs to ask
# good ones. Without these it has to guess the store names, guess which
# datasets exist, and guess whether today's data has landed yet, and a wrong
# guess costs a failed tool call the customer still pays for.
#
# Both are cheap: the catalogue one is static, and freshness is a free,
# unmetered endpoint.

@mcp.resource("basketwatch://catalogue", name="What BasketWatch covers",
              mime_type="text/markdown")
def catalogue_resource() -> str:
    """The stores, datasets and conventions, so a model does not have to guess."""
    return """# BasketWatch coverage

## Stores
`aldi`, `tesco`, `supervalu`, `dunnes`. Aldi is the unprefixed set, so its
catalogue is `/api/products` rather than `/api/aldi/products`.

## Datasets
- products: catalogue with shelf price, unit price, pack size, promotion, barcode
- changes: day-over-day movements, with shelf moves, promotions and loyalty
  prices kept as SEPARATE signals rather than blended into one number
- promotions: what is on offer now, with the retailer's own validity window
- new-products / removed: lines appearing and disappearing from the range

## Conventions
- Prices are euro decimals. `unit_price` is per kg or per litre, and
  `unit_basis` names which.
- `scrape_date` is the snapshot the values come from.
- A promotion means different things by retailer: SuperValu and Dunnes
  overwrite the displayed price with the offer price, while Tesco leaves the
  shelf price alone and shows a separate Clubcard price. Compare on a shelf
  basis or a league table becomes a promotions calendar.
- Every record returned costs one credit. Calls that return no rows are free.

## What is not here
Lidl is not tracked. Prices are as displayed online; in-store prices can differ.
"""


@mcp.resource("basketwatch://freshness", name="Today's data freshness",
              mime_type="application/json")
def freshness_resource() -> str:
    """Per-retailer coverage and last run status. Free and unmetered."""
    return json.dumps(_get("/api/status"), indent=2)


# ---------------------------------------------------------------------------
# Prompts
#
# The tools are general; these are the three jobs people actually turn up with.
# Having them as prompts means a user picks one from a menu instead of having
# to know how to phrase it, and the model gets told which tool to reach for and
# which traps to avoid.

@mcp.prompt(name="cheapest_shop", title="Find the cheapest supermarket for a basket")
def cheapest_shop_prompt(items: str) -> str:
    """Compare a shopping list across all four supermarkets.

    Args:
        items: comma-separated products, e.g. "butter, teabags, chicken breasts".
    """
    return f"""Using the BasketWatch tools, work out which Irish supermarket is
cheapest for this basket: {items}

For each item call compare_price_across_stores, then total each retailer.

Be careful with two things:
- Compare like with like. If one chain's price is a promotion and another's is
  the standing shelf price, say so rather than declaring a winner silently.
- Tesco shows a separate Clubcard price. Report both the shelf total and the
  Clubcard total, because they can rank the chains differently.

Finish with the totals per chain, the biggest single saving, and the date the
prices came from."""


@mcp.prompt(name="price_watch", title="What changed price recently")
def price_watch_prompt(store: str = "tesco") -> str:
    """Summarise recent price movements at one retailer.

    Args:
        store: aldi, tesco, supervalu or dunnes.
    """
    return f"""Using recent_price_changes for {store}, summarise what moved.

Separate genuine shelf-price changes from promotions starting and ending: they
are different signals in the data and mean different things to a shopper. Call
out anything that moved more than 20%, and note that a large jump is more often
an offer expiring than a real increase.

Give the biggest risers and fallers with old and new prices, and say which
snapshot date this is."""


@mcp.prompt(name="promotion_sweep", title="What is worth buying on offer")
def promotion_sweep_prompt(store: str = "tesco", budget: str = "") -> str:
    """Find the promotions actually worth acting on at one retailer.

    Args:
        store: aldi, tesco, supervalu or dunnes.
        budget: optional cap, e.g. "under 3 euro".
    """
    cap = f" Only include items {budget}." if budget else ""
    return f"""Using get_promotions for {store}, list the offers worth acting
on.{cap}

Rank by how much is actually saved, using was_price against the current price,
not by how loud the offer label is. Where offer_valid is present, say when each
offer ends so the user knows how long they have.

Ignore anything where the saving cannot be computed from the data rather than
guessing at it."""


# ---------------------------------------------------------------------------
# Entry point

def main() -> None:
    """Launch the server.

    stdio is the default and the right choice for a desktop client, which
    spawns the process itself. Set BASKETWATCH_MCP_TRANSPORT=streamable-http to
    host it instead, for a team sharing one deployment rather than each running
    their own. The HTTP transport binds the usual PORT/HOST pair so it drops
    onto Fly or any container host unchanged.

    Note that hosting it centrally means one API key serves everyone behind it,
    so the credits all land on that key. Per-person billing wants per-person
    keys, which means stdio.
    """
    transport = os.environ.get("BASKETWATCH_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        mcp.settings.host = os.environ.get("HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("PORT", "8080"))
        log.info("serving MCP over streamable HTTP on %s:%s",
                 mcp.settings.host, mcp.settings.port)
        mcp.run(transport="streamable-http")
        return
    mcp.run()


if __name__ == "__main__":
    main()
