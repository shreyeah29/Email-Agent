# Email Agent - Automated Invoice Processing System

An automated system that reads shared inboxes (Gmail/Outlook), extracts invoice/receipt fields into flexible JSONB storage, reconciles vendors/projects, provides a review UI, and exposes APIs with a conversational agent.

## Features

- 📧 **Email Ingestion**: Polls Gmail and Microsoft Outlook for new emails
- 📄 **Document Processing**: Extracts text from PDFs (digital and scanned via OCR)
- 🔍 **Field Extraction**: Extracts invoice fields (vendor, date, amount, line items) with confidence scores
- 🔗 **Reconciliation**: Fuzzy matching to auto-match vendors and projects
- ✏️ **Review UI**: Streamlit interface for manual corrections and review
- 🔌 **REST API**: FastAPI endpoints for querying invoices and structured data
- 💬 **Conversational Agent**: Free-text query endpoint for spend analysis
- 📊 **Audit Trail**: Complete history of all field changes

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Gmail/    │────▶│  Ingestion   │────▶│    Redis    │
│   Outlook   │     │   Service    │     │    Queue    │
└─────────────┘     └──────────────┘     └─────────────┘
                                              │
                                              ▼
                                    ┌──────────────┐
                                    │  Extractor   │
                                    │   Worker     │
                                    └──────────────┘
                                              │
                                              ▼
                                    ┌──────────────┐
                                    │  PostgreSQL  │
                                    │   (JSONB)    │
                                    └──────────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
            ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
            │ Reconciler   │         │  FastAPI     │         │  Streamlit   │
            │   Worker     │         │     API      │         │     UI       │
            └──────────────┘         └──────────────┘         └──────────────┘
```

## Tech Stack

- **Python 3.11+**
- **FastAPI** - REST API
- **PostgreSQL 15** - JSONB storage
- **Redis + RQ** - Job queue
- **S3/MinIO** - Object storage
- **Streamlit** - Review UI
- **pdfplumber + pytesseract** - PDF/OCR processing
- **RapidFuzz** - Fuzzy matching

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Python 3.11+ (for local development)
- Tesseract OCR (for local OCR processing)
- Poppler (for PDF processing)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/shreyeah29/Email-Agent.git
   cd Email-Agent
   ```

2. **Create `.env` file**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Start services with Docker Compose**
   ```bash
   make up
   ```

4. **Run database migrations**
   ```bash
   make migrate
   make seed
   ```

5. **Start workers** (in separate terminals)
   ```bash
   make worker      # Extraction worker
   make reconciler  # Reconciliation worker
   make ingestion   # Email ingestion (requires OAuth setup)
   ```

6. **Start the API**
   ```bash
   # API runs automatically in docker-compose, or locally:
   uvicorn services.api.main:app --reload
   ```

7. **Start the Review UI**
   ```bash
   make ui
   # Or: streamlit run services/ui/review.py
   ```

### Gmail OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable Gmail API
4. Create OAuth 2.0 credentials (Desktop app)
5. Download credentials and set in `.env`:
   ```
   GMAIL_CLIENT_ID=your_client_id
   GMAIL_CLIENT_SECRET=your_client_secret
   ```
6. Run ingestion service - it will open browser for OAuth flow
7. Save the generated `token.json` for future use

### Microsoft Outlook Setup

1. Go to [Azure Portal](https://portal.azure.com/)
2. Register a new application
3. Create a client secret
4. Set API permissions: `Mail.Read`
5. Set in `.env`:
   ```
   MICROSOFT_CLIENT_ID=your_client_id
   MICROSOFT_CLIENT_SECRET=your_client_secret
   MICROSOFT_TENANT_ID=your_tenant_id
   ```

## API Endpoints

### Authentication
All endpoints require Bearer token authentication:
```bash
curl -H "Authorization: Bearer dev-api-key" http://localhost:8000/invoices
```

### List Invoices
```bash
GET /invoices?vendor_id=1&page=1&page_size=20
```

### Get Invoice Details
```bash
GET /invoice/{invoice_id}
```

### Structured Query
```bash
POST /query
{
  "type": "total_by_vendor",
  "vendor_id": 1,
  "from_date": "2025-10-01",
  "to_date": "2025-10-31"
}
```

### Conversational Agent
```bash
POST /agent
{
  "text": "How much did Company A spend in October 2025?"
}
```

### Audit Trail
```bash
GET /audit/{invoice_id}
```

## Database Schema

### Core Tables

- **invoices**: Main table with `extracted` (JSONB) and `normalized` (JSONB) fields
- **vendors**: Vendor master data with aliases
- **projects**: Project master data with codes
- **invoice_audit**: Audit trail for all field changes
- **training_examples**: Saved corrections for ML training

### Extracted JSONB Structure

```json
{
  "vendor_name": {
    "value": "ACME Supplies Pvt Ltd",
    "confidence": 0.94,
    "provenance": {
      "attachment": "invoice.pdf",
      "page": 1,
      "snippet": "ACME Supplies Pvt Ltd"
    }
  },
  "invoice_number": {
    "value": "INV-2025-123",
    "confidence": 0.98
  },
  "total_amount": {
    "value": 11210.00,
    "confidence": 0.9,
    "currency": "INR"
  }
}
```

## Testing

```bash
# Run all tests
make test

# Run unit tests only
make test-unit

# Run integration tests only
make test-integration
```

## Development

### Project Structure

```
Email-Agent/
├── infra/
│   ├── docker-compose.yml
│   └── Dockerfiles/
├── services/
│   ├── ingestion/      # Email polling service
│   ├── extractor/      # PDF/OCR extraction worker
│   ├── reconciler/     # Vendor/project matching worker
│   ├── api/            # FastAPI REST API
│   └── ui/             # Streamlit review UI
├── shared/             # Shared models and config
├── migrations/         # SQL migrations
├── tests/              # Unit and integration tests
└── scripts/            # Utility scripts
```

### Running Locally (without Docker)

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Install system dependencies:
   ```bash
   # macOS
   brew install tesseract poppler
   
   # Ubuntu/Debian
   sudo apt-get install tesseract-ocr poppler-utils
   ```

3. Set up PostgreSQL and Redis locally or use Docker:
   ```bash
   docker-compose -f infra/docker-compose.yml up -d postgres redis minio
   ```

4. Run services:
   ```bash
   python services/api/main.py
   python services/extractor/worker.py
   python services/reconciler/worker.py
   streamlit run services/ui/review.py
   ```

## Makefile Commands

- `make up` - Start all Docker services
- `make down` - Stop all Docker services
- `make migrate` - Run database migrations
- `make seed` - Seed sample data
- `make worker` - Run extraction worker
- `make reconciler` - Run reconciliation worker
- `make ui` - Start Streamlit UI
- `make test` - Run all tests
- `make clean` - Clean up Docker volumes and caches

## Demo Script

Run the full demo pipeline:
```bash
./scripts/demo_run.sh
```

This will:
1. Start all Docker services
2. Run migrations
3. Seed sample data
4. Start workers
5. Show API endpoints

## Environment Variables

See `.env.example` for all required environment variables:

- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `S3_ENDPOINT_URL` - S3 endpoint (MinIO for dev)
- `S3_ACCESS_KEY` / `S3_SECRET_KEY` - S3 credentials
- `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` - Gmail OAuth
- `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` - Microsoft OAuth
- `API_KEY` - API authentication key

## Acceptance Criteria

✅ **Ingestion**: Processes sample emails, creates S3 objects, enqueues jobs  
✅ **Extraction**: Extracts fields from PDFs, stores in JSONB with confidence  
✅ **Reconciliation**: Auto-matches vendors with score >= 90  
✅ **Review UI**: Shows invoices, allows edits, creates audit records  
✅ **API**: `/query` returns totals with confidence metadata  
✅ **Agent**: `/agent` handles free-text queries with provenance  
✅ **Tests**: Unit and integration tests pass  

## Future Enhancements

- [ ] RAG-based agent with LLM integration
- [ ] pgvector for semantic search
- [ ] ML model training from audit corrections
- [ ] Webhook support for real-time email ingestion
- [ ] Multi-currency support with conversion
- [ ] Advanced line item extraction
- [ ] Email template detection

## License

MIT License

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## Support

For issues and questions, please open an issue on GitHub.

