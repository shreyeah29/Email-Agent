"""Tests for Sync Inbox feature.

Additive — does not modify existing behavior.
Tests the receipts-only inbox synchronization functionality.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi.testclient import TestClient

from services.api.sync_inbox import router, SyncRequest, SyncResponse
from services.api.main import app


@pytest.fixture
def client():
    """Test client for API."""
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def mock_gmail_service():
    """Mock Gmail service."""
    service = MagicMock()
    return service


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    db = MagicMock()
    return db


@patch('services.api.sync_inbox.build_gmail_service')
@patch('services.api.sync_inbox.search_messages')
@patch('services.api.sync_inbox.download_message_and_attachments')
@patch('services.api.sync_inbox.process_message_by_id')
@patch('services.api.sync_inbox.apply_label')
def test_sync_inbox_success(
    mock_apply_label,
    mock_process_message,
    mock_download,
    mock_search,
    mock_build_service,
    client,
    mock_db_session
):
    """Test successful sync inbox operation."""
    # Setup mocks
    mock_service = MagicMock()
    mock_build_service.return_value = mock_service
    
    # Mock search to return 2 message IDs
    mock_search.return_value = ['msg1', 'msg2']
    
    # Mock download
    mock_download.return_value = {
        'raw_email_path': '/tmp/msg1.json',
        'attachments': [
            {'filename': 'invoice.pdf', 'mime': 'application/pdf', 'path': '/tmp/invoice.pdf', 'size': 1024}
        ],
        'headers': {'From': 'test@example.com', 'Subject': 'Invoice', 'Date': '2025-01-01'}
    }
    
    # Mock processing
    mock_process_message.return_value = {
        'status': 'success',
        'invoice_records': [{
            'vendor': 'Test Vendor',
            'date': '2025-01-01',
            'total_amount': 100.0,
            'currency': 'USD',
            'line_items': [],
            'confidence': 0.9
        }],
        'summary_text': 'Processed successfully',
        'provenance_path': 'inbox/extraction/invoice1.json',
        'confidence': 0.9,
        'invoice_id': 'invoice-uuid-1',
        'already_processed': False
    }
    
    # Mock database query (no existing invoice)
    with patch('services.api.sync_inbox.get_db') as mock_get_db:
        mock_get_db.return_value.__enter__.return_value = mock_db_session
        mock_db_session.query.return_value.filter.return_value.first.return_value = None
        
        # Make request
        response = client.post(
            "/sync_inbox",
            json={"max": 100},
            headers={"Authorization": "Bearer dev-api-key"}
        )
    
    # Assertions
    assert response.status_code == 200
    data = response.json()
    assert data['total_found'] == 2
    assert data['processed'] == 2
    assert data['new_invoices'] == 2
    assert data['errors'] == 0
    
    # Verify mocks were called
    mock_build_service.assert_called_once()
    mock_search.assert_called_once()
    assert mock_download.call_count == 2
    assert mock_process_message.call_count == 2
    assert mock_apply_label.call_count == 2


@patch('services.api.sync_inbox.build_gmail_service')
def test_sync_inbox_no_credentials(mock_build_service, client):
    """Test sync inbox when credentials are missing."""
    mock_build_service.side_effect = ValueError("GMAIL_CLIENT_SECRETS_PATH must be set")
    
    response = client.post(
        "/sync_inbox",
        json={"max": 100},
        headers={"Authorization": "Bearer dev-api-key"}
    )
    
    assert response.status_code == 500
    assert "Sync failed" in response.json()['detail']


@patch('services.api.sync_inbox.build_gmail_service')
@patch('services.api.sync_inbox.search_messages')
def test_sync_inbox_no_messages(mock_search, mock_build_service, client, mock_db_session):
    """Test sync inbox when no messages are found."""
    mock_service = MagicMock()
    mock_build_service.return_value = mock_service
    mock_search.return_value = []
    
    with patch('services.api.sync_inbox.get_db') as mock_get_db:
        mock_get_db.return_value.__enter__.return_value = mock_db_session
        
        response = client.post(
            "/sync_inbox",
            json={"max": 100},
            headers={"Authorization": "Bearer dev-api-key"}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data['total_found'] == 0
    assert data['processed'] == 0
    assert data['new_invoices'] == 0


@patch('services.api.sync_inbox.build_gmail_service')
@patch('services.api.sync_inbox.search_messages')
@patch('services.api.sync_inbox.download_message_and_attachments')
@patch('services.api.sync_inbox.process_message_by_id')
def test_sync_inbox_skips_already_processed(
    mock_process_message,
    mock_download,
    mock_search,
    mock_build_service,
    client,
    mock_db_session
):
    """Test sync inbox skips already processed messages."""
    mock_service = MagicMock()
    mock_build_service.return_value = mock_service
    mock_search.return_value = ['msg1']
    
    # Mock database to return existing invoice
    existing_invoice = MagicMock()
    existing_invoice.invoice_id = 'existing-uuid'
    
    with patch('services.api.sync_inbox.get_db') as mock_get_db:
        mock_get_db.return_value.__enter__.return_value = mock_db_session
        mock_db_session.query.return_value.filter.return_value.first.return_value = existing_invoice
        
        response = client.post(
            "/sync_inbox",
            json={"max": 100},
            headers={"Authorization": "Bearer dev-api-key"}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data['skipped'] == 1
    assert data['processed'] == 0
    
    # Verify process_message was not called
    mock_process_message.assert_not_called()

