# BasketWatch MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that
exposes the **BasketWatch Irish grocery data** API as tools your AI agent
can call directly. Works with **Claude Desktop**, **Continue.dev**,
**Cursor**, and any other MCP-compatible client.

> ~47,000 Irish supermarket SKUs across Aldi, Tesco, SuperValu and Dunnes
> Stores. Refreshed every Friday at 02:00 UTC. Now queryable by Claude.

## Requires a BasketWatch API key

This MCP server is a **thin client over the BasketWatch API** — you'll need
an API key to use it. Three ways to get one:

- **Direct subscription** (recommended for production use): unlimited API
  access, weekly CSV exports, custom support. Email
  **basketwatchireland@gmail.com** to subscribe.
- **Trial / evaluation key**: time-limited key for one-off exploration.
  Email the same address with subject *"MCP trial key request"*.
- **Already on Apify or RapidAPI?** Those channels have their own auth
  flow and don't use this MCP server — use the SDK / proxy URL they
  provide instead.

The MCP server itself is free open-source — the API access behind it is
what you pay for.

## What your agent can do

Once installed, your agent has 8 grocery-aware tools:

| Tool | What it does |
|---|---|
| `status` | Cross-retailer health snapshot — SKU counts, last-scrape dates, run status |
| `search_products` | Find products by name in one or all stores |
| `compare_price_across_stores` | One-call comparison across all 4 supermarkets |
| `get_promotions` | List products currently on offer at a given store |
| `recent_price_changes` | Week-over-week price movements |
| `newly_added_products` | New listings in a configurable lookback window |
| `removed_products` | Delistings / range cuts |
| `list_products` | Paginated catalogue dump for walking the full assortment |

Example questions your agent can answer:

- *"Where can I buy Heinz Baked Beans cheapest this week?"*
- *"Build me a €40 weekly grocery list across the 4 supermarkets."*
- *"Which products got cheaper at Tesco this week?"*
- *"What promotions are running on chocolate at Dunnes right now?"*
- *"Track the price of Brennans bread for the next 12 weeks."*

## Install

```bash
pip install basketwatch-mcp
```

Or from source:

```bash
git clone https://github.com/basketwatch/basketwatch-mcp.git
cd basketwatch-mcp
pip install -e .
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BASKETWATCH_API_BASE` | (required) | Origin of the BasketWatch API (e.g. `https://basketwatch.fly.dev`, or `https://api.basketwatch.ie` once that's live) |
| `BASKETWATCH_API_KEY` | (required) | Your BasketWatch API key — issued when you subscribe / request a trial |
| `BASKETWATCH_MCP_DAILY_LIMIT` | `0` (off) | Optional client-side daily cap — extra safety on top of your key's server-side limit |
| `BASKETWATCH_MCP_RATE_PER_MIN` | `0` (off) | Optional client-side per-minute rate limit |

**Important**: by default the MCP server imposes **no client-side rate
limits** — paid subscribers get whatever throughput their key allows on the
BasketWatch origin. The two `BASKETWATCH_MCP_*_LIMIT` env vars are escape
valves for use cases like:

- Giving a Claude Desktop install to someone who shouldn't burn through
  the family / team API quota
- Self-imposed budget caps during evaluation

The origin's per-key rate limit (enforced server-side) is the
authoritative throttle. Trial keys get tight limits; paid-subscriber keys
get high or unlimited throughput.

When a client-side limit is configured AND hit, every tool returns:

```json
{
  "error": "Client-side rate limit reached: ...",
  "limit_hit": true,
  "hint": "Email basketwatchireland@gmail.com to get an API key with higher / unlimited usage."
}
```

## Hook it up to Claude Desktop

Edit your Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add a `basketwatch` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "basketwatch": {
      "command": "basketwatch-mcp",
      "env": {
        "BASKETWATCH_API_BASE": "https://basketwatch.fly.dev",
        "BASKETWATCH_API_KEY":  "your-api-key-here"
      }
    }
  }
}
```

Restart Claude Desktop. You'll see "basketwatch" listed as an available
MCP server in Claude's MCP indicator. Ask Claude *"What's the cheapest
1L of milk in Ireland?"* — it'll use the tools automatically.

## Hook it up to Continue.dev / Cursor

These clients use the same MCP protocol over stdio. Add a server block to
your `~/.continue/config.json` (Continue) or equivalent Cursor config:

```json
{
  "mcpServers": {
    "basketwatch": {
      "command": "basketwatch-mcp",
      "env": {
        "BASKETWATCH_API_BASE": "https://basketwatch.fly.dev",
        "BASKETWATCH_API_KEY":  "your-api-key-here"
      }
    }
  }
}
```

## Local development

```bash
git clone https://github.com/basketwatch/basketwatch-mcp.git
cd basketwatch-mcp
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .

export BASKETWATCH_API_BASE="https://basketwatch.fly.dev"
export BASKETWATCH_API_KEY="your-key"

# Run the server directly to test (it speaks MCP over stdio — type JSON-RPC
# requests at it or use `mcp dev` from the MCP SDK for interactive testing).
python basketwatch_mcp.py
```

## Data freshness

- All four retailers are scraped every **Friday at 02:00 UTC**.
- Each tool response includes a `scrape_date` field per row so the agent
  knows exactly when each price was captured.
- Higher-cadence pulls (daily / twice-weekly) are available with a direct
  subscription — email `basketwatchireland@gmail.com`.

## Not affiliated

BasketWatch is not affiliated with Aldi Ireland, Tesco Ireland, Musgrave
SuperValu or Dunnes Stores. Data is collected from publicly available
sources for lawful market research and price comparison purposes.

## License

MIT.
