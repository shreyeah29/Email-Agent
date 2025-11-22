# Quick Start Guide

## Prerequisites

Before running the system, ensure you have:

1. **Docker and Docker Compose** installed
   - macOS: `brew install docker docker-compose`
   - Linux: Follow [Docker installation guide](https://docs.docker.com/get-docker/)
   - Windows: Install [Docker Desktop](https://www.docker.com/products/docker-desktop)

2. **Python 3.11+** (for local development, optional)
   - Check: `python3 --version`

3. **Git** (already have it if you cloned the repo)

## Option 1: Run with Docker (Recommended)

This is the easiest way to run everything.

### Step 1: Navigate to Project Directory

```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
```

### Step 2: Create Environment File

```bash
# Copy the example env file
cp .env.example .env

# Edit .env with your credentials (optional for basic testing)
# For now, you can use the defaults for local development
```

### Step 3: Start All Services

```bash
make up
```

This starts:
- PostgreSQL database (port 5432)
- Redis (port 6379)
- MinIO S3 storage (ports 9000, 9001)
- FastAPI service (port 8000)

**Wait 10-15 seconds** for services to start up.

### Step 4: Run Database Migrations

```bash
make migrate
make seed
```

This creates the database tables and seeds sample vendors/projects.

### Step 5: Verify Services Are Running

```bash
# Check Docker containers
docker ps

# Test API health endpoint
curl http://localhost:8000/health
```

You should see: `{"status":"healthy"}`

### Step 6: Start Workers (in separate terminals)

Open **3 new terminal windows/tabs** and run:

**Terminal 1 - Extraction Worker:**
```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
make worker
```

**Terminal 2 - Reconciliation Worker:**
```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
make reconciler
```

**Terminal 3 - Ingestion Service (optional, requires OAuth setup):**
```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
make ingestion
```

### Step 7: Start Review UI (optional)

In another terminal:
```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
make ui
```

Then open: http://localhost:8501

**Password:** `admin123` (default, set in .env)

## Option 2: Run Locally (Without Docker)

If you prefer to run services locally:

### Step 1: Install System Dependencies

**macOS:**
```bash
brew install postgresql redis tesseract poppler
brew services start postgresql
brew services start redis
```

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y postgresql redis-server tesseract-ocr poppler-utils
sudo systemctl start postgresql
sudo systemctl start redis
```

### Step 2: Install Python Dependencies

```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
pip install -r requirements.txt
```

### Step 3: Set Up Database

```bash
# Create database
createdb invoices

# Run migrations
psql invoices -f migrations/001_create_core_tables.sql
psql invoices -f migrations/002_seed_sample_data.sql
```

### Step 4: Set Up MinIO (S3) or Use AWS S3

**Option A: Use MinIO locally:**
```bash
docker run -d -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data --console-address ":9001"
```

**Option B: Use AWS S3:**
Update `.env` with your AWS credentials and remove `S3_ENDPOINT_URL`.

### Step 5: Update .env File

```bash
# Edit .env to point to local services
DATABASE_URL=postgresql://your_user@localhost:5432/invoices
REDIS_URL=redis://localhost:6379/0
S3_ENDPOINT_URL=http://localhost:9000
```

### Step 6: Run Services

**Terminal 1 - API:**
```bash
uvicorn services.api.main:app --reload --port 8000
```

**Terminal 2 - Extraction Worker:**
```bash
python services/extractor/worker.py
```

**Terminal 3 - Reconciliation Worker:**
```bash
python services/reconciler/worker.py
```

**Terminal 4 - Review UI:**
```bash
streamlit run services/ui/review.py --server.port 8501
```

## Testing the System

### 1. Test API Endpoints

```bash
# Health check
curl http://localhost:8000/health

# List invoices (requires API key)
curl -H "Authorization: Bearer dev-api-key" \
  http://localhost:8000/invoices

# View API documentation
open http://localhost:8000/docs
```

### 2. Test Conversational Agent

```bash
curl -X POST \
  -H "Authorization: Bearer dev-api-key" \
  -H "Content-Type: application/json" \
  -d '{"text": "How much did ACME spend in October 2025?"}' \
  http://localhost:8000/agent
```

### 3. Access Services

- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **Streamlit UI**: http://localhost:8501
- **MinIO Console**: http://localhost:9001 (minioadmin/minioadmin)

## Using the Demo Script

For a quick automated setup:

```bash
cd "/Users/sweety/Desktop/Email Agent/Email-Agent"
./scripts/demo_run.sh
```

This script will:
1. Start all Docker services
2. Run migrations
3. Seed sample data
4. Start workers
5. Show you how to test the API

## Common Commands

```bash
# Start all services
make up

# Stop all services
make down

# View logs
make logs

# Run tests
make test

# Clean everything (removes volumes)
make clean

# Rebuild Docker images
make build
```

## Troubleshooting

### Port Already in Use

If you get port conflicts:
```bash
# Check what's using the port
lsof -i :8000  # For API
lsof -i :5432  # For PostgreSQL

# Stop conflicting services or change ports in docker-compose.yml
```

### Database Connection Errors

```bash
# Check if PostgreSQL is running
docker ps | grep postgres

# Check logs
docker-compose -f infra/docker-compose.yml logs postgres
```

### S3/MinIO Errors

```bash
# Ensure MinIO is running
docker ps | grep minio

# Access MinIO console: http://localhost:9001
# Create bucket: inbox-bucket
```

### Workers Not Processing Jobs

```bash
# Check Redis is running
docker ps | grep redis

# Check worker logs
docker-compose -f infra/docker-compose.yml logs extractor
```

## Next Steps

1. **Set up Gmail OAuth** (see README.md for Gmail setup)
2. **Add sample invoices** via the ingestion service
3. **Review extracted invoices** in the Streamlit UI
4. **Test the API** with the examples in API_EXAMPLES.md

## Where to Run Commands

All commands should be run from the project root directory:
```bash
/Users/sweety/Desktop/Email Agent/Email-Agent
```

You can verify you're in the right place:
```bash
pwd
# Should show: /Users/sweety/Desktop/Email Agent/Email-Agent

ls
# Should show: README.md, Makefile, services/, infra/, etc.
```

