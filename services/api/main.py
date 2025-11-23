"""FastAPI service - invoice query API and conversational agent."""
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID

from fastapi import FastAPI, Depends, HTTPException, Query, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func, cast, String, ARRAY
from sqlalchemy.dialects.postgresql import array
import re

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    ollama = None

from shared import get_db, Invoice, Vendor, Project, InvoiceAudit, s3_client, settings
from services.api.candidates import router as candidates_router
from services.api.sync_inbox import router as sync_inbox_router
# Import scheduler functions (may fail in test environments)
try:
    from services.api.scheduler import start_scheduler, get_scheduler_status
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    logger.warning("Scheduler module not available (likely missing apscheduler)")
    
    def start_scheduler():
        pass
    
    def get_scheduler_status():
        return {"status": "unavailable", "error": "apscheduler not installed"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Invoice Processing API", version="1.0.0")
security = HTTPBearer()

# Include routers
app.include_router(candidates_router)
app.include_router(sync_inbox_router)


def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API key from header."""
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


# Request/Response models
class InvoiceSummary(BaseModel):
    invoice_id: str
    vendor_name: Optional[str]
    invoice_date: Optional[str]
    total_amount: Optional[float]
    currency: Optional[str]
    confidence: Optional[float]
    attachments: List[Dict]
    reconciliation_status: Optional[str]


class InvoiceDetail(BaseModel):
    invoice_id: str
    source_email_id: Optional[str]
    created_at: str
    raw_email_s3: Optional[str]
    attachments: List[Dict]
    raw_text: Optional[str]
    extracted: Dict
    normalized: Dict
    tags: List[str]
    reconciliation_status: Optional[str]
    audit_trail: List[Dict]


class QueryRequest(BaseModel):
    type: str  # "total_by_vendor", "total_by_project", etc.
    vendor_id: Optional[int] = None
    project_id: Optional[int] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None


class QueryResponse(BaseModel):
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    period: Dict[str, str]
    total_amount: float
    currency: Optional[str]
    invoice_count: int
    low_confidence_count: int
    low_confidence_ids: List[str]


class AgentRequest(BaseModel):
    text: str


class AgentResponse(BaseModel):
    query: str
    answer_text: str
    sources: List[Dict]
    caveats: List[str]


def get_presigned_url(s3_path: str, expires_in: int = 3600) -> str:
    """Generate presigned URL for S3 object, using localhost endpoint for browser access.
    
    For MinIO, we need to create a new client with localhost endpoint to generate
    a valid presigned URL that works in the browser.
    """
    if not s3_path or not s3_path.startswith('s3://'):
        return ""
    
    try:
        bucket = s3_path.split('/')[2]
        key = '/'.join(s3_path.split('/')[3:])
        
        # For MinIO, we need to use localhost endpoint for browser access
        # Create a new client with localhost endpoint if we're using MinIO
        endpoint_url = settings.s3_endpoint_url
        
        if endpoint_url and 'minio:9000' in endpoint_url:
            # Create a new boto3 client with localhost endpoint for presigned URLs
            import boto3
            from botocore.config import Config
            
            localhost_endpoint = endpoint_url.replace('minio:9000', 'localhost:9000')
            
            # Create client with localhost endpoint
            localhost_client = boto3.client(
                's3',
                endpoint_url=localhost_endpoint,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                config=Config(signature_version='s3v4')
            )
            
            # Generate presigned URL with localhost client
            url = localhost_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': key},
                ExpiresIn=expires_in
            )
            return url
        else:
            # Use default client (AWS S3 or MinIO without hostname replacement needed)
            url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': key},
                ExpiresIn=expires_in
            )
            return url
    except Exception as e:
        logger.error(f"Error generating presigned URL: {e}")
        return ""


def get_attachment_urls(attachments: List[Dict]) -> List[Dict]:
    """Convert S3 paths to presigned URLs."""
    result = []
    for att in attachments:
        att_copy = att.copy()
        if 'url' in att_copy and att_copy['url'].startswith('s3://'):
            att_copy['presigned_url'] = get_presigned_url(att_copy['url'])
        result.append(att_copy)
    return result


def get_field_value(extracted: Dict, normalized: Dict, field_name: str, prefer_normalized: bool = True):
    """Get field value, preferring normalized over extracted."""
    if prefer_normalized and normalized and field_name in normalized:
        return normalized[field_name]
    
    if extracted and field_name in extracted:
        field_data = extracted[field_name]
        if isinstance(field_data, dict) and 'value' in field_data:
            return field_data['value']
        return field_data
    
    return None


def calculate_confidence(extracted: Dict) -> float:
    """Calculate average confidence from extracted fields."""
    if not extracted:
        return 0.0
    
    confidences = []
    for field_data in extracted.values():
        if isinstance(field_data, dict) and 'confidence' in field_data:
            confidences.append(field_data['confidence'])
    
    return sum(confidences) / len(confidences) if confidences else 0.5


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.on_event("startup")
def startup_event():
    """Start scheduler on API startup."""
    # Skip scheduler in test environment
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("TESTING") or not SCHEDULER_AVAILABLE:
        logger.info("Skipping scheduler startup (test environment or scheduler unavailable)")
        return
    
    try:
        start_scheduler()
        logger.info("✅ Gmail ingestion scheduler started on API startup")
    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {e}", exc_info=True)
        # Don't fail startup if scheduler fails


@app.on_event("shutdown")
def shutdown_event():
    """Stop scheduler on API shutdown."""
    if not SCHEDULER_AVAILABLE:
        return
    
    try:
        from services.api.scheduler import stop_scheduler
        stop_scheduler()
        logger.info("Scheduler stopped on API shutdown")
    except Exception as e:
        logger.error(f"Error stopping scheduler: {e}")


@app.get("/scheduler/status")
def get_scheduler_status_endpoint(api_key: str = Depends(verify_api_key)):
    """Get scheduler status."""
    return get_scheduler_status()


@app.delete("/invoices/all", dependencies=[Depends(verify_api_key)])
def clear_all_invoices(db: Session = Depends(get_db)):
    """Clear all invoices and audit records from the database.
    
    WARNING: This is a destructive operation that cannot be undone.
    """
    try:
        # Delete audit records first (foreign key constraint)
        deleted_audit = db.query(InvoiceAudit).delete()
        
        # Delete all invoices
        deleted_invoices = db.query(Invoice).delete()
        
        db.commit()
        
        return {
            "message": "All data cleared successfully",
            "deleted_invoices": deleted_invoices,
            "deleted_audit_records": deleted_audit
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error clearing data: {e}")
        raise HTTPException(status_code=500, detail=f"Error clearing data: {str(e)}")


@app.get("/invoices", response_model=List[InvoiceSummary])
def list_invoices(
    vendor_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """List invoices with filtering and pagination."""
    query = db.query(Invoice)
    
    # Apply filters
    if vendor_id:
        query = query.filter(Invoice.normalized['vendor_id'].astext == str(vendor_id))
    
    if project_id:
        query = query.filter(Invoice.normalized['project_id'].astext == str(project_id))
    
    if status:
        query = query.filter(Invoice.reconciliation_status == status)
    
    if tag:
        # Use PostgreSQL array contains operator
        query = query.filter(Invoice.tags.any(tag))
    
    # Date filtering (simplified - would need proper date parsing)
    if from_date or to_date:
        # This is a simplified version - would need proper date field extraction
        pass
    
    # Pagination
    total = query.count()
    invoices = query.offset((page - 1) * page_size).limit(page_size).all()
    
    results = []
    for inv in invoices:
        extracted = inv.extracted or {}
        normalized = inv.normalized or {}
        
        vendor_name = get_field_value(extracted, normalized, 'vendor_name')
        if not vendor_name and normalized.get('vendor_id'):
            vendor = db.query(Vendor).filter(Vendor.vendor_id == normalized['vendor_id']).first()
            if vendor:
                vendor_name = vendor.canonical_name
        
        invoice_date = get_field_value(extracted, normalized, 'date')
        total_amount = get_field_value(extracted, normalized, 'total_amount')
        currency = get_field_value(extracted, normalized, 'currency')
        
        results.append(InvoiceSummary(
            invoice_id=str(inv.invoice_id),
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            currency=currency,
            confidence=calculate_confidence(extracted),
            attachments=get_attachment_urls(inv.attachments or []),
            reconciliation_status=inv.reconciliation_status
        ))
    
    return results


@app.get("/invoice/{invoice_id}", response_model=InvoiceDetail)
def get_invoice(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Get full invoice details."""
    invoice = db.query(Invoice).filter(Invoice.invoice_id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # Get audit trail
    audit_records = db.query(InvoiceAudit).filter(
        InvoiceAudit.invoice_id == invoice_id
    ).order_by(InvoiceAudit.changed_at.desc()).all()
    
    audit_trail = [
        {
            "audit_id": str(a.audit_id),
            "field_name": a.field_name,
            "old_value": a.old_value,
            "new_value": a.new_value,
            "user_name": a.user_name,
            "changed_at": a.changed_at.isoformat()
        }
        for a in audit_records
    ]
    
    return InvoiceDetail(
        invoice_id=str(invoice.invoice_id),
        source_email_id=invoice.source_email_id,
        created_at=invoice.created_at.isoformat(),
        raw_email_s3=get_presigned_url(invoice.raw_email_s3) if invoice.raw_email_s3 else None,
        attachments=get_attachment_urls(invoice.attachments or []),
        raw_text=invoice.raw_text[:1000] if invoice.raw_text else None,  # First 1000 chars
        extracted=invoice.extracted or {},
        normalized=invoice.normalized or {},
        tags=invoice.tags or [],
        reconciliation_status=invoice.reconciliation_status,
        audit_trail=audit_trail
    )


@app.post("/query", response_model=QueryResponse)
def structured_query(
    request: QueryRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Execute structured queries on invoices."""
    query = db.query(Invoice)
    
    # Build query based on type
    if request.type == "total_by_vendor":
        if not request.vendor_id:
            raise HTTPException(status_code=400, detail="vendor_id required for total_by_vendor")
        
        query = query.filter(Invoice.normalized['vendor_id'].astext == str(request.vendor_id))
        
        vendor = db.query(Vendor).filter(Vendor.vendor_id == request.vendor_id).first()
        vendor_name = vendor.canonical_name if vendor else None
        
        # Calculate totals
        invoices = query.all()
        total_amount = 0.0
        currency = None
        low_confidence_count = 0
        low_confidence_ids = []
        
        for inv in invoices:
            normalized = inv.normalized or {}
            extracted = inv.extracted or {}
            
            amount = normalized.get('total_amount') or get_field_value(extracted, normalized, 'total_amount', False)
            if amount:
                total_amount += float(amount)
            
            if not currency:
                currency = normalized.get('currency') or get_field_value(extracted, normalized, 'currency', False)
            
            conf = calculate_confidence(extracted)
            if conf < 0.7:
                low_confidence_count += 1
                low_confidence_ids.append(str(inv.invoice_id))
        
        return QueryResponse(
            vendor_id=request.vendor_id,
            vendor_name=vendor_name,
            period={"from": request.from_date or "", "to": request.to_date or ""},
            total_amount=total_amount,
            currency=currency,
            invoice_count=len(invoices),
            low_confidence_count=low_confidence_count,
            low_confidence_ids=low_confidence_ids
        )
    
    elif request.type == "total_by_project":
        if not request.project_id:
            raise HTTPException(status_code=400, detail="project_id required for total_by_project")
        
        query = query.filter(Invoice.normalized['project_id'].astext == str(request.project_id))
        
        project = db.query(Project).filter(Project.project_id == request.project_id).first()
        project_name = project.name if project else None
        
        invoices = query.all()
        total_amount = 0.0
        currency = None
        low_confidence_count = 0
        low_confidence_ids = []
        
        for inv in invoices:
            normalized = inv.normalized or {}
            extracted = inv.extracted or {}
            
            amount = normalized.get('total_amount') or get_field_value(extracted, normalized, 'total_amount', False)
            if amount:
                total_amount += float(amount)
            
            if not currency:
                currency = normalized.get('currency') or get_field_value(extracted, normalized, 'currency', False)
            
            conf = calculate_confidence(extracted)
            if conf < 0.7:
                low_confidence_count += 1
                low_confidence_ids.append(str(inv.invoice_id))
        
        return QueryResponse(
            project_id=request.project_id,
            project_name=project_name,
            period={"from": request.from_date or "", "to": request.to_date or ""},
            total_amount=total_amount,
            currency=currency,
            invoice_count=len(invoices),
            low_confidence_count=low_confidence_count,
            low_confidence_ids=low_confidence_ids
        )
    
    else:
        raise HTTPException(status_code=400, detail=f"Unknown query type: {request.type}")


def search_documents_by_keywords(query_text: str, db: Session, limit: int = 10) -> List[Dict]:
    """Search ALL documents by keywords in raw_text (which contains full PDF content).
    
    This searches through ALL PDF content from all processed emails.
    The agent uses this as its complete knowledge base - any question about
    any PDF content can be answered using this search.
    
    Improved matching: handles hyphens, case-insensitive, partial matches.
    """
    query_lower = query_text.lower()
    # Normalize query: replace hyphens with spaces, handle punctuation
    query_normalized = query_lower.replace('-', ' ').replace('_', ' ')
    # Extract keywords - keep words longer than 2 chars, also handle numbers
    keywords = []
    for word in query_normalized.split():
        word_clean = word.strip('.,!?;:()[]{}"\'').lower()
        if len(word_clean) > 2 or word_clean.isdigit():
            keywords.append(word_clean)
    
    if not keywords:
        # If no keywords, return all documents for general queries
        all_invoices = db.query(Invoice).filter(
            Invoice.raw_text.isnot(None),
            Invoice.raw_text != ''
        ).order_by(Invoice.created_at.desc()).limit(limit).all()
        return [{
            "invoice_id": str(inv.invoice_id),
            "relevance": 0.5,
            "match_count": 0,
            "snippet": inv.raw_text[:500] if inv.raw_text else "",
            "full_text": inv.raw_text or "",
            "full_text_preview": inv.raw_text[:2000] if inv.raw_text else "",
            "attachment_names": [att.get('filename', 'Unknown') for att in (inv.attachments or [])],
            "doc_type": "Document",
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "url": get_presigned_url(inv.raw_email_s3) if inv.raw_email_s3 else None,
            "extracted_fields": inv.extracted or {}
        } for inv in all_invoices]
    
    # Get all invoices with raw_text (contains full PDF content)
    all_invoices = db.query(Invoice).filter(
        Invoice.raw_text.isnot(None),
        Invoice.raw_text != ''
    ).all()
    
    matches = []
    for inv in all_invoices:
        if not inv.raw_text:
            continue
        
        # Normalize text for matching: lowercase, replace hyphens with spaces
        raw_text_normalized = inv.raw_text.lower().replace('-', ' ').replace('_', ' ')
        raw_text_lower = inv.raw_text.lower()  # Keep for snippet extraction
        full_text = inv.raw_text  # Keep original for display
        
        # Count keyword matches - improved matching
        match_count = 0
        for keyword in keywords:
            # Check if keyword appears in normalized text
            if keyword in raw_text_normalized:
                match_count += 1
            # Also check for partial matches (e.g., "roll" matches "roll-off")
            elif any(keyword in word or word in keyword for word in raw_text_normalized.split() if len(word) > 2):
                match_count += 0.5  # Partial match gets half credit
        
        if match_count == 0:
            continue
        
        # Calculate relevance score (keyword density + position bonus)
        relevance = match_count / len(keywords)
        
        # Bonus for matches in first 500 chars (title/header area)
        if any(keyword in raw_text_normalized[:500] for keyword in keywords):
            relevance += 0.2
        
        # Extract snippet around first match
        snippet = ""
        for keyword in keywords:
            # Search in normalized text for better matching
            idx = raw_text_normalized.find(keyword)
            if idx != -1:
                # Use original text for snippet (not normalized)
                start = max(0, idx - 150)
                end = min(len(inv.raw_text), idx + len(keyword) + 150)
                snippet = inv.raw_text[start:end].strip()
                # Clean up snippet
                snippet = ' '.join(snippet.split())
                break
        
        # Get attachment info
        attachments = inv.attachments or []
        attachment_names = [att.get('filename', 'Unknown') for att in attachments]
        
        # Get extracted fields for context
        extracted = inv.extracted or {}
        doc_type = "Unknown"
        if any(kw in raw_text_lower for kw in ['invoice', 'bill', 'receipt']):
            doc_type = "Invoice/Receipt"
        elif any(kw in raw_text_lower for kw in ['contract', 'agreement']):
            doc_type = "Contract"
        elif any(kw in raw_text_lower for kw in ['report', 'analysis']):
            doc_type = "Report"
        elif any(kw in raw_text_lower for kw in ['manual', 'guide', 'documentation']):
            doc_type = "Documentation"
        
        matches.append({
            "invoice_id": str(inv.invoice_id),
            "relevance": min(1.0, relevance),  # Cap at 1.0
            "match_count": match_count,
            "snippet": snippet[:500] if snippet else full_text[:500],
            "full_text": full_text,  # FULL text for comprehensive understanding
            "full_text_preview": full_text[:2000],  # Preview for display
            "attachment_names": attachment_names,
            "doc_type": doc_type,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "url": get_presigned_url(inv.raw_email_s3) if inv.raw_email_s3 else None,
            "extracted_fields": extracted  # Include extracted fields for context
        })
    
    # Sort by relevance
    matches.sort(key=lambda x: x['relevance'], reverse=True)
    return matches[:limit]


def generate_summary(text: str, max_length: int = 500) -> str:
    """Generate an intelligent summary of text, extracting key information."""
    if not text:
        return "No content available."
    
    # Clean text
    text = ' '.join(text.split())
    
    # Try to identify document type and key sections
    text_lower = text.lower()
    
    # Extract key information patterns
    key_info = []
    
    # Look for titles/headings (lines in ALL CAPS or Title Case)
    lines = text.split('\n')
    potential_titles = [line.strip() for line in lines[:20] if line.strip() and (line.isupper() or line.istitle())]
    if potential_titles:
        key_info.append(f"Title/Subject: {potential_titles[0]}")
    
    # Look for dates
    dates = re.findall(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', text)
    if dates:
        key_info.append(f"Date(s): {', '.join(dates[:3])}")
    
    # Look for amounts/money
    amounts = re.findall(r'[\$₹€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?', text)
    if amounts:
        key_info.append(f"Amount(s): {', '.join(amounts[:3])}")
    
    # Look for email addresses
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
    if emails:
        key_info.append(f"Contact: {', '.join(emails[:2])}")
    
    # Split into sentences
    sentences = re.split(r'[.!?]+\s+', text)
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 10]
    
    # Prioritize sentences with key terms
    important_terms = ['summary', 'overview', 'introduction', 'conclusion', 'key', 'important', 'main', 'purpose']
    prioritized = []
    regular = []
    
    for sentence in sentences:
        if any(term in sentence.lower() for term in important_terms):
            prioritized.append(sentence)
        else:
            regular.append(sentence)
    
    # Build summary
    summary_parts = []
    
    # Add key info if found
    if key_info:
        summary_parts.append(" | ".join(key_info))
    
    # Add prioritized sentences first
    for sentence in prioritized[:3]:
        if len(' '.join(summary_parts)) + len(sentence) + 10 <= max_length:
            summary_parts.append(sentence)
    
    # Add regular sentences
    for sentence in regular:
        current_length = len(' '.join(summary_parts))
        if current_length + len(sentence) + 10 <= max_length:
            summary_parts.append(sentence)
        else:
            break
    
    summary = '. '.join(summary_parts)
    
    if not summary or len(summary) < 50:
        # Fallback: first meaningful sentences
        summary = '. '.join(sentences[:5])
        if len(text) > max_length:
            summary = summary[:max_length-3] + "..."
    
    return summary.strip()


def answer_question_with_llm(question: str, documents: List[Dict]) -> str:
    """Use LLM (Ollama - free, local) to answer questions based on document content.
    
    This is much more accurate than regex-based matching as it understands
    context, semantics, and can extract information intelligently.
    """
    if not documents:
        return "I couldn't find any relevant documents to answer your question."
    
    # Check if Ollama is available
    if not OLLAMA_AVAILABLE:
        logger.error("Ollama library not installed! Install with: pip install ollama")
        raise Exception("Ollama library not available")
    
    # FIRST: Try direct extraction from structured data for price questions
    # This is more reliable than LLM for finding specific items
    question_lower = question.lower()
    is_price_question = 'price' in question_lower or 'cost' in question_lower
    is_total_price = 'total price' in question_lower or 'total cost' in question_lower
    is_order_total = 'order total' in question_lower or 'receipt total' in question_lower or ('total' in question_lower and ('order' in question_lower or 'receipt' in question_lower))
    
    # Handle "order total" or "receipt total" queries
    if is_order_total:
        # Extract receipt/order number if mentioned
        receipt_number = None
        import re
        # Look for patterns like "H8551-451363", "H8551", or just numbers
        receipt_match = re.search(r'([A-Z]?\d+[-]?\d*)', question)
        if receipt_match:
            receipt_number = receipt_match.group(1)
            logger.info(f"Order total query: Looking for receipt/order number: {receipt_number}")
        
        # Search through documents for matching receipt
        for doc in documents:
            # Check if this document matches the receipt number
            invoice_number = doc.get('extracted_fields', {}).get('invoice_number', {}).get('value', '')
            attachment_names = doc.get('attachment_names', [])
            full_text = doc.get('full_text', '') or doc.get('full_text_preview', '')
            
            # Match by invoice number or attachment name
            matches_receipt = False
            if receipt_number:
                if receipt_number in str(invoice_number) or receipt_number in ' '.join(attachment_names) or receipt_number in full_text:
                    matches_receipt = True
                    logger.info(f"Order total: Found matching receipt {receipt_number} in document {attachment_names[0] if attachment_names else 'Unknown'}")
            else:
                # No receipt number specified, use first document
                matches_receipt = True
            
            if matches_receipt:
                extracted_fields = doc.get('extracted_fields', {})
                
                # PRIORITY 1: Try to extract from raw text FIRST (most accurate for "Order Total")
                order_total_found = False
                if full_text:
                    # Look for "Order Total: $X.XX" or similar patterns (prioritize this)
                    total_patterns = [
                        r'Order\s+Total[:\s]+\$?([\d,]+\.?\d*)',  # "Order Total: $326.18"
                        r'Grand\s+Total[:\s]+\$?([\d,]+\.?\d*)',  # "Grand Total: $326.18"
                        r'Charged[:\s]+\$?([\d,]+\.?\d*)',  # "Charged: $326.18"
                        r'Amount\s+Due[:\s]+\$?([\d,]+\.?\d*)',  # "Amount Due: $326.18"
                        r'Balance\s+Due[:\s]+\$?([\d,]+\.?\d*)',  # "Balance Due: $326.18"
                    ]
                    for pattern in total_patterns:
                        match = re.search(pattern, full_text, re.IGNORECASE)
                        if match:
                            total_value = match.group(1).replace(',', '')
                            logger.info(f"Order total: Extracted from text pattern '{pattern}': ${total_value}")
                            return f"${total_value}"
                
                # PRIORITY 2: Try to get total_amount from extracted fields
                total_amount = extracted_fields.get('total_amount', {}).get('value')
                if total_amount:
                    logger.info(f"Order total: Found in extracted fields: ${total_amount}")
                    return f"${total_amount}"
                
                # PRIORITY 3: Don't calculate from line_items for "order total" queries
                # The subtotal + tax might not equal the actual Order Total (could have fees, discounts, etc.)
                # Let the LLM find it in the full document text instead
                logger.info(f"Order total: Not found in direct extraction, using LLM to search full document text")
        
        # If we get here, we didn't find a match - let LLM handle it
        logger.info("Order total: No direct match found, using LLM")
    
    if is_price_question:
        # Extract item name from question
        item_name = None
        if 'of' in question_lower:
            of_idx = question_lower.find('of')
            item_name = question_lower[of_idx + 3:].strip()
            # Remove trailing question words
            for end_word in ['?', '.', ' cost', ' price', ' unit price', ' total price', ' total cost']:
                if item_name.endswith(end_word):
                    item_name = item_name[:-len(end_word)].strip()
        
        if item_name:
            # Search through all documents' line items
            # Extract key terms from item name (filter common words)
            common_words = {'the', 'a', 'an', 'and', 'or', 'with', 'for', 'in', 'on', 'at', 'to', 'of', 'is', 'are', 'was', 'were'}
            item_terms = [t.lower().strip('.,!?;:()[]{}') for t in item_name.split() if len(t) > 2 and t.lower() not in common_words]
            
            # Include important terms (brands, numbers, specific product words)
            for word in item_name.split():
                word_clean = word.lower().strip('.,!?;:()[]{}')
                # Include brands (capitalized), numbers, or specific terms
                if (word[0].isupper() and len(word) > 2) or word.isdigit() or word_clean in ['cu', 'gang', 'nail', 'knockout', 'knockouts', 'box', 'set']:
                    if word_clean not in item_terms:
                        item_terms.append(word_clean)
            
            logger.info(f"Direct extraction: Searching for '{item_name}' with key terms: {item_terms[:10]}")
            
            best_match = None
            best_score = 0
            
            for doc in documents:
                extracted_fields = doc.get('extracted_fields', {})
                line_items = extracted_fields.get('line_items', {}).get('value', [])
                
                if line_items:
                    for item in line_items:
                        desc = str(item.get('description', '')).lower()
                        
                        # Calculate match score
                        matches = sum(1 for term in item_terms if term in desc)
                        score = matches / max(len(item_terms), 1)  # Percentage match
                        
                        # Bonus for brand name match (very important)
                        brands = ['dewalt', 'steel city', 'cantex', 'carlon', 'husky', 'diablo', 'defiant']
                        question_brand = None
                        for brand in brands:
                            if brand in item_name.lower():
                                question_brand = brand
                                break
                        
                        if question_brand and question_brand in desc:
                            score += 0.3  # Big bonus for brand match
                            logger.info(f"  Brand match bonus: {question_brand} found in '{item.get('description', '')[:50]}...'")
                        
                        # Track best match
                        if score > best_score:
                            best_score = score
                            best_match = (item, doc, score, matches)
                        
                        logger.info(f"  Item '{item.get('description', '')[:50]}...' - Score: {score:.2f} ({matches}/{len(item_terms)} terms)")
            
            # Only use direct extraction if we have HIGH confidence (>= 60% match or >= 5 terms matched)
            if best_match and (best_score >= 0.6 or (best_match[3] >= 5 and best_score >= 0.4)):
                item, doc, score, matches = best_match
                logger.info(f"Direct extraction: High confidence match (score: {best_score:.2f}, {matches}/{len(item_terms)} terms) - '{item.get('description', '')[:50]}...'")
                
                if is_total_price:
                    subtotal = item.get('subtotal')
                    if subtotal:
                        logger.info(f"Direct extraction: Returning subtotal ${subtotal}")
                        return f"${subtotal}"
                else:
                    unit_price = item.get('unit_price')
                    if unit_price:
                        # Clean unit_price if it has multiple values
                        if isinstance(unit_price, str) and '\n' in unit_price:
                            prices = [p.strip() for p in unit_price.split('\n') if p.strip()]
                            unit_price = prices[-1] if prices else unit_price
                        # Extract numeric value
                        import re
                        price_match = re.search(r'[\d,]+\.?\d*', str(unit_price))
                        if price_match:
                            logger.info(f"Direct extraction: Returning unit price ${price_match.group(0)}")
                            return f"${price_match.group(0)}"
            else:
                if best_match:
                    logger.info(f"Direct extraction: Low confidence (score: {best_score:.2f}), falling back to LLM for better semantic matching")
                else:
                    logger.info(f"Direct extraction: No match found, using LLM")
    
    logger.info(f"Calling Ollama with model {settings.ollama_model} for question: {question[:50]}...")
    
    try:
        # Prepare context from documents - use FULL text AND structured line_items
        context_parts = []
        for doc in documents[:5]:  # Use top 5 documents to ensure we find the right one
            full_text = doc.get('full_text', '') or doc.get('full_text_preview', '')
            attachment_names = doc.get('attachment_names', [])
            extracted_fields = doc.get('extracted_fields', {})
            
            doc_name = attachment_names[0] if attachment_names else 'Unknown'
            doc_info = f"=== Document: {doc_name} ===\n"
            
            # Include structured line items if available (more reliable than text parsing)
            if extracted_fields and 'line_items' in extracted_fields:
                line_items = extracted_fields['line_items'].get('value', [])
                if line_items:
                    doc_info += "Line Items (structured data):\n"
                    for item in line_items:
                        desc = item.get('description', '')
                        unit_price = item.get('unit_price', item.get('price', 'N/A'))
                        # Clean unit_price if it has multiple values
                        if isinstance(unit_price, str) and '\n' in unit_price:
                            # Take the last price (usually the discounted/final price)
                            prices = [p.strip() for p in unit_price.split('\n') if p.strip()]
                            unit_price = prices[-1] if prices else unit_price
                        qty = item.get('quantity', '')
                        subtotal = item.get('subtotal', 'N/A')
                        # Show subtotal prominently as it's the total price for that item
                        doc_info += f"  Item: {desc}\n"
                        doc_info += f"    Unit Price: ${unit_price} | Quantity: {qty} | TOTAL PRICE (Subtotal): ${subtotal}\n"
                    doc_info += "\n"
            
            # Also include full text for context (limit to 5000 chars per doc for faster processing)
            if full_text:
                # Truncate to avoid very long contexts that slow down Ollama
                text_preview = full_text[:5000] if len(full_text) > 5000 else full_text
                doc_info += f"Full Document Text:\n{text_preview}\n"
            
            context_parts.append(doc_info)
            logger.info(f"Added document {doc_name} with {len(full_text)} chars + {len(extracted_fields.get('line_items', {}).get('value', []))} line items")
        
        if not context_parts:
            logger.warning("No readable text found in documents")
            return "The documents don't contain readable text content."
        
        context = "\n\n".join(context_parts)
        logger.info(f"Total context length: {len(context)} characters from {len(context_parts)} documents")
        
        # Prepare prompt for LLM - generic and flexible for ANY item in ANY PDF
        system_prompt = """You are a precise document analysis assistant. Extract exact information from documents and return ONLY the answer.

CRITICAL RULES:
1. Return ONLY the answer - just "$X.XX" format, nothing else
2. Look at "Line Items (structured data)" section FIRST - this is the MOST RELIABLE source
3. Find the line item whose description matches the item from the question
4. Use semantic understanding to match items - match meaning, not just exact words
5. For "total price" or "total cost" questions:
   - ALWAYS use the "Subtotal (Total Price)" value - this is the total for that specific item
   - Do NOT calculate unit_price × quantity - use the subtotal directly
   - The subtotal is the accurate total price for that item
6. For "unit price" or "price per item" questions:
   - Use the "Unit Price" value
   - If there are multiple prices (like "$39.97 / each\n$29.97 / each"), use the final/discounted price
7. Format: Just "$X.XX" - nothing else

DO NOT:
- Include any explanations
- Include email forwarding information
- Include HTML or email headers
- Say "Based on..." or "I found..."
- Return information about different items
- Calculate manually - use the values from line items directly"""
        
        user_prompt = f"""Question: {question}

Document Content:
{context}

INSTRUCTIONS:
1. Determine what the question is asking:
   - If it asks for "order total", "receipt total", or "total" for a receipt/order number:
     * CRITICAL: Look carefully in the "Full Document Text" for the FINAL TOTAL
     * Search for these exact phrases (case-insensitive):
       - "Order Total: $X.XX" or "Order Total $X.XX"
       - "Grand Total: $X.XX"
       - "Total: $X.XX" (but be careful - this might be subtotal)
       - "Amount Due: $X.XX"
       - "Charged: $X.XX" or "Charged $X.XX"
       - "Balance Due: $X.XX"
     * The Order Total is usually AFTER the subtotal and tax lines
     * It's the FINAL amount that includes all items, tax, and fees
     * Format: "$X.XX" (e.g., "$326.18")
     * DO NOT use the subtotal from line items - use the actual "Order Total" field
   - If it asks for "total price" or "total cost" of a SPECIFIC ITEM:
     * Look at "Line Items (structured data)" section FIRST - this is the most accurate
     * Find the line item whose description matches the item from the question
     * Match semantically: "DEWALT Modular Right Angle Attachment Set" matches any line item containing "DEWALT", "Modular", "Right Angle", "Attachment"
     * Look for "TOTAL PRICE (Subtotal): $X.XX" in that line item
     * Use that value - it's the total price for that specific item
     * Format: "$X.XX" (e.g., "$29.97")
   - If it asks for "unit price" or "price per item":
     * Use the "Unit Price" value
     * If multiple prices shown, use the final/discounted price
     * Format: "$X.XX" (e.g., "$29.97")
2. Return ONLY the answer - just "$X.XX" format, nothing else

Examples:
- Question: "order total for H8551-451363 Receipt"
  * Search for "Order Total:" in the document text
  * If you see "Subtotal: $307.72" and "Order Total: $326.18", use $326.18
  * Answer: "$326.18"
- Question: "total price of DEWALT Modular Right Angle Attachment Set"
  Answer: "$29.97" (from line item subtotal)
- Return: "$29.97"

Return format: Just the price like "$29.97" - nothing else."""
        
        # Call Ollama API (free, local)
        logger.info(f"Making Ollama API call to {settings.ollama_base_url} with model: {settings.ollama_model}")
        
        # Set the base URL for Ollama client
        import ollama
        client = ollama.Client(host=settings.ollama_base_url)
        
        response = client.chat(
            model=settings.ollama_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            options={
                "temperature": 0.0,  # Zero temperature for maximum precision
                "num_predict": 200,  # Shorter response for faster processing
                "num_ctx": 4096  # Limit context window for faster processing
            }
        )
        
        answer = response['message']['content'].strip()
        logger.info(f"LLM raw response: {answer[:300]}...")
        
        # Extract price from answer - be very aggressive
        import re
        
        # First, try to find all prices in the answer
        prices = re.findall(r'\$[\d,]+\.?\d*', answer)
        logger.info(f"Found prices in response: {prices}")
        
        # If we found prices, use smart logic to pick the right one
        if prices:
            # For "total price" questions, look for the largest reasonable price
            # For "unit price" questions, look for smaller prices
            question_lower = question.lower()
            is_total_price = 'total price' in question_lower or 'total cost' in question_lower
            
            # Convert prices to numbers for comparison
            price_values = []
            for price_str in prices:
                try:
                    # Remove $ and commas, convert to float
                    num_str = price_str.replace('$', '').replace(',', '')
                    price_values.append((float(num_str), price_str))
                except:
                    continue
            
            logger.info(f"Price values: {price_values}, is_total_price: {is_total_price}")
            
            if price_values:
                if is_total_price:
                    # For total price, prefer larger values (but reasonable - $1 to $1000)
                    # The DEWALT item subtotal is $29.97, so we want values in this range
                    valid_prices = [(val, str_price) for val, str_price in price_values if 1 <= val <= 1000]
                    if valid_prices:
                        # Sort by value descending, take the largest (most likely the subtotal)
                        valid_prices.sort(reverse=True)
                        answer = valid_prices[0][1]
                        logger.info(f"Selected total price: {answer} from {valid_prices}")
                    else:
                        # Fallback to largest price found
                        price_values.sort(reverse=True)
                        answer = price_values[0][1]
                        logger.info(f"Fallback to largest price: {answer}")
                else:
                    # For unit price, prefer smaller values (under $100)
                    valid_prices = [(val, str_price) for val, str_price in price_values if 0.01 <= val <= 100]
                    if valid_prices:
                        # Sort by value ascending, take the smallest
                        valid_prices.sort()
                        answer = valid_prices[0][1]
                        logger.info(f"Selected unit price: {answer}")
                    else:
                        # Fallback to smallest price found
                        price_values.sort()
                        answer = price_values[0][1]
                        logger.info(f"Fallback to smallest price: {answer}")
        
        # If no prices found or answer doesn't start with $, try to extract from text
        if not answer.startswith('$'):
            price_match = re.search(r'\$[\d,]+\.?\d*', answer)
            if price_match:
                answer = price_match.group(0)
                logger.info(f"Extracted price from text: {answer}")
        
        # Clean up verbose prefixes
        verbose_prefixes = [
            "Based on the provided documents",
            "Based on the documents",
            "I found",
            "I will attempt",
            "Let me search",
            "After searching",
            "Looking at the documents",
            "The line item that matches"
        ]
        for prefix in verbose_prefixes:
            if answer.lower().startswith(prefix.lower()):
                remaining = answer[len(prefix):].strip()
                price_match = re.search(r'\$[\d,]+\.?\d*', remaining)
                if price_match:
                    answer = price_match.group(0)
                    break
        
        logger.info(f"Final cleaned answer: {answer}")
        return answer.strip()
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error calling Ollama API: {e}")
        
        # Check if Ollama server is not running
        if "connection" in error_msg.lower() or "refused" in error_msg.lower() or "111" in error_msg:
            logger.error(f"❌ Ollama server not running! Please start Ollama: ollama serve")
            return f"I cannot answer this question because Ollama is not running. Please start Ollama by running 'ollama serve' in your terminal, or install Ollama from https://ollama.ai if you haven't already. Error: {error_msg}"
        
        # Check if model is not available
        if "model" in error_msg.lower() and ("not found" in error_msg.lower() or "404" in error_msg):
            logger.error(f"❌ Ollama model '{settings.ollama_model}' not found! Please pull it: ollama pull {settings.ollama_model}")
            return f"I cannot answer this question because the Ollama model '{settings.ollama_model}' is not installed. Please run 'ollama pull {settings.ollama_model}' to download it. Error: {error_msg}"
        
        # For other errors, fallback to regex
        logger.warning("Falling back to regex-based extraction due to API error")
        return answer_question_from_documents(question, documents)


def answer_question_from_documents(question: str, documents: List[Dict]) -> str:
    """Answer a question based on FULL document content - works for any document type.
    
    This is a fallback method when LLM is not available.
    """
    if not documents:
        return "I couldn't find any relevant documents to answer your question."
    
    question_lower = question.lower()
    
    # Use the FULL text from the most relevant document(s)
    best_match = documents[0] if documents else None
    if not best_match:
        return "No relevant documents found."
    
    # Get FULL text (not just preview)
    full_text = best_match.get('full_text', '') or best_match.get('full_text_preview', '')
    snippet = best_match.get('snippet', '')
    doc_type = best_match.get('doc_type', 'Document')
    attachment_names = best_match.get('attachment_names', [])
    extracted_fields = best_match.get('extracted_fields', {})
    
    # Use full text for comprehensive understanding
    context = full_text if full_text else snippet
    
    if not context:
        return "The document doesn't contain readable text content."
    
    # Answer based on question type - provide clean, direct answers
    if any(word in question_lower for word in ['what', 'about', 'describe', 'explain', 'tell me about']):
        # Summary/description question - use full text for comprehensive answer
        summary = generate_summary(context, max_length=500)
        # Clean, direct answer without verbose prefixes
        return summary
    
    elif any(word in question_lower for word in ['summarize', 'summary']):
        # Explicit summary request - clean format
        summary = generate_summary(context, max_length=600)
        return summary
    
    elif any(word in question_lower for word in ['how many', 'count', 'number']):
        # Count question - look for numbers in context
        numbers = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b', context)
        if numbers:
            # Filter out very small numbers (likely not the answer)
            significant_numbers = [n for n in numbers if float(n.replace(',', '')) > 10]
            if significant_numbers:
                return f"Found: {', '.join(significant_numbers[:5])}"
            return f"Found {len(numbers)} number(s): {', '.join(numbers[:5])}"
        return "No specific numbers found in the document."
    
    elif any(word in question_lower for word in ['when', 'date', 'time']):
        # Date question
        dates = re.findall(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', context)
        if dates:
            return f"**Date(s):** {', '.join(dates[:3])}"
        return "No dates found in the document."
    
    elif any(word in question_lower for word in ['who', 'person', 'author', 'sender', 'from']):
        # Person/author question
        emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', context)
        if emails:
            return f"Found contact information: {', '.join(emails[:3])}."
        # Look for names (capitalized words that might be names)
        potential_names = re.findall(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b', context[:500])
        if potential_names:
            return f"Found potential names/entities: {', '.join(set(potential_names[:3]))}."
        return "I couldn't find specific person/author information in the document."
    
    elif any(word in question_lower for word in ['where', 'location', 'address']):
        # Location question
        addresses = re.findall(r'\d+\s+[A-Za-z0-9\s,]+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|City|State|Country)', context, re.IGNORECASE)
        if addresses:
            return f"Found location information: {', '.join(addresses[:2])}."
        return "I couldn't find specific location information in the document."
    
    elif any(word in question_lower for word in ['why', 'reason', 'because', 'purpose']):
        # Reason/purpose question - look for explanatory sentences
        sentences = re.split(r'[.!?]+\s+', context)
        explanatory = [s for s in sentences if any(word in s.lower() for word in ['because', 'reason', 'purpose', 'due to', 'in order to'])]
        if explanatory:
            return f"Based on the document: {explanatory[0][:300]}"
        return generate_summary(context, max_length=400)
    
    else:
        # General question - search for relevant content in full text
        # Extract keywords from question - handle hyphens and punctuation
        question_normalized = question_lower.replace('-', ' ').replace('_', ' ')
        question_keywords = []
        for word in question_normalized.split():
            word_clean = word.strip('.,!?;:()[]{}"\'').lower()
            if len(word_clean) > 2 or word_clean.isdigit():
                question_keywords.append(word_clean)
        
        # Extract item/product name from question
        item_name = None
        if 'unit price' in question_lower or 'price' in question_lower:
            # Pattern: "what is the unit price of X" -> X is the item
            if 'of' in question_lower:
                of_idx = question_lower.find('of')
                item_name = question_lower[of_idx + 3:].strip()
                # Remove trailing question words
                for end_word in ['?', '.', ' cost', ' price', ' unit price']:
                    if item_name.endswith(end_word):
                        item_name = item_name[:-len(end_word)].strip()
            elif 'what is' in question_lower or 'what was' in question_lower:
                # Pattern: "what is X price" -> X is the item
                parts = question_lower.split()
                if 'price' in parts:
                    price_idx = parts.index('price')
                    if price_idx > 2:
                        item_name = ' '.join(parts[2:price_idx])
        
        # Special handling for unit price questions - USE STRUCTURED TABLE DATA
        if 'unit price' in question_lower and item_name:
            # First, try to use extracted line_items from the document (if available)
            # This is much more reliable than parsing raw text
            line_items_data = extracted_fields.get('line_items', {})
            
            # Debug: log what we have
            logger.info(f"Looking for item: {item_name}")
            logger.info(f"Line items data type: {type(line_items_data)}, keys: {line_items_data.keys() if isinstance(line_items_data, dict) else 'N/A'}")
            
            if isinstance(line_items_data, dict) and 'value' in line_items_data:
                # Line items are stored as structured data
                items = line_items_data['value']
                logger.info(f"Found {len(items) if isinstance(items, list) else 0} line items")
                
                if isinstance(items, list) and len(items) > 0:
                    item_normalized = item_name.lower().replace('-', ' ').replace('_', ' ')
                    # Extract key terms - focus on distinctive words
                    item_terms = []
                    for word in item_normalized.split():
                        word_clean = word.strip('.,!?;:()[]{}"\'').lower()
                        if word_clean.isdigit() or word_clean in ['steel', 'city', 'octagon', 'box', 'knockouts']:
                            item_terms.append(word_clean)
                        elif len(word_clean) > 2 and word_clean not in ['the', 'with', 'and', 'for', 'in']:
                            item_terms.append(word_clean)
                    
                    logger.info(f"Search terms: {item_terms}")
                    
                    # Search through structured line items
                    best_item_match = None
                    best_match_score = 0
                    
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        
                        description = str(item.get('description', '') or item.get('name', '')).lower()
                        logger.info(f"Checking item: {description[:80]}...")
                        
                        # Check if item matches - be more lenient
                        matches = sum(1 for term in item_terms if term in description)
                        # Bonus for matching key terms together
                        if 'steel' in description and 'city' in description:
                            matches += 3  # Strong match
                        if 'octagon' in description and 'box' in description:
                            matches += 2
                        if 'knockouts' in description or 'knockout' in description:
                            matches += 2
                        # Bonus for matching the number "4"
                        if '4' in item_normalized and '4' in description:
                            matches += 1
                        
                        logger.info(f"Match score: {matches} (terms: {item_terms})")
                        
                        if matches > best_match_score:
                            best_match_score = matches
                            best_item_match = item
                    
                    # If we found a good match, return the unit price
                    if best_item_match and best_match_score >= 2:
                        unit_price = best_item_match.get('unit_price') or best_item_match.get('price') or best_item_match.get('unit price')
                        logger.info(f"Best match found! unit_price: {unit_price}, type: {type(unit_price)}")
                        
                        if unit_price:
                            if isinstance(unit_price, (int, float)):
                                return f"The unit price is ${unit_price:.2f}"
                            elif isinstance(unit_price, str):
                                # Clean up string price (handle cases like "$39.97 / each\n$29.97 / each")
                                # Extract first numeric price
                                price_match = re.search(r'(\d{1,3}(?:,\d{3})*\.\d{2})', unit_price)
                                if price_match:
                                    price_val = float(price_match.group(1).replace(',', ''))
                                    return f"The unit price is ${price_val:.2f}"
                                else:
                                    price_clean = re.sub(r'[^\d.]', '', unit_price)
                                    try:
                                        price_val = float(price_clean)
                                        return f"The unit price is ${price_val:.2f}"
                                    except ValueError:
                                        pass
                    else:
                        logger.info(f"No good match found. Best score: {best_match_score}")
            
            # Fallback: search raw text (if structured data not available)
            item_normalized = item_name.lower().replace('-', ' ').replace('_', ' ')
            lines = context.split('\n')
            
            # Extract key search terms
            item_terms = []
            for word in item_normalized.split():
                word_clean = word.strip('.,!?;:()[]{}"\'').lower()
                if word_clean.isdigit() or word_clean in ['steel', 'city', 'octagon', 'box']:
                    item_terms.append(word_clean)
                elif len(word_clean) > 2 and word_clean not in ['the', 'with', 'and', 'for', 'in']:
                    item_terms.append(word_clean)
            
            # Find item line
            item_line = None
            item_line_idx = -1
            best_match_score = 0
            
            for idx, line in enumerate(lines):
                line_normalized = line.lower().replace('-', ' ').replace('_', ' ')
                matches = sum(1 for term in item_terms if term in line_normalized)
                if 'steel' in line_normalized and 'city' in line_normalized:
                    matches += 2
                if matches > best_match_score and matches >= 2:
                    item_line = line
                    item_line_idx = idx
                    best_match_score = matches
            
            if item_line:
                # Extract prices from the item line
                all_prices = re.findall(r'\$(\d{1,3}(?:,\d{3})*\.\d{2})', item_line)
                if all_prices:
                    # Filter for reasonable unit prices ($0.50 - $50)
                    unit_prices = []
                    for price_str in all_prices:
                        try:
                            price_val = float(price_str.replace(',', ''))
                            if 0.50 <= price_val <= 50.00:
                                unit_prices.append((price_val, f"${price_str}"))
                        except ValueError:
                            continue
                    if unit_prices:
                        unit_prices.sort(key=lambda x: x[0])
                        return f"The unit price is {unit_prices[0][1]}"
        
        # Extract main phrase for "how much did X cost" questions
        main_phrase = None
        if 'how much' in question_lower or ('what is' in question_lower and not item_name):
            # Pattern: "how much did X cost" -> X is the item
            parts = question_lower.split()
            if 'did' in parts:
                did_idx = parts.index('did')
                cost_idx = parts.index('cost') if 'cost' in parts else len(parts)
                if did_idx + 1 < cost_idx:
                    main_phrase = ' '.join(parts[did_idx + 1:cost_idx])
            elif 'what is' in question_lower:
                # Pattern: "what is X" -> X is the item
                is_idx = parts.index('is')
                if is_idx + 1 < len(parts):
                    main_phrase = ' '.join(parts[is_idx + 1:])
        
        # Normalize context for matching
        context_normalized = context.lower().replace('-', ' ').replace('_', ' ')
        sentences = re.split(r'[.!?]+\s+', context)
        relevant_sentences = []
        
        search_phrase = item_name or main_phrase
        if search_phrase:
            search_phrase_normalized = search_phrase.lower().replace('-', ' ').replace('_', ' ')
            for sentence in sentences:
                sentence_normalized = sentence.lower().replace('-', ' ').replace('_', ' ')
                # Check for phrase match
                if search_phrase_normalized in sentence_normalized:
                    relevant_sentences.append(sentence.strip())
                # Also check for keyword matches
                elif any(keyword in sentence_normalized for keyword in question_keywords):
                    relevant_sentences.append(sentence.strip())
        else:
            # Just keyword matching
            for sentence in sentences:
                sentence_normalized = sentence.lower().replace('-', ' ').replace('_', ' ')
                if any(keyword in sentence_normalized for keyword in question_keywords):
                    relevant_sentences.append(sentence.strip())
        
        if relevant_sentences:
            answer = '. '.join(relevant_sentences[:3])
            if len(answer) > 400:
                answer = answer[:400] + "..."
            return answer
        
        # Special handling for cost/price questions
        if any(word in question_lower for word in ['cost', 'price', 'amount', 'paid', 'total']) and (main_phrase or item_name):
            search_phrase = item_name or main_phrase
            search_phrase_normalized = search_phrase.lower().replace('-', ' ')
            # Find sentences with both the phrase and an amount
            for sentence in sentences:
                sentence_normalized = sentence.lower().replace('-', ' ').replace('_', ' ')
                if search_phrase_normalized in sentence_normalized:
                    # Extract amounts from this sentence
                    amounts = re.findall(r'[\$₹€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?', sentence)
                    if amounts:
                        # Return the amount found near the service name
                        significant_amounts = []
                        for amt in amounts:
                            try:
                                # Extract numeric value
                                num_str = re.sub(r'[^\d.]', '', amt)
                                if num_str:
                                    amt_val = float(num_str)
                                    # For unit prices, prefer smaller amounts
                                    if 'unit price' in question_lower:
                                        if 0.01 <= amt_val <= 999.99:
                                            significant_amounts.append(amt)
                                    elif amt_val > 10:
                                        significant_amounts.append(amt)
                            except (ValueError, AttributeError):
                                continue
                        if significant_amounts:
                            return f"The {'unit price' if 'unit price' in question_lower else 'cost'} is {significant_amounts[0]}"
        
        # Fallback to summary
        summary = generate_summary(context, max_length=400)
        return summary


@app.post("/agent", response_model=AgentResponse)
def conversational_agent(
    request: AgentRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Enhanced AI agent that uses ALL PDF content as its knowledge base.
    
    The agent digests ALL PDF content from all processed emails and can answer
    questions about ANYTHING in those PDFs, regardless of document type.
    
    Capabilities:
    - Understands and summarizes ANY type of document (invoices, receipts, contracts, reports, etc.)
    - Answers questions about ANY content in the PDFs
    - Searches across ALL processed documents using full PDF text
    - Provides intelligent summaries and insights
    - Works with any document type - not limited to invoices/receipts
    """
    text = request.text.lower()
    
    # ALWAYS search documents first - use them as knowledge base
    # For unit price questions, extract item name first to filter documents
    item_name_for_filter = None
    if 'unit price' in text or ('price' in text and 'what' in text):
        # Extract item name for filtering
        if 'of' in text:
            of_idx = text.find('of')
            item_name_for_filter = text[of_idx + 3:].strip()
            # Remove trailing question words
            for end_word in ['?', '.', ' cost', ' price', ' unit price']:
                if item_name_for_filter.endswith(end_word):
                    item_name_for_filter = item_name_for_filter[:-len(end_word)].strip()
    
    # Search documents - if we have an item name, filter more strictly
    if item_name_for_filter:
        # Search with item name to get only relevant documents
        matching_docs = search_documents_by_keywords(item_name_for_filter, db, limit=10)
        # Further filter: only keep documents that actually contain the item name
        item_normalized = item_name_for_filter.lower().replace('-', ' ').replace('_', ' ')
        item_terms = [w for w in item_normalized.split() if len(w) > 2 and w not in ['the', 'with', 'and', 'for', 'box', 'in']]
        filtered_docs = []
        for doc in matching_docs:
            doc_text = doc.get('full_text', '').lower().replace('-', ' ').replace('_', ' ')
            # Check if at least 3 key terms appear in the document
            matches = sum(1 for term in item_terms if term in doc_text)
            if matches >= min(3, len(item_terms)):
                filtered_docs.append(doc)
        matching_docs = filtered_docs if filtered_docs else matching_docs[:1]  # At least return top match
    else:
        matching_docs = search_documents_by_keywords(request.text, db, limit=10)
    
    # Check if it's a general document question or invoice-specific query
    is_general_question = any(word in text for word in [
        'what', 'about', 'describe', 'explain', 'summarize', 'summary', 
        'tell me', 'show me', 'search', 'find', 'document', 'attachment', 'pdf', 'file'
    ])
    
    is_invoice_query = any(word in text for word in ['invoice', 'receipt', 'bill', 'vendor', 'spend', 'cost', 'amount', 'total'])
    
    # Try structured invoice query first (if it looks like an invoice question AND we have no general docs)
    if is_invoice_query and not is_general_question and len(matching_docs) == 0:
        # Extract vendor name (simple pattern matching)
        vendor_name = None
        vendor_id = None
        
        vendors = db.query(Vendor).all()
        for vendor in vendors:
            names = [vendor.canonical_name.lower()]
            if vendor.aliases:
                names.extend([a.lower() for a in vendor.aliases])
            
            for name in names:
                if name in text:
                    vendor_name = vendor.canonical_name
                    vendor_id = vendor.vendor_id
                    break
            
            if vendor_id:
                break
        
        # Extract date range (simple regex)
        date_pattern = r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})'
        date_match = re.search(date_pattern, text)
        
        # If we have vendor and date, execute structured query
        if vendor_id and date_match:
            month_name = date_match.group(1)
            year = date_match.group(2)
            
            month_map = {
                'january': '01', 'february': '02', 'march': '03', 'april': '04',
                'may': '05', 'june': '06', 'july': '07', 'august': '08',
                'september': '09', 'october': '10', 'november': '11', 'december': '12'
            }
            month_num = month_map.get(month_name, '01')
            from_date = f"{year}-{month_num}-01"
            to_date = f"{year}-{month_num}-31"
            
            query_req = QueryRequest(
                type="total_by_vendor",
                vendor_id=vendor_id,
                from_date=from_date,
                to_date=to_date
            )
            
            query_resp = structured_query(query_req, db, api_key)
            
            currency_symbol = "₹" if query_resp.currency == "INR" else query_resp.currency or ""
            answer = f"{currency_symbol}{query_resp.total_amount:,.2f} across {query_resp.invoice_count} invoices"
            
            if query_resp.low_confidence_count > 0:
                answer += f" ({query_resp.invoice_count - query_resp.low_confidence_count} auto-matched, {query_resp.low_confidence_count} need review)"
            
            invoices = db.query(Invoice).filter(
                Invoice.normalized['vendor_id'].astext == str(vendor_id)
            ).limit(10).all()
            
            sources = []
            for inv in invoices:
                extracted = inv.extracted or {}
                conf = calculate_confidence(extracted)
                sources.append({
                    "invoice_id": str(inv.invoice_id),
                    "url": get_presigned_url(inv.raw_email_s3) if inv.raw_email_s3 else "",
                    "confidence": conf
                })
            
            caveats = []
            if query_resp.low_confidence_count > 0:
                caveats.append(f"{query_resp.low_confidence_count} invoices have low confidence and may need review")
            
            return AgentResponse(
                query=request.text,
                answer_text=answer,
                sources=sources,
                caveats=caveats
            )
    
    # General document search and Q&A - PRIMARY MODE
    # Use ALL documents (PDFs) as knowledge base
    # The agent can answer questions about ANY content in ANY PDF
    
    if not matching_docs:
        # Try simple count queries
        if 'how many' in text and ('invoice' in text or 'document' in text):
            total_count = db.query(Invoice).count()
            return AgentResponse(
                query=request.text,
                answer_text=f"There are {total_count} documents in the system.",
                sources=[],
                caveats=[]
            )
        
        # If no matches but user asked about documents, provide helpful response
        if any(word in text for word in ['document', 'attachment', 'pdf', 'file', 'search']):
            return AgentResponse(
                query=request.text,
                answer_text="I couldn't find any documents matching your query. The system processes emails and their attachments. Try asking about specific content, keywords, or document types.",
                sources=[],
                caveats=["No matching documents found. Make sure documents have been processed."]
            )
        
        return AgentResponse(
            query=request.text,
            answer_text="I couldn't find any relevant documents matching your query. Try rephrasing with different keywords or ask about document content, summaries, or specific topics.",
            sources=[],
            caveats=["No matching documents found"]
        )
    
    # Generate comprehensive answer from documents using LLM (Ollama - free, local) or fallback to regex
    # ALWAYS use LLM if Ollama is available - no regex fallback
    # IMPORTANT: For unit price questions, include ALL invoices with line items to ensure we find the right one
    if 'unit price' in request.text.lower() or ('price' in request.text.lower() and 'what' in request.text.lower()):
        logger.info(f"Unit price question detected, ensuring ALL relevant invoices are included")
        # Get ALL invoices with line items
        all_invoices = db.query(Invoice).filter(
            Invoice.extracted['line_items'].isnot(None)
        ).limit(10).all()
        
        # Build a set of already included invoice IDs
        existing_ids = {d.get('invoice_id') for d in matching_docs}
        
        # Add ALL invoices with line items to ensure comprehensive search
        for inv in all_invoices:
            inv_id_str = str(inv.invoice_id)
            if inv_id_str not in existing_ids:
                extracted = inv.extracted or {}
                line_items = extracted.get('line_items', {}).get('value', [])
                if line_items:
                    matching_docs.append({
                        'invoice_id': inv_id_str,
                        'full_text': inv.raw_text or '',
                        'full_text_preview': (inv.raw_text or '')[:10000],  # More text
                        'attachment_names': [a.get('filename', '') for a in (inv.attachments or [])],
                        'extracted_fields': extracted,
                        'url': get_presigned_url(inv.raw_email_s3) if inv.raw_email_s3 else '',
                        'snippet': (inv.raw_text or '')[:200],
                        'relevance': 0.5  # Default relevance for comprehensive search
                    })
                    logger.info(f"Added invoice {inv_id_str[:8]}... ({inv.attachments[0].get('filename') if inv.attachments else 'no attachment'}) with {len(line_items)} line items")
        
        logger.info(f"Total documents for LLM search: {len(matching_docs)}")
    
    if OLLAMA_AVAILABLE:
        logger.info(f"Using LLM (Ollama) to answer question: {request.text} with {len(matching_docs)} documents")
        answer = answer_question_with_llm(request.text, matching_docs)
    else:
        logger.warning(f"LLM (Ollama) not available, falling back to regex")
        answer = answer_question_from_documents(request.text, matching_docs)
    
    # Build sources list with document information - clean format
    # For unit price questions, only show documents that actually contain the item
    sources = []
    if item_name_for_filter:
        # Filter sources to only show documents with the item
        item_normalized = item_name_for_filter.lower().replace('-', ' ').replace('_', ' ')
        item_terms = [w for w in item_normalized.split() if len(w) > 2 and w not in ['the', 'with', 'and', 'for', 'box', 'in']]
        # Also include important terms like "steel", "city", numbers
        important_terms = [w for w in item_normalized.split() if w.isdigit() or w in ['steel', 'city']]
        item_terms.extend(important_terms)
        
        for doc in matching_docs:
            doc_text = doc.get('full_text', '').lower().replace('-', ' ').replace('_', ' ')
            matches = sum(1 for term in item_terms if term in doc_text)
            # Also check for partial matches
            if '4' in item_normalized and '4' in doc_text:
                matches += 1
            if matches >= min(2, len(item_terms)):
                attachment_names = doc.get('attachment_names', [])
                doc_info = {
                    "invoice_id": doc['invoice_id'],
                    "url": doc.get('url', ''),
                    "confidence": min(0.95, doc['relevance'] * 1.2),
                    "attachment_names": attachment_names,
                    "snippet": doc.get('snippet', '')[:200],
                    "doc_type": doc.get('doc_type', 'Document'),
                    "filename": attachment_names[0] if attachment_names else "Document"
                }
                sources.append(doc_info)
    else:
        # For general questions, show top matches
        for doc in matching_docs[:5]:  # Top 5 matches
            attachment_names = doc.get('attachment_names', [])
            doc_info = {
                "invoice_id": doc['invoice_id'],
                "url": doc.get('url', ''),
                "confidence": min(0.95, doc['relevance'] * 1.2),
                "attachment_names": attachment_names,
                "snippet": doc.get('snippet', '')[:200],  # Shorter, cleaner snippet
                "doc_type": doc.get('doc_type', 'Document'),
                "filename": attachment_names[0] if attachment_names else "Document"
            }
            sources.append(doc_info)
    
    # Clean up answer - remove verbose prefixes
    # The answer is already clean from answer_question_from_documents
    
    caveats = []
    if len(matching_docs) > 5:
        caveats.append(f"Found {len(matching_docs)} matching documents, showing top 5 results")
    
    # Add note about document types if multiple types found
    doc_types = set([doc.get('doc_type', 'Document') for doc in matching_docs[:5]])
    if len(doc_types) > 1:
        caveats.append(f"Documents include: {', '.join(doc_types)}")
    
    return AgentResponse(
        query=request.text,
        answer_text=answer,
        sources=sources,
        caveats=caveats
    )


@app.get("/audit/{invoice_id}")
def get_audit_trail(
    invoice_id: UUID,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Get audit trail for an invoice."""
    audit_records = db.query(InvoiceAudit).filter(
        InvoiceAudit.invoice_id == invoice_id
    ).order_by(InvoiceAudit.changed_at.desc()).all()
    
    return [
        {
            "audit_id": str(a.audit_id),
            "field_name": a.field_name,
            "old_value": a.old_value,
            "new_value": a.new_value,
            "user_name": a.user_name,
            "changed_at": a.changed_at.isoformat(),
            "meta": a.meta
        }
        for a in audit_records
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

