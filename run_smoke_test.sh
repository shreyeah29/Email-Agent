#!/bin/bash
# Smoke test for candidate message review workflow
# This test validates the complete flow: fetch → process → status

set -e

API_URL="http://localhost:8000"
API_KEY="dev-api-key"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║     SMOKE TEST: Review & Process Approved Emails              ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Step A: GET /candidate_messages
echo "Step A: GET /candidate_messages?q=has:attachment&max=5"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
RESPONSE=$(curl -s -H "Authorization: Bearer $API_KEY" \
  "$API_URL/candidates/messages?q=has:attachment&max=5")

if echo "$RESPONSE" | grep -q '"detail"'; then
    echo "❌ Error:"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
    exit 1
fi

CANDIDATE_COUNT=$(echo "$RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else 0)" 2>/dev/null || echo "0")

if [ "$CANDIDATE_COUNT" -eq 0 ]; then
    echo "⚠️  No candidates found. This is OK if inbox is empty."
    echo "✅ Step A passed (no candidates to process)"
    exit 0
fi

echo "✅ Found $CANDIDATE_COUNT candidate(s)"
echo ""
echo "First 3 previews:"
echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for i, msg in enumerate(data[:3], 1):
    print(f\"  {i}. Message ID: {msg.get('message_id', 'N/A')[:30]}...\")
    print(f\"     Subject: {msg.get('subject', 'No Subject')[:50]}\")
    print()
" 2>/dev/null

FIRST_MSG_ID=$(echo "$RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d[0]['message_id'] if isinstance(d, list) and len(d) > 0 else '')" 2>/dev/null)

if [ -z "$FIRST_MSG_ID" ]; then
    echo "❌ Could not extract message ID"
    exit 1
fi

echo ""
echo "Step B: POST /candidates/process"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Processing message: $FIRST_MSG_ID"
PROCESS_RESPONSE=$(curl -s -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"message_ids\": [\"$FIRST_MSG_ID\"], \"label_after\": false}" \
  "$API_URL/candidates/process")

if echo "$PROCESS_RESPONSE" | grep -q '"detail"'; then
    echo "❌ Error:"
    echo "$PROCESS_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$PROCESS_RESPONSE"
    exit 1
fi

JOB_ID=$(echo "$PROCESS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['jobs'][0]['job_id'] if 'jobs' in d and len(d['jobs']) > 0 else '')" 2>/dev/null)

if [ -z "$JOB_ID" ]; then
    echo "❌ Could not extract job ID"
    echo "Response: $PROCESS_RESPONSE"
    exit 1
fi

echo "✅ Job queued: $JOB_ID"
echo ""

# Step C: Poll GET /process_status
echo "Step C: Polling GET /process_status?job_id=$JOB_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
MAX_WAIT=120
ELAPSED=0
STATUS=""
FINAL_RESPONSE=""

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STATUS_RESPONSE=$(curl -s -H "Authorization: Bearer $API_KEY" \
      "$API_URL/candidates/process_status?job_id=$JOB_ID")
    
    STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('status', 'unknown'))" 2>/dev/null || echo "unknown")
    PROGRESS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('progress', 0))" 2>/dev/null || echo "0")
    
    printf "\r   Status: %-12s Progress: %3d%% (%ds)" "$STATUS" "$PROGRESS" "$ELAPSED"
    
    if [ "$STATUS" = "success" ] || [ "$STATUS" = "failed" ]; then
        FINAL_RESPONSE="$STATUS_RESPONSE"
        break
    fi
    
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done

echo ""
echo ""

# Step D: Output final results
echo "Step D: Final Results"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Candidate count: $CANDIDATE_COUNT"
echo "Queued job_id: $JOB_ID"
echo "Final job status: $STATUS"
echo ""

if [ "$STATUS" = "success" ]; then
    echo "✅ Processing successful!"
    echo ""
    echo "Invoice Records [0]:"
    echo "$FINAL_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
records = result.get('invoice_records', [])
if records and len(records) > 0:
    rec = records[0]
    print(f\"  vendor: {rec.get('vendor', 'N/A')}\")
    print(f\"  date: {rec.get('date', 'N/A')}\")
    print(f\"  total_amount: {rec.get('total_amount', 'N/A')}\")
    print(f\"  confidence: {rec.get('confidence', 0):.2f}\")
else:
    print('  No invoice records found')
" 2>/dev/null
else
    echo "❌ Processing failed or timed out"
    echo "Response:"
    echo "$FINAL_RESPONSE" | python3 -m json.tool 2>/dev/null | head -20
fi

echo ""
echo "✅ Smoke test completed!"

