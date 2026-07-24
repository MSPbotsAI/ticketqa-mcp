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

### ⚠️ Coverage and verification status

The source spec **only fully documents `POST /api/qa/ingest`** (request/response schema, validation rules, idempotency semantics). The other 5 endpoints are mentioned by name and a one-line description only. Two repos were checked (`tqa-gtm`, `app-aiticketqa`) looking for the actual backend implementation to confirm exact field names, but neither contains the `eval_ref`/`schema_version: "2.0"` schema described in the spec — the real backend repo was not found, and this build proceeded from the spec alone per explicit instruction.

| Tool | Coverage |
|---|---|
| `ticketqa_ingest_result` | ✅ Fully specified in source doc |
| `ticketqa_validate_result` | ✅ Fully specified (same envelope as ingest) |
| `ticketqa_report_error` | ⚠️ Inferred — doc only says "same envelope header fields + failed_stage/error_type/error_message" |
| `ticketqa_start_run` | ⚠️ Inferred — doc only documents the response shape (`{eval_ref, status}`), not the request body |
| `ticketqa_get_result` | ⚠️ Inferred — doc only names the endpoint, no query params documented |
| `ticketqa_get_results` | ⚠️ Inferred — doc only names the endpoint, no query params documented |

**Verify the 4 ⚠️ tools against a real call before relying on them in production.**

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
| `X-MSP-Tenant-Id` | string | 必填 | 无 | 无(自由文本,UUID) | 租户标识。按 SOP 要求作为必填鉴权参数接收,但**不转发**给下游 TicketQA App API——该 API 当前是单租户实现(`tenant_code` 恒为 `"default"`)。 | `X-MSP-Tenant-Id: e9f794fe-a6b4-4f35-bd2f-fcd19c5cc308` |
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
| `ticketqa_start_run` | 触发一次工单 QA 评审,拿到 `eval_ref`(⚠️请求体推断,未验证) | `ticket_id`(必填), `trigger?`(dict) |
| `ticketqa_ingest_result` | 把完成的 QA 评审结果写回(幂等键 `eval_ref`) | `eval_ref`(必填), `ticket`(必填 dict), `rule_results`(必填 list[dict], 1-500 条), `trigger?`(dict) |
| `ticketqa_report_error` | 上报一次 QA 运行失败(⚠️字段推断,未验证) | `eval_ref`(必填), `failed_stage`(必填), `error_type`(必填), `error_message`(必填), `ticket_id?`, `trigger?`(dict) |
| `ticketqa_validate_result` | 用同一 envelope 干跑校验,永不写库 | 同 `ticketqa_ingest_result` |
| `ticketqa_get_result` | 查单条评审结果(⚠️查询参数推断,未验证) | `ticket_id?`, `eval_ref?`(至少给一个) |
| `ticketqa_get_results` | 查评审结果列表(⚠️查询参数推断,未验证) | `limit?`, `offset?`, `ticket_id?`, `extra_params?`(dict) |

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
    "params": { "name": "ticketqa_get_results", "arguments": { "limit": 10 } }
  }'
```

## API Reference

- Source spec: `api-qa-ingest.md`, attached to [PRD-14991](https://app.clickup.com/t/2280862/PRD-14991)
- Parent feature: [PRD-14493 — POC Agent Ticket QA](https://app.clickup.com/t/2280862/PRD-14493)

## Known Gaps / Implementation Notes

- Only `POST /api/qa/ingest` and `POST /api/qa/validate` (same envelope) are backed by a fully-documented schema. `ticketqa_report_error`, `ticketqa_start_run`, `ticketqa_get_result`, and `ticketqa_get_results` were built from one-line descriptions in the source doc — **field names, requiredness, and response shapes for these 4 are not confirmed against the actual backend** and should be validated against a real call (or the actual backend source, once located) before production use.
- The actual backend repository implementing `/api/qa/ingest` etc. was not found — `tqa-gtm` (TicketQA GTM/sales-analytics app) and `app-aiticketqa` were both checked and ruled out (neither has the `eval_ref`/`schema_version: "2.0"` schema this spec describes).
- `X-MSP-Tenant-Id` is accepted and required per the Vendor MCP Service SOP's header-auth contract, but is intentionally **not forwarded** to the downstream API, which is currently single-tenant (`tenant_code` hardcoded to `"default"` per the source doc).
- Not yet tested against a live TicketQA App API — only protocol-level verification (health check, 401 on missing headers, `tools/list` returning all 6 tools) has been done so far.
