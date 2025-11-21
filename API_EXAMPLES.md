# API Examples

## Authentication

All endpoints require Bearer token authentication. Set your API key in the `Authorization` header:

```bash
export API_KEY="dev-api-key"
```

## List Invoices

```bash
# Get all invoices
curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:8000/invoices

# Filter by vendor
curl -H "Authorization: Bearer $API_KEY" \
  "http://localhost:8000/invoices?vendor_id=1&page=1&page_size=20"

# Filter by status
curl -H "Authorization: Bearer $API_KEY" \
  "http://localhost:8000/invoices?status=needs_review"
```

## Get Invoice Details

```bash
curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:8000/invoice/{invoice_id}
```

## Structured Query - Total by Vendor

```bash
curl -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "total_by_vendor",
    "vendor_id": 1,
    "from_date": "2025-10-01",
    "to_date": "2025-10-31"
  }' \
  http://localhost:8000/query
```

Response:
```json
{
  "vendor_id": 1,
  "vendor_name": "ACME Supplies Pvt Ltd",
  "period": {
    "from": "2025-10-01",
    "to": "2025-10-31"
  },
  "total_amount": 123450.00,
  "currency": "INR",
  "invoice_count": 8,
  "low_confidence_count": 2,
  "low_confidence_ids": ["uuid1", "uuid2"]
}
```

## Structured Query - Total by Project

```bash
curl -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "total_by_project",
    "project_id": 1,
    "from_date": "2025-10-01",
    "to_date": "2025-10-31"
  }' \
  http://localhost:8000/query
```

## Conversational Agent

```bash
curl -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "How much did Company A spend in October 2025?"
  }' \
  http://localhost:8000/agent
```

Response:
```json
{
  "query": "How much did Company A spend in October 2025?",
  "answer_text": "₹123,450 across 8 invoices (6 auto-matched, 2 need review). See sources below.",
  "sources": [
    {
      "invoice_id": "uuid1",
      "url": "https://...",
      "confidence": 0.92
    }
  ],
  "caveats": [
    "2 invoices low confidence may add up to ₹12,300"
  ]
}
```

## Get Audit Trail

```bash
curl -H "Authorization: Bearer $API_KEY" \
  http://localhost:8000/audit/{invoice_id}
```

## Health Check

```bash
curl http://localhost:8000/health
```

## Python Examples

```python
import requests

API_BASE = "http://localhost:8000"
API_KEY = "dev-api-key"

headers = {"Authorization": f"Bearer {API_KEY}"}

# List invoices
response = requests.get(f"{API_BASE}/invoices", headers=headers)
invoices = response.json()

# Query total by vendor
query = {
    "type": "total_by_vendor",
    "vendor_id": 1,
    "from_date": "2025-10-01",
    "to_date": "2025-10-31"
}
response = requests.post(f"{API_BASE}/query", json=query, headers=headers)
result = response.json()
print(f"Total: {result['total_amount']} {result['currency']}")

# Conversational query
agent_query = {
    "text": "How much did ACME spend in October?"
}
response = requests.post(f"{API_BASE}/agent", json=agent_query, headers=headers)
answer = response.json()
print(answer["answer_text"])
```

## Interactive API Docs

Visit http://localhost:8000/docs for interactive Swagger documentation where you can test all endpoints directly.

