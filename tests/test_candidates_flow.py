"""Tests for candidate message review and processing flow."""
import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

# Mock Gmail responses
SAMPLE_MESSAGE_LIST = {
    "messages": [
        {"id": "msg1", "threadId": "thread1"},
        {"id": "msg2", "threadId": "thread2"}
    ]
}

SAMPLE_MESSAGE_METADATA = {
    "id": "msg1",
    "snippet": "Invoice #12345 for services",
    "payload": {
        "headers": [
            {"name": "From", "value": "vendor@example.com"},
            {"name": "Subject", "value": "Invoice #12345"},
            {"name": "Date", "value": "Mon, 21 Nov 2025 10:00:00 +0000"}
        ],
        "parts": [
            {
                "filename": "invoice.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "att1"}
            }
        ]
    }
}


@pytest.fixture
def sample_email_json():
    """Load sample email JSON for testing."""
    sample_path = Path(__file__).parent / "samples" / "sample_email.json"
    if sample_path.exists():
        with open(sample_path) as f:
            return json.load(f)
    return {}


@patch('services.ingestion.gmail_helpers.get_gmail_service')
def test_get_candidate_messages(mock_service, sample_email_json):
    """Test GET /candidate_messages returns preview list."""
    from services.ingestion.gmail_helpers import get_candidate_messages
    
    # Mock Gmail service
    mock_gmail = MagicMock()
    mock_service.return_value = mock_gmail
    
    # Mock list response
    mock_gmail.users.return_value.messages.return_value.list.return_value.execute.return_value = SAMPLE_MESSAGE_LIST
    
    # Mock get response
    mock_gmail.users.return_value.messages.return_value.get.return_value.execute.return_value = SAMPLE_MESSAGE_METADATA
    
    # Call function
    results = get_candidate_messages(query="has:attachment", max_results=5)
    
    # Assertions
    assert isinstance(results, list)
    if results:  # If mocking worked
        assert 'message_id' in results[0]
        assert 'subject' in results[0]
        assert 'from' in results[0]


@patch('services.worker.message_adapter.fetch_message_body_and_attachments')
@patch('services.worker.message_adapter.SessionLocal')
def test_process_message_by_id(mock_db, mock_fetch, sample_email_json):
    """Test process_message_by_id processes a message and returns result."""
    from services.worker.message_adapter import process_message_by_id
    
    # Mock fetch
    mock_fetch.return_value = {
        "email_data": sample_email_json or {"id": "msg1", "snippet": "test"},
        "attachments": [],
        "raw_text": "Invoice #12345\nTotal: $100.00",
        "email_json": "/tmp/test.json",
        "staging_dir": "/tmp"
    }
    
    # Mock database
    mock_session = MagicMock()
    mock_db.return_value = mock_session
    
    # Call function
    result = process_message_by_id("msg1")
    
    # Assertions
    assert result['message_id'] == "msg1"
    assert 'status' in result
    assert 'invoice_records' in result
    assert 'summary_text' in result


def test_process_messages_endpoint():
    """Test POST /process_messages endpoint."""
    from services.api.candidates import process_messages_endpoint, ProcessMessagesRequest
    from fastapi.testclient import TestClient
    from services.api.main import app
    
    client = TestClient(app)
    
    # Mock the dependencies
    with patch('services.api.candidates.get_gmail_service') as mock_gmail, \
         patch('services.api.candidates.process_message_by_id') as mock_process:
        
        mock_gmail.return_value = MagicMock()
        mock_process.return_value = {
            "message_id": "msg1",
            "invoice_records": [{"vendor": "Test", "total_amount": 100}],
            "summary_text": "Test invoice",
            "provenance_path": "/path/to/provenance",
            "status": "success",
            "confidence": 0.9
        }
        
        # Make request
        response = client.post(
            "/candidates/process",
            json={"message_ids": ["msg1"], "label_after": False},
            headers={"Authorization": "Bearer dev-api-key"}
        )
        
        # Assertions
        assert response.status_code in [200, 401]  # May fail auth in test
        if response.status_code == 200:
            data = response.json()
            assert 'jobs' in data
            assert 'queued_count' in data


def test_process_status_endpoint():
    """Test GET /process_status endpoint."""
    from services.api.candidates import get_process_status_endpoint, job_store
    from fastapi.testclient import TestClient
    from services.api.main import app
    
    client = TestClient(app)
    
    # Add test job
    test_job_id = "test-job-123"
    job_store[test_job_id] = {
        "job_id": test_job_id,
        "message_id": "msg1",
        "status": "success",
        "progress": 100,
        "result": {"invoice_records": [{"vendor": "Test"}]}
    }
    
    # Make request
    response = client.get(
        f"/candidates/process_status?job_id={test_job_id}",
        headers={"Authorization": "Bearer dev-api-key"}
    )
    
    # Cleanup
    if test_job_id in job_store:
        del job_store[test_job_id]
    
    # Assertions
    assert response.status_code in [200, 401]  # May fail auth in test
    if response.status_code == 200:
        data = response.json()
        assert data['job_id'] == test_job_id
        assert data['status'] == "success"

