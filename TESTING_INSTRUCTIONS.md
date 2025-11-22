# 🧪 Testing Guide: Review & Process Approved Emails

## Prerequisites

1. **Services must be running:**
   ```bash
   docker-compose -f infra/docker-compose.yml ps
   ```
   You should see: `api`, `postgres`, `redis`, `extractor` running

2. **Gmail token must exist:**
   ```bash
   ls -la token.json
   ```
   If it doesn't exist, generate it:
   ```bash
   python3 get_gmail_token.py
   ```

---

## Method 1: Quick Automated Test (Easiest)

Run the automated smoke test script:

```bash
./run_smoke_test.sh
```

This will:
- ✅ Fetch candidate messages from Gmail
- ✅ Process the first one
- ✅ Show you the results

**Expected output:**
- Candidate count
- Job ID
- Processing status
- Invoice records (vendor, date, amount, confidence)

---

## Method 2: Visual UI Test (Recommended)

### Step 1: Start the Review UI

```bash
streamlit run services/ui/review_candidates.py
```

### Step 2: Open Browser

Navigate to: `http://localhost:8501`

### Step 3: Login

- Password: `admin123` (or check your `.env` for `UI_PASSWORD`)

### Step 4: Use the Interface

1. **Enter a Gmail query** (or use presets):
   - `has:attachment` - All emails with attachments
   - `subject:invoice` - Emails with "invoice" in subject
   - `has:attachment subject:(invoice OR receipt OR bill)` - Combined

2. **Click "🔍 Find Candidates"**

3. **Review the list:**
   - See subject, from, date
   - Click "👁️ Preview" to see full snippet
   - Check attachment info

4. **Select emails to process:**
   - Check boxes next to emails you want
   - Use "✅ Select All" / "❌ Deselect All" for bulk

5. **Process selected:**
   - Optionally check "Apply 'ProcessedByAgent' label"
   - Click "🚀 Process Selected Messages"

6. **Watch progress:**
   - See real-time status updates
   - View extracted invoice records
   - Check confidence scores

---

## Method 3: API Test (Command Line)

### Step 1: Fetch Candidate Messages

```bash
curl -H "Authorization: Bearer dev-api-key" \
  "http://localhost:8000/candidates/messages?q=has:attachment&max=5" | \
  python3 -m json.tool
```

**Expected:** Array of message previews with:
- `message_id`
- `subject`
- `from`
- `date`
- `snippet`
- `has_attachment`
- `attachment_filenames`

### Step 2: Process a Message

Copy a `message_id` from Step 1, then:

```bash
MESSAGE_ID="paste_message_id_here"

curl -X POST \
  -H "Authorization: Bearer dev-api-key" \
  -H "Content-Type: application/json" \
  -d "{\"message_ids\": [\"$MESSAGE_ID\"], \"label_after\": false}" \
  http://localhost:8000/candidates/process | \
  python3 -m json.tool
```

**Expected:** Job IDs:
```json
{
  "jobs": [{"job_id": "...", "message_id": "...", "status": "queued"}],
  "queued_count": 1
}
```

### Step 3: Check Job Status

Copy the `job_id` from Step 2, then:

```bash
JOB_ID="paste_job_id_here"

curl -H "Authorization: Bearer dev-api-key" \
  "http://localhost:8000/candidates/process_status?job_id=$JOB_ID" | \
  python3 -m json.tool
```

**Expected:** Job status with results:
```json
{
  "job_id": "...",
  "status": "success",
  "progress": 100,
  "result": {
    "invoice_records": [...],
    "summary_text": "...",
    "confidence": 0.85
  }
}
```

---

## Method 4: Swagger UI (Interactive)

1. **Open Swagger UI:**
   ```
   http://localhost:8000/docs
   ```

2. **Authorize:**
   - Click "Authorize" button (top right)
   - Enter: `dev-api-key`
   - Click "Authorize"

3. **Test endpoints:**
   - Find "candidates" section
   - `GET /candidates/messages` - Click "Try it out"
   - `POST /candidates/process` - Click "Try it out"
   - `GET /candidates/process_status` - Click "Try it out"

---

## Troubleshooting

### ❌ "Gmail credentials not found"

**Solution:**
```bash
# Generate token locally
python3 get_gmail_token.py

# Make sure token.json exists
ls -la token.json
```

### ❌ "could not locate runnable browser"

**Solution:** This is expected in Docker. The token must be generated locally first, then mounted in Docker.

### ❌ "No candidates found"

**Solution:** Try different queries:
- `has:attachment`
- `subject:invoice`
- `is:unread has:attachment`
- `from:vendor@example.com`

### ❌ API returns 401

**Solution:** Check API key matches `.env`:
```bash
grep API_KEY .env
# Should show: API_KEY=dev-api-key
```

### ❌ Services not running

**Solution:**
```bash
# Start services
docker-compose -f infra/docker-compose.yml up -d api postgres redis extractor

# Check status
docker-compose -f infra/docker-compose.yml ps
```

---

## What to Expect

After successful processing:

✅ **Database:** Invoice records in `invoices` table
✅ **S3:** Raw email JSON and attachments stored
✅ **Extracted Fields:** Vendor, date, amount, line items
✅ **Confidence Scores:** Quality indicators (0.0 - 1.0)
✅ **Job Status:** Shows "success" with results

**View processed invoices:**
```bash
# Via API
curl -H "Authorization: Bearer dev-api-key" \
  http://localhost:8000/invoices | python3 -m json.tool

# Or via dashboard
streamlit run services/ui/dashboard.py
# Then open: http://localhost:8501
```

---

## Quick Test Script

Save this as `test_quick.sh`:

```bash
#!/bin/bash
API_URL="http://localhost:8000"
API_KEY="dev-api-key"

echo "1️⃣ Fetching candidates..."
curl -s -H "Authorization: Bearer $API_KEY" \
  "$API_URL/candidates/messages?q=has:attachment&max=3" | \
  python3 -m json.tool | head -30

echo ""
echo "2️⃣ Copy a message_id from above and run:"
echo "   MESSAGE_ID='your_id_here'"
echo "   curl -X POST -H 'Authorization: Bearer $API_KEY' \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"message_ids\": [\"\$MESSAGE_ID\"], \"label_after\": false}' \\"
echo "     $API_URL/candidates/process | python3 -m json.tool"
```

Run it:
```bash
chmod +x test_quick.sh
./test_quick.sh
```

---

## Success Indicators

✅ **API responds** with candidate messages
✅ **Processing completes** with status "success"
✅ **Invoice records** appear in database
✅ **Confidence scores** are reasonable (>0.5)
✅ **No errors** in logs

---

## Next Steps

After testing:
1. View processed invoices in dashboard
2. Check extraction quality
3. Review confidence scores
4. Process more emails as needed

