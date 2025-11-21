"""SQLAlchemy models for the invoice system."""
from sqlalchemy import Column, Integer, String, Text, TIMESTAMP, ARRAY, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from shared.config import Base
import uuid


class Vendor(Base):
    __tablename__ = "vendors"
    
    vendor_id = Column(Integer, primary_key=True)
    canonical_name = Column(Text, nullable=False)
    aliases = Column(ARRAY(Text))
    meta = Column(JSONB)
    created_at = Column(TIMESTAMP, server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    
    project_id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    codes = Column(ARRAY(Text))
    meta = Column(JSONB)
    created_at = Column(TIMESTAMP, server_default=func.now())


class Invoice(Base):
    __tablename__ = "invoices"
    
    invoice_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_email_id = Column(Text)
    created_at = Column(TIMESTAMP, server_default=func.now())
    raw_email_s3 = Column(Text)
    attachments = Column(JSONB)
    raw_text = Column(Text)
    extracted = Column(JSONB)
    normalized = Column(JSONB)
    tags = Column(ARRAY(Text))
    extractor_version = Column(Text)
    reconciliation_status = Column(Text)
    extra = Column(JSONB)


class InvoiceAudit(Base):
    __tablename__ = "invoice_audit"
    
    audit_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.invoice_id", ondelete="CASCADE"))
    field_name = Column(Text)
    old_value = Column(Text)
    new_value = Column(Text)
    user_name = Column(Text)
    changed_at = Column(TIMESTAMP, server_default=func.now())
    meta = Column(JSONB)


class TrainingExample(Base):
    __tablename__ = "training_examples"
    
    example_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.invoice_id", ondelete="SET NULL"))
    original_extracted = Column(JSONB)
    corrected_extracted = Column(JSONB)
    corrected_normalized = Column(JSONB)
    created_at = Column(TIMESTAMP, server_default=func.now())
    user_name = Column(Text)

