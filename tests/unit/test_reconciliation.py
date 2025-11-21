"""Unit tests for reconciliation logic."""
import pytest
from services.reconciler.worker import Reconciler
from shared.models import Vendor, Project
from sqlalchemy.orm import Session


class TestReconciler:
    """Test vendor/project reconciliation."""
    
    def test_match_vendor_exact(self, db_session, sample_vendor):
        """Test exact vendor name matching."""
        reconciler = Reconciler(db_session)
        vendor_id, score, suggestions = reconciler.match_vendor("ACME Supplies Pvt Ltd")
        
        assert vendor_id == sample_vendor.vendor_id
        assert score >= 90
    
    def test_match_vendor_fuzzy(self, db_session, sample_vendor):
        """Test fuzzy vendor name matching."""
        reconciler = Reconciler(db_session)
        vendor_id, score, suggestions = reconciler.match_vendor("ACME Supplies")
        
        assert vendor_id == sample_vendor.vendor_id
        assert score >= 60
    
    def test_match_vendor_no_match(self, db_session):
        """Test vendor matching with no match."""
        reconciler = Reconciler(db_session)
        vendor_id, score, suggestions = reconciler.match_vendor("Unknown Company XYZ")
        
        assert vendor_id is None or score < 60
    
    def test_match_project(self, db_session, sample_project):
        """Test project matching."""
        reconciler = Reconciler(db_session)
        project_id, score, suggestions = reconciler.match_project("Project Alpha")
        
        assert project_id == sample_project.project_id
        assert score >= 90
    
    def test_reconcile_invoice(self, db_session, sample_vendor, sample_invoice):
        """Test full invoice reconciliation."""
        reconciler = Reconciler(db_session)
        updated = reconciler.reconcile_invoice(sample_invoice)
        
        assert updated
        assert sample_invoice.normalized.get('vendor_id') == sample_vendor.vendor_id
        assert sample_invoice.reconciliation_status == 'auto_matched'

