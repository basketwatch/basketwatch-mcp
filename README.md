# BasketWatch MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that
exposes the **BasketWatch Irish grocery data** API as tools your AI agent
can call directly. Works with **Claude Desktop**, **Continue.dev**,
**Cursor**, and any other MCP-compatible client.

> ~47,000 Irish supermarket SKUs across Aldi, Tesco, SuperValu and Dunnes
> Stores. Refreshed every night. Now queryable by Claude.

## Try it with no key at all

Install it, point it at the API, ask a question. With no key you get a live
**50-row sample** on every tool, straight from this morning's scrape. Real
prices, real promotions, enough to see whether the data is what you need
before paying anything.

## Paying for it: credit packs

Access is **metered per record returned**, at **EUR 0.001 per record**,
bought up front as credits. No subscription, no monthly minimum, and
**credits never expire**.

| Pack | Records |
|---|---|
| EUR 1.00 | 1,000 |
| EUR 49.99 | 49,990 |
| EUR 99.99 | 99,990 |
| EUR 199.99 | 199,990 |
| EUR 499.99 | 499,990 |
| EUR 999.99 | 999,990 |

Buy one at **<https://basketwatchireland.com/pricing>** and you are emailed a
key. Put it in `BASKETWATCH_API_KEY` and this server starts using it.

**Your credits work here exactly as they do anywhere else.** There is no
separate MCP plan, no surcharge and no second balance to keep track of. One
record returned costs one credit whether it came from `curl`, your own code,
or your assistant calling a tool. Every response reports what it cost and
what is left, and `GET /api/credits` shows the balance and ledger for free.

Calls that return no rows are free. A question that finds nothing costs
nothing.

### Agents can buy their own

An autonomous agent can purchase a credit pack itself, with no human, no
browser and no card entry, by paying over
[MPP](https://mpp.dev) with a Stripe shared payment token. It reads the price
at `/api/credits/mpp`, receives a signed payment challenge, pays, and gets
back a funded key it can use immediately. See the
[API reference](https://basketwatchireland.com/api-reference#agent-payments).

### Bespoke access

Bulk historical exports, scheduled delivery, custom matching or an SLA are a
conversation rather than a signup: **info@basketwatchireland.com**.

The MCP server itself is free and open source. The data behind it is what you
pay for.

## What your agent can do

Once installed, your agent has 8 grocery-aware tools:

| Tool | What it does |
|---|---|
| `status` | Cross-retailer health snapshot — SKU counts, last-scrape dates, run status |
| `search_products` | Find products by name in one or all stores |
| `compare_price_across_stores` | One-call comparison across all 4 supermarkets |
| `get_promotions` | List products currently on offer at a given store |
| `recent_price_changes` | Day-over-day price movements, with shelf moves, promotions and loyalty prices kept separate |
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
| `BASKETWATCH_API_KEY` | (optional) | Your BasketWatch key. Without one you get a 50-row sample per call; with one, up to `BASKETWATCH_MCP_MAX_ROWS`, metered against your credits |
| `BASKETWATCH_MCP_MAX_ROWS` | `200` | Most rows a single tool call may return. Raise it for longer answers, lower it to keep a chatty model's credit spend predictable |
| `BASKETWATCH_MCP_TRANSPORT` | `stdio` | Set to `streamable-http` to host the server for a team instead of running it locally. Note that one hosted server means one key, so all the credits land on that one balance |
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
  "hint": "Email info@basketwatchireland.com to get an API key with higher / unlimited usage."
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

- All four retailers are refreshed **every night**.
- Each tool response includes a `scrape_date` field per row so the agent
  knows exactly when each price was captured.
- Higher-cadence pulls (daily / twice-weekly) are available with a direct
  subscription — email `info@basketwatchireland.com`.

## Not affiliated

BasketWatch is not affiliated with Aldi Ireland, Tesco Ireland, Musgrave
SuperValu or Dunnes Stores. Data is collected from publicly available
sources for lawful market research and price comparison purposes.

## License

MIT.
