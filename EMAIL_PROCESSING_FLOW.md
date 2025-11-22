# Email Processing Flow - How It Works

## Overview

The system processes emails through a multi-stage pipeline:

```
Email Inbox → Ingestion → S3 Storage → Redis Queue → Extraction → Database → Reconciliation
```

## Step-by-Step Process

### 1. **Email Ingestion** (`services/ingestion/main.py`)

The ingestion service polls your Gmail inbox for new emails.

**How it works:**
- Connects to Gmail API using OAuth
- Polls for unread emails every 60 seconds (configurable)
- For each new email:
  1. Downloads the full email message (JSON format)
  2. Saves raw email to S3: `s3://bucket/inbox/raw/{email_id}.json`
  3. Downloads all attachments (PDFs, images, etc.)
  4. Saves attachments to S3: `s3://bucket/inbox/attachments/{email_id}/{filename}`
  5. Creates a job in Redis queue with email metadata

**Example job payload:**
```json
{
  "email_id": "abc123",
  "source": "gmail",
  "s3_raw": "s3://bucket/inbox/raw/abc123.json",
  "attachments": [
    "s3://bucket/inbox/attachments/abc123/invoice.pdf"
  ],
  "received_at": "2025-11-21T14:30:00"
}
```

### 2. **Extraction Worker** (`services/extractor/worker.py`)

The extraction worker processes jobs from the Redis queue.

**How it works:**
- Listens to `extraction_queue` in Redis
- For each job:
  1. Downloads raw email JSON from S3
  2. Downloads all attachments from S3
  3. Extracts text from email body (plain text or HTML)
  4. Processes attachments:
     - **PDFs**: Uses `pdfplumber` for digital PDFs, `pytesseract` (OCR) for scanned PDFs
     - **Images**: Uses `pytesseract` for OCR text extraction
  5. Combines all text into `raw_text` field
  6. Runs field extraction using regex patterns:
     - Invoice number (patterns like "Invoice #", "INV-", etc.)
     - Date (various date formats)
     - Vendor name (company names with Ltd/Inc/etc.)
     - Total amount (keywords: "Total", "Amount Due", etc.)
     - Currency (currency codes or symbols)
     - Line items (from PDF tables if detected)
  7. Creates invoice record in database with:
     - `raw_email_s3`: Link to original email
     - `attachments`: List of attachment URLs
     - `raw_text`: All extracted text
     - `extracted`: JSONB with all fields + confidence scores
     - `reconciliation_status`: "needs_review"
  8. Saves extraction results to S3: `s3://bucket/inbox/extraction/{invoice_id}.json`

**Example extracted data:**
```json
{
  "vendor_name": {
    "value": "ACME Supplies Pvt Ltd",
    "confidence": 0.94,
    "provenance": {
      "attachment": "invoice.pdf",
      "page": 1,
      "snippet": "ACME Supplies Pvt Ltd\n123 Main St"
    }
  },
  "invoice_number": {
    "value": "INV-2025-123",
    "confidence": 0.98
  },
  "date": {
    "value": "2025-10-21",
    "confidence": 0.95
  },
  "total_amount": {
    "value": 11210.00,
    "confidence": 0.9,
    "currency": "INR"
  }
}
```

### 3. **Reconciliation Worker** (`services/reconciler/worker.py`)

The reconciliation worker matches invoices to known vendors and projects.

**How it works:**
- Scans for invoices with `reconciliation_status = 'needs_review'`
- For each invoice:
  1. Extracts vendor name from `extracted.vendor_name.value`
  2. Uses fuzzy matching (RapidFuzz) to compare with:
     - `vendors.canonical_name`
     - `vendors.aliases` (array of alternative names)
  3. Scoring:
     - **Score ≥ 90**: Auto-match → sets `normalized.vendor_id`, status = "auto_matched"
     - **Score 60-89**: Stores suggestions in `extra.suggestions` for review
     - **Score < 60**: Leaves for manual review
  4. Same process for project matching (if project name detected)
  5. Updates `normalized` JSONB with:
     - `vendor_id`, `vendor_name`
     - `project_id`, `project_name` (if matched)
     - `total_amount`, `currency`, `date` (from extracted)

**Example reconciliation:**
- Extracted vendor: "ACME Supplies"
- Database vendor: "ACME Supplies Pvt Ltd"
- Fuzzy match score: 92%
- Result: Auto-matched, `normalized.vendor_id = 1`

### 4. **Review UI** (`services/ui/review.py`)

The Streamlit UI allows manual review and correction.

**Features:**
- Lists invoices needing review
- Shows extracted fields with confidence scores
- Shows reconciliation suggestions
- Allows editing fields
- Creates audit trail for all changes
- Saves training examples for ML improvements

### 5. **API Access** (`services/api/main.py`)

The FastAPI provides endpoints to query processed invoices.

**Endpoints:**
- `GET /invoices` - List invoices with filters
- `GET /invoice/{id}` - Get full invoice details
- `POST /query` - Structured queries (totals by vendor/project)
- `POST /agent` - Conversational queries ("How much did X spend?")

## Visual Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    EMAIL INBOX (Gmail)                      │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              INGESTION SERVICE (Polling)                    │
│  • Fetches unread emails                                    │
│  • Downloads attachments                                    │
│  • Saves to S3                                              │
│  • Enqueues job to Redis                                    │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    S3 STORAGE (MinIO)                       │
│  • inbox/raw/{email_id}.json                                │
│  • inbox/attachments/{email_id}/{filename}                  │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                  REDIS QUEUE (extraction_queue)             │
│  • Job: {email_id, s3_paths, attachments}                  │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              EXTRACTION WORKER                              │
│  1. Downloads email + attachments from S3                   │
│  2. Extracts text (PDF/OCR)                                 │
│  3. Runs regex patterns for field extraction                │
│  4. Calculates confidence scores                            │
│  5. Saves to PostgreSQL (invoices table)                    │
│  6. Saves extraction JSON to S3                             │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              POSTGRESQL DATABASE                            │
│  • invoices table with extracted JSONB                      │
│  • reconciliation_status = 'needs_review'                   │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              RECONCILIATION WORKER                          │
│  1. Fuzzy matches vendor names                              │
│  2. Fuzzy matches project names                             │
│  3. Updates normalized JSONB                                │
│  4. Sets status: 'auto_matched' or 'needs_review'          │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              REVIEW UI (Streamlit)                          │
│  • Manual corrections                                       │
│  • Audit trail                                              │
│  • Training examples                                        │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              API (FastAPI)                                  │
│  • Query invoices                                           │
│  • Conversational agent                                     │
│  • Presigned URLs for attachments                           │
└─────────────────────────────────────────────────────────────┘
```

## Configuration Required

To start processing emails, you need:

### Gmail Setup:
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create project → Enable Gmail API
3. Create OAuth 2.0 credentials (Desktop app)
4. Add to `.env`:
   ```
   GMAIL_CLIENT_ID=your_client_id
   GMAIL_CLIENT_SECRET=your_client_secret
   ```
5. Run ingestion service → Browser opens for OAuth → Save `token.json`


## Example: Processing an Invoice Email

**Step 1: Email arrives**
```
From: billing@acme.com
Subject: Invoice INV-2025-123
Attachment: invoice.pdf
```

**Step 2: Ingestion picks it up**
- Saves email JSON to S3
- Downloads `invoice.pdf` to S3
- Creates Redis job

**Step 3: Extractor processes it**
- Extracts text from PDF
- Finds: "ACME Supplies Pvt Ltd", "INV-2025-123", "$1,210.00"
- Creates database record with confidence scores

**Step 4: Reconciler matches it**
- Matches "ACME Supplies" → "ACME Supplies Pvt Ltd" (92% match)
- Auto-assigns vendor_id = 1
- Sets status = "auto_matched"

**Step 5: Available via API**
```bash
curl -H "Authorization: Bearer dev-api-key" \
  http://localhost:8000/invoices
# Returns invoice with vendor, amount, confidence scores
```

## Monitoring

Check logs to see the pipeline in action:
```bash
# Ingestion logs
docker-compose -f infra/docker-compose.yml logs -f ingestion

# Extractor logs
docker-compose -f infra/docker-compose.yml logs -f extractor

# Reconciler logs
docker-compose -f infra/docker-compose.yml logs -f reconciler
```

## Current Status

- ✅ Ingestion service: Running (needs OAuth setup)
- ✅ Extractor worker: Running (ready to process)
- ✅ Reconciler worker: Running (ready to match)
- ✅ Database: Ready (4 vendors, 4 projects seeded)
- ⏳ Waiting for: Email OAuth credentials to start processing

