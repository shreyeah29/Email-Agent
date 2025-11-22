# Review & Process Approved Emails - Implementation Summary

## Files Created

1. **services/ingestion/gmail_helpers.py**
   - `get_gmail_service()` - Authenticates with Gmail, reuses existing token
   - `get_candidate_messages()` - Fetches message metadata previews
   - `fetch_message_body_and_attachments()` - Downloads message and attachments
   - `apply_label()` - Non-destructively applies Gmail label

2. **services/worker/message_adapter.py**
   - `process_message_by_id()` - Processes single message through extraction pipeline
   - Integrates with existing extractor
   - Stores results in database and S3

3. **services/api/candidates.py**
   - `GET /candidates/messages` - List candidate emails
   - `POST /candidates/process` - Process selected message IDs
   - `GET /candidates/process_status` - Track job progress

4. **services/ui/review_candidates.py**
   - Streamlit UI for reviewing and selecting emails
   - Search, preview, select, and process interface
   - Progress tracking and results display

5. **tests/test_candidates_flow.py**
   - Unit tests for candidate flow
   - Mocked Gmail responses

6. **smoke_test.sh**
   - End-to-end smoke test script

## Files Modified

1. **services/api/main.py**
   - Added candidates router import and inclusion

2. **README.md**
   - Added "Review & Process Approved Emails" section with usage instructions

3. **.gitignore**
   - Added patterns for token files and client secrets

4. **infra/docker-compose.yml**
   - Added token.json mount for API service
   - Added Gmail env vars for API service

## Features

✅ **Selective Processing**: Only processes explicitly selected message IDs
✅ **Non-Destructive**: Label-only operations, no message deletion
✅ **Token Reuse**: Uses existing Gmail OAuth token
✅ **Additive**: No changes to existing ingestion flows
✅ **Progress Tracking**: Real-time job status monitoring
✅ **Metadata Preview**: Shows email info without downloading attachments

## API Endpoints

- `GET /candidates/messages?q=<query>&max=<number>` - Fetch candidate previews
- `POST /candidates/process` - Process selected messages
- `GET /candidates/process_status?job_id=<id>` - Get job status

## Usage

1. **Via Streamlit UI**:
   ```bash
   streamlit run services/ui/review_candidates.py
   ```

2. **Via API**:
   ```bash
   curl -H "Authorization: Bearer dev-api-key" \
     http://localhost:8000/candidates/messages?q=has:attachment
   ```

## Safety Guarantees

- ✅ Only processes explicitly selected message IDs
- ✅ No automatic inbox scanning
- ✅ Non-destructive (label only, no deletion)
- ✅ Reuses existing Gmail OAuth token
- ✅ Existing ingestion flows unchanged

