"""Integration tests for the full pipeline."""
import pytest
import json
import uuid
from services.extractor.worker import InvoiceExtractor, process_extraction_job
from services.reconciler.worker import Reconciler
from shared.models import Invoice, Vendor


class TestPipeline:
    """Test end-to-end pipeline."""
    
    def test_extraction_job_creates_invoice(self, db_session, monkeypatch):
        """Test that extraction job creates invoice record."""
        # Mock S3 client
        class MockS3:
            def get_object(self, Bucket, Key):
                class MockBody:
                    def read(self):
                        return json.dumps({
                            "id": "test_email_123",
                            "payload": {
                                "body": {
                                    "data": "dGVzdCBib2R5"  # base64 "test body"
                                }
                            }
                        }).encode()
                return {"Body": MockBody()}
            
            def put_object(self, **kwargs):
                pass
        
        monkeypatch.setattr("services.extractor.worker.s3_client", MockS3())
        
        job_data = {
            "email_id": "test_email_123",
            "s3_raw": "s3://bucket/inbox/raw/test_email_123.json",
            "attachments": [],
            "received_at": "2025-01-01T00:00:00"
        }
        
        process_extraction_job(job_data, db_session)
        
        invoice = db_session.query(Invoice).filter(
            Invoice.source_email_id == "test_email_123"
        ).first()
        
        assert invoice is not None
        assert invoice.extracted is not None
        assert invoice.reconciliation_status == 'needs_review'
    
    def test_reconciliation_updates_invoice(self, db_session, sample_vendor):
        """Test that reconciliation updates invoice normalized fields."""
        extracted = {
            "vendor_name": {
                "value": "ACME Supplies Pvt Ltd",
                "confidence": 0.94
            },
            "total_amount": {
                "value": 1000.0,
                "confidence": 0.9
            }
        }
        
        invoice = Invoice(
            source_email_id="test_123",
            extracted=extracted,
            normalized={},
            reconciliation_status="needs_review"
        )
        db_session.add(invoice)
        db_session.commit()
        
        reconciler = Reconciler(db_session)
        reconciler.reconcile_invoice(invoice)
        db_session.commit()
        
        assert invoice.normalized.get('vendor_id') == sample_vendor.vendor_id
        assert invoice.reconciliation_status == 'auto_matched'

