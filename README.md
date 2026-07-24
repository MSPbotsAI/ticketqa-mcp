# ticketqa-mcp

MCP server for the **MSPbots TicketQA Data Store API** — lets the `mspbots-ticket-qa` Agent write completed QA evaluation results (and read them back) to the App's `qa_results` / `rule_results` tables.

> **Naming note:** this is not a third-party vendor integration. It wraps an **internal MSPbots App API** (path prefix `/apps/agent-ticket-qa/api/qa/...`) that backs the "Agent Ticket QA" feature (see parent PRD [PRD-14493](https://app.clickup.com/t/2280862/PRD-14493)). "MSP" in the header names below refers to the MSPbots Agent Platform itself, not an external MSP tool vendor.

## Overview

This server implements the [Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE transport) and exposes **6 tools** covering the write-back workflow described in the source spec (`api-qa-ingest.md`, attached to [PRD-14991](https://app.clickup.com/t/2280862/PRD-14991)):

1. `ticketqa_start_run` — trigger a QA run, get an `eval_ref`
2. `ticketqa_ingest_result` — write the completed evaluation
3. `ticketqa_report_error` — report a failed run instead of ingesting
4. `ticketqa_validate_result` — dry-run validate an envelope without writing
5. `ticketqa_get_result` — read a single result
6. `ticketqa_get_results` — list results

It follows the MSPbots **Vendor MCP Service SOP**: stateless, no stored credentials, per-request header authentication.

### Coverage and verification status

The source spec (`api-qa-ingest.md`) only fully documents `POST /api/qa/ingest`; the other 5 endpoints were originally just a one-line description each. The actual backend repo could not be located (`tqa-gtm` and `app-aiticketqa` were both checked and ruled out), so the initial build proceeded from the spec alone. **Live testing against a real tenant on `agentint.mspbots.ai` (INT) has since confirmed most of the envelope/response shapes** — see below.

| Tool | Coverage |
|---|---|
| `ticketqa_validate_result` | ✅ Live-verified (dry-run, same envelope as ingest) |
| `ticketqa_ingest_result` | ✅ Envelope shape live-verified via `validate` (same schema); the actual write (`/ingest` itself) has not been called |
| `ticketqa_get_result` | ✅ Live-verified against real ticket data |
| `ticketqa_get_results` | ✅ Live-verified (`status=fail`, `sort=recent`, `page`, `page_size` confirmed working; full enum of allowed `status`/`sort` values not enumerated) |
| `ticketqa_report_error` | ⚠️ Still inferred — has write side effects, not tested |
| `ticketqa_start_run` | ⚠️ Still inferred — triggers a real QA evaluation run (costs an LLM judge call), not tested |

**⚠️ Critical auth finding from live testing:** the source spec only mentions `Authorization: Bearer <token>`, but the platform's routing layer actually requires the tenant ID as an **`X_Tenant_ID` cookie** (not a header, and undocumented in the spec) to resolve which app to route to — a valid bearer token alone gets `404 {"error": "App not found"}`. This server forwards `X-MSP-Tenant-Id` downstream as that cookie. A `Host` cookie was also observed in a real browser-captured request but tested and confirmed **not required**.

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
# {"status": "ok", "service": "ticketqa-mcp", "transport": "http"}
```

No credentials are required for the health endpoint.

## 授权参数说明 (Authentication)

Every request to `/mcp` must include the following HTTP headers:

| Header | 类型 | 是否必填 | 默认值 | 枚举值 | 字段描述 | Example |
|---|---|---|---|---|---|---|
| `X-MSP-Token` | string | 必填 | 无 | 无(自由文本,JWT) | Agent Platform 已签发的访问凭证(JWT bearer token)。本服务原样转发为下游请求的 `Authorization: Bearer <token>`,不做任何换取/校验逻辑。 | `X-MSP-Token: eyJhbGciOiJFZERTQSJ9...` |
| `X-MSP-Tenant-Id` | string | 必填 | 无 | 无(自由文本,UUID) | 租户标识。**转发给下游 TicketQA App API,但形式是 Cookie `X_Tenant_ID=<value>`,不是 header**——这是平台路由层用来判断请求归属哪个 app/租户的机制,源文档完全没提到,是实测确认的(只带 Bearer token 不带这个 cookie 会返回 `404 App not found`)。 | `X-MSP-Tenant-Id: e9f794fe-a6b4-4f35-bd2f-fcd19c5cc308` |
| `X-MSP-Host` | string | 必填 | 无 | 无(自由文本,base URL) | TicketQA App API 所在的 host。本服务会拼接 `/apps/agent-ticket-qa/api/qa/<endpoint>` 得到完整请求地址(与源文档 §4 请求示例的路径一致)。 | `X-MSP-Host: https://agentosint.mspbots.ai` |

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
| `ticketqa_start_run` | 触发一次工单 QA 评审,拿到 `eval_ref`(⚠️请求体推断,未验证,有真实副作用未测试) | `ticket_id`(必填), `trigger?`(dict) |
| `ticketqa_ingest_result` | 把完成的 QA 评审结果写回(幂等键 `eval_ref`;envelope 已通过 validate 实测确认,写入本身未调用) | `eval_ref`(必填), `ticket`(必填 dict), `rule_results`(必填 list[dict], 1-500 条), `trigger?`(dict) |
| `ticketqa_report_error` | 上报一次 QA 运行失败(⚠️字段推断,未验证,有写入副作用未测试) | `eval_ref`(必填), `failed_stage`(必填), `error_type`(必填), `error_message`(必填), `ticket_id?`, `trigger?`(dict) |
| `ticketqa_validate_result` | 用同一 envelope 干跑校验,永不写库(✅ 已实测) | 同 `ticketqa_ingest_result` |
| `ticketqa_get_result` | 查单条评审结果(✅ 已实测) | `ticket_id?`, `eval_ref?`(至少给一个) |
| `ticketqa_get_results` | 查评审结果列表(✅ 已实测,`status=fail`/`sort=recent` 确认可用) | `page?`, `page_size?`, `status?`, `sort?`, `extra_params?`(dict) |

`ticket` 对象字段(用于 `ticketqa_ingest_result` / `ticketqa_validate_result`):`ticket_id`(必填 str)、`ticket_oml_level`(必填 int 1-5)、`ticket_pass`(必填 bool,须等于 `ticket_oml_level >= pass_threshold`)、`pass_threshold`(必填 int 2-5)、`evaluated_at`(必填 ISO 时间串);可选 `ticket_data`、`oml_explain`、`coaching_suggestion`、`ticket_content_hash`、`capture_updated_time`。

`rule_results[i]` 对象字段:`rule_id`(必填 str,同包内不可重复)、`domain`(必填 str)、`score`(必填 `pass`/`fail`)、`confidence`(必填 int 0-100)、`oml_level`(必填 int 1-5)、`findings`(必填 str,≤16000 字符);可选 `base_severity`(`critical`/`major`/`minor`)、`base_weight`、`corrective_action`、`rule_instruction_snapshot`、`alpha`(bool,默认 false)。

`trigger` 对象字段(可选,以上多个工具共用):`trigger_source`(`manual`/`scheduled`/`onboarding`,默认 `manual`)、`filter_id`、`triggered_by`、`rubric_version`、`judge_model`、`psa`(`connectwise`/`autotask`/`default`)。

## 测试示例 (Test Example)

Ingest a QA result:

```json
{
  "method": "tools/call",
  "params": {
    "name": "ticketqa_ingest_result",
    "arguments": {
      "eval_ref": "b3f1c9e2-7a44-4e0b-9c11-2d6f0a5e8c31",
      "ticket": {
        "ticket_id": "3536972",
        "ticket_oml_level": 4,
        "ticket_pass": true,
        "pass_threshold": 3,
        "evaluated_at": "2026-07-24T08:15:30.000Z"
      },
      "rule_results": [
        {
          "rule_id": "response.first_response_time",
          "domain": "responsiveness",
          "score": "pass",
          "confidence": 92,
          "oml_level": 4,
          "findings": "首次响应 8 分钟，优于 SLA 30 分钟。"
        }
      ]
    }
  }
}
```

Equivalent `curl` against the running server (streamable HTTP MCP endpoint):

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "X-MSP-Token: <token>" \
  -H "X-MSP-Tenant-Id: <tenant-id>" \
  -H "X-MSP-Host: https://agentosint.mspbots.ai" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "ticketqa_get_results", "arguments": { "page": 1, "page_size": 10, "status": "fail" } }
  }'
```

## API Reference

- Source spec: `api-qa-ingest.md`, attached to [PRD-14991](https://app.clickup.com/t/2280862/PRD-14991)
- Parent feature: [PRD-14493 — POC Agent Ticket QA](https://app.clickup.com/t/2280862/PRD-14493)

## Known Gaps / Implementation Notes

- **Auth mechanism differs from the source spec**: the spec only documents `Authorization: Bearer <token>`. Live testing found the platform's routing layer additionally requires the tenant ID as an `X_Tenant_ID` **cookie** (undocumented) — without it, requests 404 with `{"error": "App not found"}` even with a valid token. Fixed by forwarding `X-MSP-Tenant-Id` as that cookie.
- `ticketqa_get_results` and `ticketqa_get_result` were originally built from one-line endpoint descriptions and have since been **live-verified** against `agentint.mspbots.ai` — see the coverage table above for the confirmed query params and response shapes.
- `ticketqa_validate_result`'s envelope (shared with `ticketqa_ingest_result`) has been live-verified via a dry-run call. The actual write path (`POST /api/qa/ingest` itself) has not been called, per the standing policy of not testing endpoints with real side effects without explicit instruction.
- `ticketqa_report_error` and `ticketqa_start_run` remain **unverified inferences** — both have real side effects (writing an error record; triggering an actual LLM-judge evaluation run) and were not tested. Verify these against a real call before relying on them in production.
- The actual backend repository implementing `/api/qa/ingest` etc. was not found in GitHub — `tqa-gtm` (TicketQA GTM/sales-analytics app) and `app-aiticketqa` were both checked and ruled out (neither has the `eval_ref`/`schema_version: "2.0"` schema this spec describes). Live testing against the deployed API on `agentint.mspbots.ai` was used instead to confirm behavior.
