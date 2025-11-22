#!/bin/bash
# Smoke test for candidate message review flow

set -e

API_URL="http://localhost:8000"
API_KEY="dev-api-key"

echo "=== SMOKE TEST: Candidate Message Review Flow ==="
echo ""

# Step A: GET /candidate_messages
echo "Step A: Fetching candidate messages..."
RESPONSE=$(curl -s -H "Authorization: Bearer $API_KEY" \
  "$API_URL/candidates/messages?q=has:attachment&max=5")

if echo "$RESPONSE" | grep -q "detail"; then
    echo "❌ Error: $RESPONSE"
    exit 1
fi

CANDIDATES=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data) if isinstance(data, list) else 0)")
echo "✅ Found $CANDIDATES candidate message(s)"

if [ "$CANDIDATES" -gt 0 ]; then
    FIRST_MSG_ID=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data[0]['message_id'] if isinstance(data, list) and len(data) > 0 else '')")
    FIRST_SUBJECT=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data[0].get('subject', 'N/A') if isinstance(data, list) and len(data) > 0 else 'N/A')")
    
    echo "   First message ID: $FIRST_MSG_ID"
    echo "   First subject: $FIRST_SUBJECT"
    echo ""
    
    # Step B: POST /process_messages
    echo "Step B: Processing first message..."
    PROCESS_RESPONSE=$(curl -s -X POST \
      -H "Authorization: Bearer $API_KEY" \
      -H "Content-Type: application/json" \
      -d "{\"message_ids\": [\"$FIRST_MSG_ID\"], \"label_after\": false}" \
      "$API_URL/candidates/process")
    
    JOB_ID=$(echo "$PROCESS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['jobs'][0]['job_id'] if 'jobs' in data and len(data['jobs']) > 0 else '')")
    
    if [ -z "$JOB_ID" ]; then
        echo "❌ Error: No job ID returned"
        echo "Response: $PROCESS_RESPONSE"
        exit 1
    fi
    
    echo "✅ Job queued: $JOB_ID"
    echo ""
    
    # Step C: Poll GET /process_status
    echo "Step C: Polling job status (max 2 minutes)..."
    MAX_WAIT=120
    ELAPSED=0
    STATUS=""
    
    while [ $ELAPSED -lt $MAX_WAIT ]; do
        STATUS_RESPONSE=$(curl -s -H "Authorization: Bearer $API_KEY" \
          "$API_URL/candidates/process_status?job_id=$JOB_ID")
        
        STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('status', 'unknown'))")
        
        echo "   Status: $STATUS (${ELAPSED}s elapsed)"
        
        if [ "$STATUS" = "success" ] || [ "$STATUS" = "failed" ]; then
            break
        fi
        
        sleep 5
        ELAPSED=$((ELAPSED + 5))
    done
    
    echo ""
    
    # Step D: Print final results
    echo "Step D: Final Results"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Candidate count: $CANDIDATES"
    echo "Job ID: $JOB_ID"
    echo "Final status: $STATUS"
    
    if [ "$STATUS" = "success" ]; then
        INVOICE_RECORDS=$(echo "$STATUS_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
records = result.get('invoice_records', [])
if records:
    rec = records[0]
    print(f\"Vendor: {rec.get('vendor', 'N/A')}\")
    print(f\"Date: {rec.get('date', 'N/A')}\")
    print(f\"Total: {rec.get('total_amount', 'N/A')}\")
    print(f\"Confidence: {rec.get('confidence', 0):.2f}\")
else:
    print('No invoice records found')
")
        echo "$INVOICE_RECORDS"
    else
        echo "❌ Processing failed or timed out"
    fi
    
    echo ""
    echo "✅ Smoke test completed!"
else
    echo "⚠️  No candidates found - this is OK if inbox is empty"
    echo "✅ Smoke test completed (no candidates to process)"
fi

