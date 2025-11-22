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

from shared import get_db, Invoice, Vendor, Project, InvoiceAudit, s3_client, settings
from services.api.candidates import router as candidates_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Invoice Processing API", version="1.0.0")
security = HTTPBearer()

# Include candidates router
app.include_router(candidates_router)


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
    """Generate presigned URL for S3 object, replacing internal hostname with localhost for browser access."""
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
        # Replace internal Docker hostname with localhost for browser access
        # This handles cases where S3_ENDPOINT_URL is set to http://minio:9000 (internal)
        # but we need http://localhost:9000 for browser access
        if url and 'minio:9000' in url:
            url = url.replace('minio:9000', 'localhost:9000')
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
    """Search all documents by keywords in raw_text and return relevant matches with FULL text."""
    query_lower = query_text.lower()
    keywords = [w.strip() for w in query_lower.split() if len(w.strip()) > 2]  # Filter short words
    
    if not keywords:
        return []
    
    # Get all invoices with raw_text
    all_invoices = db.query(Invoice).filter(
        Invoice.raw_text.isnot(None),
        Invoice.raw_text != ''
    ).all()
    
    matches = []
    for inv in all_invoices:
        if not inv.raw_text:
            continue
        
        raw_text_lower = inv.raw_text.lower()
        full_text = inv.raw_text  # Keep full text for better understanding
        
        # Count keyword matches
        match_count = sum(1 for keyword in keywords if keyword in raw_text_lower)
        if match_count == 0:
            continue
        
        # Calculate relevance score (keyword density + position bonus)
        relevance = match_count / len(keywords)
        
        # Bonus for matches in first 500 chars (title/header area)
        if any(keyword in raw_text_lower[:500] for keyword in keywords):
            relevance += 0.2
        
        # Extract snippet around first match
        snippet = ""
        for keyword in keywords:
            idx = raw_text_lower.find(keyword)
            if idx != -1:
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


def answer_question_from_documents(question: str, documents: List[Dict]) -> str:
    """Answer a question based on FULL document content - works for any document type."""
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
    
    # Use full text for comprehensive understanding
    context = full_text if full_text else snippet
    
    if not context:
        return "The document doesn't contain readable text content."
    
    # Answer based on question type
    if any(word in question_lower for word in ['what', 'about', 'describe', 'explain', 'tell me about']):
        # Summary/description question - use full text for comprehensive answer
        summary = generate_summary(context, max_length=600)
        doc_info = f"This appears to be a {doc_type.lower()}"
        if attachment_names:
            doc_info += f" from file(s): {', '.join(attachment_names[:2])}"
        return f"{doc_info}. {summary}"
    
    elif any(word in question_lower for word in ['summarize', 'summary']):
        # Explicit summary request
        summary = generate_summary(context, max_length=700)
        return f"**Summary of the document:**\n\n{summary}"
    
    elif any(word in question_lower for word in ['how many', 'count', 'number']):
        # Count question - look for numbers in context
        numbers = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\b', context)
        if numbers:
            # Filter out very small numbers (likely not the answer)
            significant_numbers = [n for n in numbers if float(n.replace(',', '')) > 10]
            if significant_numbers:
                return f"Based on the document, I found these significant numbers: {', '.join(significant_numbers[:5])}."
            return f"I found {len(numbers)} number(s) in the document: {', '.join(numbers[:5])}."
        return "I couldn't find specific numbers in the document."
    
    elif any(word in question_lower for word in ['when', 'date', 'time']):
        # Date question
        dates = re.findall(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}', context)
        if dates:
            return f"Found dates in the document: {', '.join(dates[:5])}."
        return "I couldn't find specific dates in the document."
    
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
        # Extract keywords from question
        question_keywords = [w.strip() for w in question_lower.split() if len(w.strip()) > 3]
        
        # Find sentences containing question keywords
        sentences = re.split(r'[.!?]+\s+', context)
        relevant_sentences = []
        for sentence in sentences:
            sentence_lower = sentence.lower()
            if any(keyword in sentence_lower for keyword in question_keywords):
                relevant_sentences.append(sentence.strip())
        
        if relevant_sentences:
            answer = '. '.join(relevant_sentences[:3])
            if len(answer) > 500:
                answer = answer[:500] + "..."
            return f"Based on the document content: {answer}"
        
        # Fallback to summary
        summary = generate_summary(context, max_length=500)
        return f"Here's what the document contains: {summary}"


@app.post("/agent", response_model=AgentResponse)
def conversational_agent(
    request: AgentRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Enhanced AI agent that uses ALL documents/attachments as its knowledge base.
    
    The agent can:
    - Understand and summarize ANY type of document (not just invoices)
    - Answer questions about document content
    - Search across all processed documents
    - Provide intelligent summaries and insights
    """
    text = request.text.lower()
    
    # ALWAYS search documents first - use them as knowledge base
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
    # Use ALL documents as knowledge base
    
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
    
    # Generate comprehensive answer from documents using FULL text
    answer = answer_question_from_documents(request.text, matching_docs)
    
    # Build sources list with document information
    sources = []
    for doc in matching_docs[:5]:  # Top 5 matches
        doc_info = {
            "invoice_id": doc['invoice_id'],
            "url": doc.get('url', ''),
            "confidence": min(0.95, doc['relevance'] * 1.2),
            "attachment_names": doc.get('attachment_names', []),
            "snippet": doc.get('snippet', '')[:300],  # Longer snippet
            "doc_type": doc.get('doc_type', 'Document')
        }
        sources.append(doc_info)
    
    # Enhance answer with document type information
    if matching_docs:
        best_doc = matching_docs[0]
        doc_type = best_doc.get('doc_type', 'Document')
        attachment_names = best_doc.get('attachment_names', [])
        
        # Add document context to answer
        if attachment_names and not answer.startswith("This appears to be"):
            answer = f"[Document: {', '.join(attachment_names[:2])}] {answer}"
    
    # Add summary if explicitly requested
    if any(word in text for word in ['summarize', 'summary']):
        if matching_docs:
            best_doc = matching_docs[0]
            full_text = best_doc.get('full_text', '') or best_doc.get('full_text_preview', '')
            if full_text:
                summary = generate_summary(full_text, max_length=700)
                answer = f"**Document Summary:**\n{summary}\n\n**Answer to your question:**\n{answer}"
    
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

