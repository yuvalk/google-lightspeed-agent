---
name: efficient-counting
description: |
  Answers total/count/"how many?" queries for any resource — CVEs, hosts,
  advisory rules, blueprints, subscriptions, or content sources — with a
  single API call using limit=1 and response metadata instead of fetching
  all pages. [PREFERRED]
metadata:
  author: red-hat
  version: "1.0"
---

## Efficient Counting [PREFERRED]

When the user asks "how many [resources]?" (total count questions), do NOT fetch all
pages to count. Instead, call the relevant MCP tool with `limit=1` and `offset=0` and
read the total from the response metadata — one API call, no data transfer:

- **Vulnerability tools** (JSON:API responses): total is at `meta.total_items`
- **Inventory tools**: total is at `total`
- **Advisor, Content Sources, Image Builder, RHSM tools**: total is at `meta.count`

Pass the user's filters as normal tool arguments alongside `limit=1`.

**Examples:**
- "How many CVEs?" -> call `vulnerability__get_cves` with `limit=1`, report `meta.total_items`
- "How many critical CVEs?" -> call `vulnerability__get_cves` with `limit=1, severity=Critical`, report `meta.total_items`
- "How many hosts?" -> call `inventory__list_hosts` with `limit=1`, report `total`
- "How many hosts running RHEL 9?" -> call `inventory__list_hosts` with `limit=1, operating_system=RHEL 9`, report `total`
- "How many advisor rules?" -> call `advisor__get_active_rules` with `limit=1`, report `meta.count`
- "How many blueprints?" -> call `image-builder__get_blueprints` with `limit=1`, report `meta.count`

## Never Refuse to Count [STRICT]

Do NOT tell the user that counting results is "beyond your operational capacity" or
would "exceed resource limits." You have no such limits — the tools support counting
via response metadata, and you never need to fetch every page to answer "how many."

When the user asks a "how many" question:
1. **Apply filters first** — use the filter parameters documented in the
   `multi-step-workflows` skill to narrow results.
2. **Use response metadata** — make one filtered call with `limit=1` and read the total
   from the metadata field paths described above.

A "how many" question never requires fetching every page. For queries where the
user needs actual data (not just a count), see the `pagination-handling` skill.

## Counting Workflow Examples [GUIDANCE]

**"How many critical remediable CVEs are on host X?"** (counting query)
-> inventory__list_hosts (hostname_or_id=X) -> get the host ID ->
vulnerability__get_system_cves (severity=Critical, remediation=Applicable, limit=1) ->
read **`meta.total_items`** from the response -> report the count.
You do NOT need to fetch every page to answer "how many" — the response metadata gives
the total matching the filters in a single call.

**"How many systems are in my inventory?"** (system counting query)
-> inventory__list_hosts (limit=1) -> read **`total`** from the response ->
report the count. Do NOT use `vulnerability__get_systems` for general system counts —
it returns a smaller subset that excludes immutable systems.
