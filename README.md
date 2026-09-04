# ticketqa-mcp

MCP server for the **agent-ticketqa App's QA data API** — lets the QA skill
suite (`ticket-qa-runtime` / `qa-writeback` / `qa-ticket-preview`) write and
read back the App-orchestrated, per-ticket QA evaluation pipeline.

> **Naming note:** this is not a third-party vendor integration. It wraps an
> **internal MSPbots App API** (path prefix `/apps/agent-ticket-qa/api/...`)
> that backs the "Agent Ticket QA" feature (parent PRD
> [PRD-14493](https://app.clickup.com/t/2280862/PRD-14493)). "MSP" in the
> header names below refers to the MSPbots Agent Platform itself, not an
> external MSP tool vendor.

## Overview

This server implements the [Model Context Protocol](https://modelcontextprotocol.io/)
(Streamable HTTP/SSE transport) and exposes **7 tools**, one per endpoint in
the App's finalized data-interface spec
(`qa-api-spec.md` v1.0, attached to [PRD-18253](https://app.clickup.com/t/2280862/PRD-18253)):

| Tool | Endpoint | Read/write |
|---|---|---|
| `qa_store_ticket_data` | `POST /api/qa/ticket-data` | write |
| `qa_get_ticket_data` | `GET /api/qa/ticket-data` | **read** |
| `qa_store_domain_results` | `POST /api/qa/domain-results` | write |
| `qa_store_summary` | `POST /api/qa/summary` | write (also archives) |
| `qa_report_writeback` | `POST /api/qa/writeback-report` | write |
| `qa_report_turn_error` | `POST /api/qa/turn-error` | write |
| `qa_get_ruleset` | `GET /api/criteria` | **read** |

It follows the MSPbots **Vendor MCP Service SOP**: stateless, no stored
credentials, per-request header authentication.

### Rebuilt for the v1.0 spec (2026-09-04) — supersedes the original 6-tool build

This server originally wrapped a different, much less complete draft of this
App's API (`api-qa-ingest.md`, one endpoint — `/ingest` — fully specified,
five others one-line descriptions): `ticketqa_start_run` /
`ticketqa_ingest_result` / `ticketqa_report_error` / `ticketqa_validate_result`
/ `ticketqa_get_result` / `ticketqa_get_results`, none of which exist in the
App's API surface any more. **The App's data interface was redesigned around
an explicit App-orchestrated stage machine** (`assemble → judging (per
domain) → summary → archived`, §1.3), and the finalized v1.0 spec
(PRD-18253) is a clean, field-level, from-scratch contract — this rebuild
replaces the tool layer entirely rather than patching the old one. See git
history for the prior build if it's ever needed for reference.

## The evaluation lifecycle, in one paragraph

One ticket evaluation = one `eval_ref` (uuid), generated and fully
orchestrated by the App as a sequence of turns. This server is only ever
called from *inside* one App-initiated turn — a tool here never advances
the stage itself; the next turn always starts on the App's own schedule.
`qa_store_ticket_data` happens once (assemble); `qa_store_domain_results`
happens once per domain (judging — the App tells the skill which domain is
current each turn); `qa_store_summary` happens once, and archives the
evaluation in the same call; `qa_get_ticket_data` may be called at any point
after assembly to re-fetch the authoritative snapshot instead of trusting
conversation memory. If a turn can't be completed, `qa_report_turn_error`
ends it cleanly instead of leaving the App's watchdog to guess a timeout.
Once archived, any external action taken (a PSA note, a field update, an
alert) must be reported through `qa_report_writeback` — including a
deliberate no-op, reported as `status="skipped"`. `qa_get_ruleset` stands
apart from all of this: it's for conversational/preview scoring only, and
carries no `eval_ref`.

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m ticketqa_mcp
```

## Health Check

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

No credentials are required for the health endpoint.

## 授权参数说明 (Authentication)

Every request to `/mcp` must include the following HTTP headers:

| Header | 类型 | 是否必填 | 默认值 | 枚举值 | 字段描述 | Example |
|---|---|---|---|---|---|---|
| `X-MSP-Token` | string | 必填 | 无 | 无(自由文本,JWT) | Agent Platform 已签发的访问凭证(JWT bearer token)——App 侧文档(qa-api-spec.md v1.0 §1.2)明确说这就是网页请求用的同一个 token,没有专用 MCP token、写 token 或签名 header。本服务原样转发为下游请求的 `Authorization: Bearer <token>`,不做任何换取/校验逻辑。 | `X-MSP-Token: eyJhbGciOiJFZERTQSJ9...` |
| `X-MSP-Tenant-Id` | string | 必填 | 无 | 无(自由文本,UUID) | 租户标识。转发给下游 App API 时改名为 `X_Tenant_ID` header——这是**平台路由层**用来判断请求归属哪个 app/租户的机制,App 自己的接口规范完全没提到(它描述的是"请求路由成功之后 App 自己怎么响应",不包括路由本身),是上一版实测确认的(只带 Bearer token 不带这个 header 会返回 `404 {"error": "App not found"}`),这版沿用未重新验证。 | `X-MSP-Tenant-Id: e9f794fe-a6b4-4f35-bd2f-fcd19c5cc308` |
| `X-MSP-Host` | string | 必填 | 无 | 无(自由文本,base URL) | App API 所在的 host。本服务据此拼接 `/apps/agent-ticket-qa/api/qa/<endpoint>`(六个流水线接口)或 `/apps/agent-ticket-qa/api/criteria`(规则集接口,前缀不同——qa-api-spec.md v1.0 §3.7 明确指出)。 | `X-MSP-Host: https://agentosint.mspbots.ai` |

Missing any of the three headers returns `401 Unauthorized`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_HTTP_PORT` | `8080` | Listening port |
| `MCP_HTTP_HOST` | `0.0.0.0` | Listening host |

## MCP Endpoint

```
POST http://localhost:8080/mcp
```

Connect your MCP client with:
- Transport: `http` (Streamable HTTP / SSE)
- Headers: `X-MSP-Token`, `X-MSP-Tenant-Id`, `X-MSP-Host` (all required)

## Tool List

| Tool | 功能 | 参数 |
|---|---|---|
| `qa_store_ticket_data` | 写入装配好的工单快照(assemble 阶段) | `eval_ref`(必填)、`ticket_id`(必填)、`ticket_data`(必填 dict)、`ticket_content_hash?`、`capture_updated_time?`、`psa?` |
| `qa_get_ticket_data` | 回查工单快照(只读,装配完成后任意时刻,含归档后) | `eval_ref`(必填) |
| `qa_store_domain_results` | 写入单个域的逐规则判定(judging 阶段,每域一次) | `eval_ref`(必填)、`domain`(必填)、`rule_results`(必填 list) |
| `qa_store_summary` | 写入整单评级+coaching,同事务内归档(summary 阶段) | `eval_ref`(必填)、`ticket_oml_level`(必填 int 1-5)、`ticket_pass`(必填 bool)、`pass_threshold`(必填 int)、`oml_explain`(必填 dict)、`coaching_suggestion`(必填 str ≤16000)、`skill_version?`、`judge_model?` |
| `qa_report_writeback` | 回报一次写回动作结果(仅归档后) | `eval_ref`(必填)、`action_type`(必填 write_note\|update_field\|send_alert)、`status`(必填 success\|failed\|skipped)、`action_ref?`、`target?`、`detail?`、`error?`、`executed_at?` |
| `qa_report_turn_error` | 上报回合内不可恢复失败(assemble/judging/summary 通用) | `eval_ref`(必填)、`failed_stage`(必填)、`error_type`(必填)、`error_message`(必填)、`detail?` |
| `qa_get_ruleset` | 读取当前生效规则集(只读,与评估无关,供对话式预览) | 无参数 |

`rule_results[]`(用于 `qa_store_domain_results`):`rule_id`(必填,须属于该域下发集合)、`score`(必填 `pass`\|`fail`)、`confidence?`(int 0-100)、`findings`(必填 str ≤16000)、`corrective_action?`(str ≤2048)。**不要**提交 `oml_level`/`alpha`/`base_severity`——App 自己从派发时的规则快照补齐。

字段级完整规格见 [`qa-api-spec.md` v1.0](https://app.clickup.com/t/2280862/PRD-18253)(附件),本 README 只摘录 MCP 封装相关的部分。

## 测试示例 (Test Example)

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-MSP-Token: <token>" \
  -H "X-MSP-Tenant-Id: <tenant-id>" \
  -H "X-MSP-Host: https://agentosint.mspbots.ai" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "qa_get_ruleset", "arguments": {} }
  }'
```

## API Reference

- Source spec: `qa-api-spec.md` v1.0 (finalized 2026-09-04), attached to [PRD-18253](https://app.clickup.com/t/2280862/PRD-18253)
- Index doc for integrators: `MCP-REQUIREMENTS.md`, same attachment set
- Parent feature: [PRD-14493 — POC Agent Ticket QA](https://app.clickup.com/t/2280862/PRD-14493)

## Known Gaps

- **Not yet exercised against a real evaluation.** This build was written
  entirely from the finalized `qa-api-spec.md` v1.0 — field names, types,
  stage-gating rules, and idempotency semantics are all taken directly from
  the spec, not guessed. The spec itself states the App side has passed its
  own offline acceptance suite (fake-skill 32 / pipeline-e2e 21 / p4-verify
  33 assertions), but **no call from this server has been run against a
  real `eval_ref`** — that needs an actual evaluation run (App-triggered) to
  test against, which wasn't available while building this.
- **The `X_Tenant_ID` gateway-routing requirement is carried forward, not
  re-verified against these specific new endpoints.** The prior build of
  this server confirmed empirically that the platform's routing layer 404s
  with a flat `{"error": "App not found"}` (not the App's own envelope
  shape — see next point) without this header, even with a valid bearer
  token. Live-called again during this rebuild (`GET
  /apps/agent-ticket-qa/api/criteria` against a real INT host with a dummy
  tenant/token) and got exactly that same 404 shape back — the routing
  layer's behavior itself is reconfirmed live, though of course a *real*
  tenant/token pair was never tried.
- **Two response envelopes exist, and this client now handles both.** The
  App's own documented envelope (`{success, data}` / `{success: false,
  error: {code, message, details}}`, qa-api-spec.md v1.0 §1.4) only applies
  once a request actually reaches App code. A routing-layer failure (wrong
  or missing tenant, app not found) never reaches that code at all, and
  comes back as this platform's own flat `{"error": "<string>"}` instead —
  confirmed by the live 404 above. `TicketQAClient._handle` checks for a
  `message` key first, then an `error` string key, before giving up with a
  generic "unknown error"; get this wrong and a real "App not found" or
  similar routing failure collapses into an unhelpful message with no
  actionable content.
- **`error.details` (the field-level validation error list) is passed
  through verbatim, per the spec's own explicit instruction** ("skill 依赖
  它在同一回合内自纠重试" — swallowing it lets the model retry blind).
  Never trim or summarize it in a future edit.
- **`qa_store_summary`'s local `ticket_pass` invariant check is a
  convenience, not a substitute for the App's own validation** — it only
  catches the one documented invariant (`ticket_pass ==
  (ticket_oml_level >= pass_threshold)`) before spending a round trip; every
  other field-level rule in the spec is enforced server-side only.
- **No write/config endpoints beyond the seven above are wrapped** — this
  matches `MCP-REQUIREMENTS.md`'s own scope statement exactly ("App 侧 MCP
  的边界 = 第 2 节的七个接口,一个不多一个不少"). The criteria-editing
  endpoints (`PUT/DELETE /api/criteria/rules/:id`) are explicitly *not* to
  be wrapped — rule edits are a human-only, page-only action by design
  (migration decisions D5/D20).
