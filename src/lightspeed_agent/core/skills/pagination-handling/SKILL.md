---
name: pagination-handling
description: |
  Teaches the agent how to handle paginated API responses correctly.
  Covers default fetch behavior, pagination metadata interpretation,
  stop conditions, and per-API pagination conventions for Vulnerability,
  Inventory, Advisor, and other tool categories. [PREFERRED]
metadata:
  author: red-hat
  version: "1.0"
---

## Pagination Awareness [PREFERRED]

Several tools return paginated results. Systems can have 1,000+ CVEs, accounts can have
thousands of hosts.

**Default behavior — fetch first, ask later**: When the user does NOT specify a quantity
or limit, fetch the first page with a sensible default (e.g., 20 for CVE lists, 50 for
host listings). After receiving the response, check the total count — `meta.total_items` for
Vulnerability tools, `total` for Inventory tools, `meta.count` for Advisor and
other tools. If significantly more data exists, tell the user the total and offer
to fetch more:

"Showing 20 of 1,247 CVEs (sorted by severity). Would you like me to fetch more,
or apply filters (e.g., Critical only, remediatable) to narrow the results?"

Do NOT present a pagination menu before the first call — answer the question first,
then let the user decide whether they need more.

**When to skip the offer** (user already specified scope):
- "Show me the top 3 CVEs on host X" → use limit=3, no follow-up needed
- "Get the first page of vulnerabilities" → use limit=100 offset=0, no follow-up needed
- "How many critical CVEs affect host X?" → use the `efficient-counting` skill

**Exception — remediatable CVE queries**: When the user asks for remediatable CVEs on a
specific system, fetch all pages automatically. Remediatable CVEs can appear on any page,
so the first page alone often returns zero matches.

**Pagination execution**: For multi-page fetches, **call the same MCP tool repeatedly**
with JSON arguments matching the tool schema (see the `tool-invocation-rules` skill).
[Red Hat Lightspeed MCP](https://github.com/RedHatInsights/insights-mcp) returns Insights
API JSON as-is; list responses are often JSON:API-style (`data`, `meta`, `links`) or
`results` with `page`/`per_page`/`total` — read the fields present. If the pagination
shape is unclear, fall back to `*_get_openapi` to confirm.

**Vulnerability tools** (OpenAPI `application/vnd.api+json`): Paginated responses include
three required top-level keys: **`data`**, **`links`**, and **`meta`**. Use query
parameters **`limit`** (page size) and **`offset`** (index of the first record). The
API defines **`page`** / **`page_size`** too, but **limit/offset pagination takes
precedence** over page-based pagination — prefer **`limit`** and **`offset`** for every
call. Advance **`offset`** by **`meta.limit`** from the response (or by the `limit` you
requested), e.g. next `offset` = current `meta.offset` + `meta.limit`.

**Pagination metadata** (critical — avoids invalid requests and misleading errors such
as HTTP 403 on out-of-range pages): After **each** response, read:

- **`meta.total_items`**: total rows available for this query (integer).
- **`meta.limit`**, **`meta.offset`**, **`meta.page`**, **`meta.page_size`**, **`meta.pages`**:
current pagination state from the server.
- **`links.next`**: URL for the next page, or **`null`** when there is **no** next page.

**Stop fetching** (whichever applies first) — do **not** issue another tool call to load
"more pages" when:

1. **`links.next`** is **`null`**, or
2. The next **`offset`** you would use is **≥ `meta.total_items`**, or
3. **`data`** is a JSON array with **fewer elements than `limit`** (last partial page)
or **empty**, or
4. The user asked for "N pages" and you have already made **N** successful requests
— unless you already stopped earlier due to (1)–(3).

If the user asked for "N pages" but fewer pages exist, stop when (1)–(3) say so and
report that fewer pages were available (avoids empty-page / out-of-range errors).

**Other tool categories** (Advisor, Inventory, Image Builder, …) may use different
parameter names or response shapes. After each response, advance `offset`/`page`
using `meta`/`links.next` or `total`/`per_page` as appropriate for that API.
If the pagination shape is unfamiliar, use `*_get_openapi` to confirm before looping.

## Never Refuse to Paginate [STRICT]

Do NOT tell the user that paginating or filtering results is "beyond your operational
capacity" or would "exceed resource limits." You have no such limits — the tools support
pagination and filtering, and you can call them as many times as needed.

When the result set is large:
1. **Apply filters first** — use the filter parameters documented in the
   `multi-step-workflows` skill to narrow results before paginating.
2. **Paginate when needed** — if the user needs actual data (not just a count), paginate
   through all pages using the stop conditions above.
3. **For counting queries** — see the `efficient-counting` skill; a "how many" question
   never requires fetching every page.

If a tool result triggers a `tool_result_too_large` error, follow the retry strategies
in the `error-handling` skill (reduce page size, add filters). Never treat a large
result set as a reason to give up.
