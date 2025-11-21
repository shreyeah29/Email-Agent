"""Pytest configuration and fixtures."""
import pytest
import os
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from shared.config import Base, settings
from shared.models import Invoice, Vendor, Project

# Use test database
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql://invoice_user:invoice_pass@localhost:5432/invoices_test")


@pytest.fixture(scope="session")
def db_engine():
    """Create test database engine."""
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Create test database session."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def sample_vendor(db_session):
    """Create a sample vendor."""
    vendor = Vendor(
        canonical_name="ACME Supplies Pvt Ltd",
        aliases=["ACME", "Acme Supplies"],
        meta={"category": "supplies"}
    )
    db_session.add(vendor)
    db_session.commit()
    return vendor


@pytest.fixture
def sample_project(db_session):
    """Create a sample project."""
    project = Project(
        name="Project Alpha",
        codes=["ALPHA", "PROJ-ALPHA"],
        meta={"client": "Client A"}
    )
    db_session.add(project)
    db_session.commit()
    return project


@pytest.fixture
def sample_invoice(db_session):
    """Create a sample invoice."""
    extracted = {
        "vendor_name": {
            "value": "ACME Supplies Pvt Ltd",
            "confidence": 0.94,
            "provenance": {"attachment": "invoice.pdf", "page": 1}
        },
        "invoice_number": {
            "value": "INV-2025-123",
            "confidence": 0.98
        },
        "date": {
            "value": "2025-10-21",
            "confidence": 0.95
        },
        "total_amount": {
            "value": 11210.00,
            "confidence": 0.9,
            "currency": "INR"
        }
    }
    
    invoice = Invoice(
        source_email_id="test_email_123",
        raw_email_s3="s3://bucket/inbox/raw/test_email_123.json",
        attachments=[],
        raw_text="Sample invoice text",
        extracted=extracted,
        normalized={},
        extractor_version="v1.0.0",
        reconciliation_status="needs_review"
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice

