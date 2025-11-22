# Testing Guide: Review & Process Approved Emails

## Prerequisites

1. **Ensure Gmail token is valid**:
   ```bash
   # If token.json doesn't exist or is expired, generate it:
   python3 get_gmail_token.py
   ```

2. **Start required services**:
   ```bash
   docker-compose -f infra/docker-compose.yml up -d api postgres redis minio extractor
   ```

3. **Verify services are running**:
   ```bash
   docker-compose -f infra/docker-compose.yml ps
   ```

## Testing Methods

### Method 1: Test via API (Recommended)

#### Step 1: Test GET /candidates/messages

```bash
curl -H "Authorization: Bearer dev-api-key" \
  "http://localhost:8000/candidates/messages?q=has:attachment&max=5" | \
  python3 -m json.tool
```

**Expected**: Returns array of message previews with:
- `message_id`
- `subject`
- `from`
- `date`
- `snippet`
- `has_attachment`
- `attachment_filenames`

#### Step 2: Test POST /candidates/process

```bash
# First, get a message ID from step 1
MESSAGE_ID="your_message_id_here"

curl -X POST \
  -H "Authorization: Bearer dev-api-key" \
  -H "Content-Type: application/json" \
  -d "{\"message_ids\": [\"$MESSAGE_ID\"], \"label_after\": false}" \
  http://localhost:8000/candidates/process | \
  python3 -m json.tool
```

**Expected**: Returns job IDs:
```json
{
  "jobs": [{"job_id": "...", "message_id": "...", "status": "queued"}],
  "queued_count": 1
}
```

#### Step 3: Test GET /process_status

```bash
# Use job_id from step 2
JOB_ID="your_job_id_here"

curl -H "Authorization: Bearer dev-api-key" \
  "http://localhost:8000/candidates/process_status?job_id=$JOB_ID" | \
  python3 -m json.tool
```

**Expected**: Returns job status with results:
```json
{
  "job_id": "...",
  "message_id": "...",
  "status": "success",
  "progress": 100,
  "result": {
    "invoice_records": [...],
    "summary_text": "...",
    "confidence": 0.85
  }
}
```

### Method 2: Test via Streamlit UI

#### Step 1: Start the Review UI

```bash
streamlit run services/ui/review_candidates.py
```

#### Step 2: Use the UI

1. Open browser: `http://localhost:8501`
2. Enter password: `admin123`
3. Enter Gmail query (e.g., `has:attachment` or `subject:invoice`)
4. Click "Find Candidates"
5. Review the list of candidate emails
6. Select emails you want to process (checkboxes)
7. Click "Process Selected Messages"
8. Watch progress and view results

### Method 3: Run Automated Smoke Test

```bash
./smoke_test.sh
```

This script automatically:
- Fetches candidate messages
- Processes the first one
- Polls for status
- Shows final results

## Quick Test Script

Save this as `quick_test.sh`:

```bash
#!/bin/bash
API_URL="http://localhost:8000"
API_KEY="dev-api-key"

echo "1. Fetching candidates..."
CANDIDATES=$(curl -s -H "Authorization: Bearer $API_KEY" \
  "$API_URL/candidates/messages?q=has:attachment&max=3")

echo "$CANDIDATES" | python3 -m json.tool | head -20

# Extract first message ID
MSG_ID=$(echo "$CANDIDATES" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d[0]['message_id'] if isinstance(d, list) and len(d) > 0 else '')")

if [ -n "$MSG_ID" ]; then
    echo ""
    echo "2. Processing message: $MSG_ID"
    RESULT=$(curl -s -X POST \
      -H "Authorization: Bearer $API_KEY" \
      -H "Content-Type: application/json" \
      -d "{\"message_ids\": [\"$MSG_ID\"], \"label_after\": false}" \
      "$API_URL/candidates/process")
    
    echo "$RESULT" | python3 -m json.tool
    
    JOB_ID=$(echo "$RESULT" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['jobs'][0]['job_id'] if 'jobs' in d and len(d['jobs']) > 0 else '')")
    
    if [ -n "$JOB_ID" ]; then
        echo ""
        echo "3. Checking status for job: $JOB_ID"
        sleep 5
        curl -s -H "Authorization: Bearer $API_KEY" \
          "$API_URL/candidates/process_status?job_id=$JOB_ID" | \
          python3 -m json.tool
    fi
fi
```

## Troubleshooting

### Issue: "Gmail credentials not found"
**Solution**: Ensure `token.json` exists and is valid, or set `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` in `.env`

### Issue: "could not locate runnable browser"
**Solution**: Token needs refresh. Run `python3 get_gmail_token.py` locally (not in Docker)

### Issue: "No candidates found"
**Solution**: Try different Gmail queries:
- `has:attachment`
- `subject:invoice`
- `from:vendor@example.com`
- `is:unread has:attachment`

### Issue: API returns 401
**Solution**: Check API key matches `.env` file (default: `dev-api-key`)

## Expected Results

After processing, you should see:
- ✅ Invoice records in database
- ✅ Extracted fields (vendor, date, amount)
- ✅ Confidence scores
- ✅ Attachments stored in S3
- ✅ Job status shows "success"

Check the dashboard at `http://localhost:8501` to see processed invoices!

