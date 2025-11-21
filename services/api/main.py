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
from sqlalchemy import and_, or_, func, cast, String
import re

from shared import get_db, Invoice, Vendor, Project, InvoiceAudit, s3_client, settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Invoice Processing API", version="1.0.0")
security = HTTPBearer()


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
    """Generate presigned URL for S3 object."""
    if not s3_path or not s3_path.startswith('s3://'):
        return ""
    
    try:
        bucket = s3_path.split('/')[2]
        key = '/'.join(s3_path.split('/')[3:])
        
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
        query = query.filter(Invoice.tags.contains([tag]))
    
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


@app.post("/agent", response_model=AgentResponse)
def conversational_agent(
    request: AgentRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Free-text conversational endpoint (MVP rule-based)."""
    text = request.text.lower()
    
    # Extract vendor name (simple pattern matching)
    vendor_name = None
    vendor_id = None
    
    # Try to find vendor mentions
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
    
    # Extract metric
    is_spend_query = any(word in text for word in ['spend', 'spent', 'cost', 'total', 'amount'])
    is_invoice_query = 'invoice' in text or 'invoices' in text
    
    # If we have vendor and date, execute query
    if vendor_id and date_match:
        month_name = date_match.group(1)
        year = date_match.group(2)
        
        # Convert month to date range (simplified)
        month_map = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12'
        }
        month_num = month_map.get(month_name, '01')
        from_date = f"{year}-{month_num}-01"
        to_date = f"{year}-{month_num}-31"
        
        # Execute query
        query_req = QueryRequest(
            type="total_by_vendor",
            vendor_id=vendor_id,
            from_date=from_date,
            to_date=to_date
        )
        
        query_resp = structured_query(query_req, db, api_key)
        
        # Build answer
        currency_symbol = "₹" if query_resp.currency == "INR" else query_resp.currency or ""
        answer = f"{currency_symbol}{query_resp.total_amount:,.2f} across {query_resp.invoice_count} invoices"
        
        if query_resp.low_confidence_count > 0:
            answer += f" ({query_resp.invoice_count - query_resp.low_confidence_count} auto-matched, {query_resp.low_confidence_count} need review)"
        
        # Get source invoices
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
    
    else:
        # Ambiguous query
        return AgentResponse(
            query=request.text,
            answer_text="I need more information. Please specify a vendor name and date range.",
            sources=[],
            caveats=["Could not extract vendor or date from query"]
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

