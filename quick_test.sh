#!/bin/bash
# Quick test script for candidate message review flow

API_URL="http://localhost:8000"
API_KEY="dev-api-key"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║     QUICK TEST: Review & Process Approved Emails               ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Step 1: Fetch candidates
echo "Step 1: Fetching candidate messages..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
CANDIDATES=$(curl -s -H "Authorization: Bearer $API_KEY" \
  "$API_URL/candidates/messages?q=has:attachment&max=3")

if echo "$CANDIDATES" | grep -q "detail"; then
    echo "❌ Error fetching candidates:"
    echo "$CANDIDATES" | python3 -m json.tool 2>/dev/null || echo "$CANDIDATES"
    exit 1
fi

CANDIDATE_COUNT=$(echo "$CANDIDATES" | python3 -c "import sys, json; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else 0)" 2>/dev/null || echo "0")

if [ "$CANDIDATE_COUNT" -eq 0 ]; then
    echo "⚠️  No candidates found. Try a different query or check your Gmail."
    echo ""
    echo "Try these queries:"
    echo "  - has:attachment"
    echo "  - subject:invoice"
    echo "  - is:unread has:attachment"
    exit 0
fi

echo "✅ Found $CANDIDATE_COUNT candidate(s)"
echo ""

# Show first 3 candidates
echo "First 3 candidates:"
echo "$CANDIDATES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for i, msg in enumerate(data[:3], 1):
    print(f\"  {i}. {msg.get('subject', 'No Subject')[:50]}\")
    print(f\"     From: {msg.get('from', 'Unknown')[:40]}\")
    print(f\"     ID: {msg.get('message_id', 'N/A')[:20]}...\")
    print(f\"     Attachments: {len(msg.get('attachment_filenames', []))}\")
    print()
" 2>/dev/null

# Extract first message ID
MSG_ID=$(echo "$CANDIDATES" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d[0]['message_id'] if isinstance(d, list) and len(d) > 0 else '')" 2>/dev/null)

if [ -z "$MSG_ID" ]; then
    echo "❌ Could not extract message ID"
    exit 1
fi

echo ""
echo "Step 2: Processing message: $MSG_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
RESULT=$(curl -s -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"message_ids\": [\"$MSG_ID\"], \"label_after\": false}" \
  "$API_URL/candidates/process")

if echo "$RESULT" | grep -q "detail"; then
    echo "❌ Error processing message:"
    echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"
    exit 1
fi

echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"
echo ""

JOB_ID=$(echo "$RESULT" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['jobs'][0]['job_id'] if 'jobs' in d and len(d['jobs']) > 0 else '')" 2>/dev/null)

if [ -z "$JOB_ID" ]; then
    echo "❌ Could not extract job ID"
    exit 1
fi

echo ""
echo "Step 3: Polling job status (max 2 minutes)..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
MAX_WAIT=120
ELAPSED=0
STATUS=""

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STATUS_RESPONSE=$(curl -s -H "Authorization: Bearer $API_KEY" \
      "$API_URL/candidates/process_status?job_id=$JOB_ID")
    
    STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('status', 'unknown'))" 2>/dev/null)
    PROGRESS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('progress', 0))" 2>/dev/null)
    
    printf "\r   Status: %-10s Progress: %3d%% (%ds elapsed)" "$STATUS" "$PROGRESS" "$ELAPSED"
    
    if [ "$STATUS" = "success" ] || [ "$STATUS" = "failed" ]; then
        break
    fi
    
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done

echo ""
echo ""

# Step 4: Show final results
echo "Step 4: Final Results"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Candidate count: $CANDIDATE_COUNT"
echo "Job ID: $JOB_ID"
echo "Final status: $STATUS"
echo ""

if [ "$STATUS" = "success" ]; then
    echo "✅ Processing successful!"
    echo ""
    echo "Invoice Records:"
    echo "$STATUS_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
records = result.get('invoice_records', [])
if records:
    for i, rec in enumerate(records, 1):
        print(f\"  Record {i}:\")
        print(f\"    Vendor: {rec.get('vendor', 'N/A')}\")
        print(f\"    Date: {rec.get('date', 'N/A')}\")
        print(f\"    Total: {rec.get('currency', '')} {rec.get('total_amount', 'N/A')}\")
        print(f\"    Confidence: {rec.get('confidence', 0):.2f}\")
        print()
else:
    print('  No invoice records found')
" 2>/dev/null
    
    echo "Summary:"
    echo "$STATUS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(f\"  {d.get('result', {}).get('summary_text', 'N/A')}\")" 2>/dev/null
else
    echo "❌ Processing failed or timed out"
    echo "Response:"
    echo "$STATUS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$STATUS_RESPONSE"
fi

echo ""
echo "✅ Test completed!"
echo ""
echo "View processed invoices in dashboard: http://localhost:8501"

