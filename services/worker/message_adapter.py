"""Adapter to process single Gmail messages through the extraction pipeline.

Additive — does not modify existing behavior.
This adapter calls existing extractor functions without modifying them.
"""
import os
import json
import logging
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path

from shared import SessionLocal, Invoice, s3_client, settings, ensure_s3_bucket
from services.ingestion.gmail_helpers import fetch_message_body_and_attachments, get_gmail_service
from services.extractor.worker import InvoiceExtractor

logger = logging.getLogger(__name__)


def process_message_by_id(message_id: str, force: bool = False) -> Dict[str, Any]:
    """Process a single Gmail message through the extraction pipeline.
    
    Additive — does not modify existing behavior.
    This function:
    1. Fetches message body and attachments from Gmail
    2. Stages files locally
    3. Invokes existing extractor pipeline
    4. Stores results in database and S3
    5. Returns structured result
    
    Implements idempotency: if message already processed, returns existing result.
    
    Args:
        message_id: Gmail message ID
        force: If True, reprocess even if already processed
        
    Returns:
        Dict with:
        - message_id
        - invoice_records: List of extracted invoice data
        - summary_text: Human-readable summary
        - provenance_path: Path to stored provenance data
        - status: "success" or "failed"
        - confidence: Average confidence score
    """
    # Idempotency check: if already processed, return existing result
    if not force:
        db = SessionLocal()
        try:
            existing = db.query(Invoice).filter(Invoice.source_email_id == message_id).first()
            if existing:
                logger.info(f"Message {message_id} already processed, returning existing result")
                extracted = existing.extracted or {}
                normalized = existing.normalized or {}
                
                vendor_name = normalized.get('vendor_name') or extracted.get('vendor_name', {}).get('value') if isinstance(extracted.get('vendor_name'), dict) else None
                invoice_date = normalized.get('date') or extracted.get('date', {}).get('value') if isinstance(extracted.get('date'), dict) else None
                total_amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value') if isinstance(extracted.get('total_amount'), dict) else None
                currency = normalized.get('currency') or extracted.get('currency', {}).get('value') if isinstance(extracted.get('currency'), dict) else None
                line_items = normalized.get('line_items') or extracted.get('line_items', {}).get('value', []) if isinstance(extracted.get('line_items'), dict) else []
                
                confidences = [v.get('confidence', 0) for v in extracted.values() if isinstance(v, dict) and 'confidence' in v]
                avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
                
                summary_parts = []
                if vendor_name:
                    summary_parts.append(f"Vendor: {vendor_name}")
                if invoice_date:
                    summary_parts.append(f"Date: {invoice_date}")
                if total_amount:
                    summary_parts.append(f"Total: {currency or ''} {total_amount}")
                summary_text = " | ".join(summary_parts) if summary_parts else "Invoice already processed"
                
                return {
                    "message_id": message_id,
                    "invoice_records": [{
                        "vendor": vendor_name,
                        "date": invoice_date,
                        "total_amount": total_amount,
                        "currency": currency,
                        "line_items": line_items,
                        "confidence": avg_confidence
                    }],
                    "summary_text": summary_text,
                    "provenance_path": f"inbox/extraction/{existing.invoice_id}.json",
                    "status": "success",
                    "confidence": avg_confidence,
                    "invoice_id": str(existing.invoice_id),
                    "already_processed": True
                }
        finally:
            db.close()
    
    try:
        ensure_s3_bucket()
        
        # Create staging directory
        staging_base = Path("data/staging")
        staging_base.mkdir(parents=True, exist_ok=True)
        staging_dir = str(staging_base / message_id)
        
        # Fetch message and attachments
        logger.info(f"Fetching message {message_id} from Gmail...")
        # Get Gmail service to reuse credentials
        gmail_service = get_gmail_service()
        staged_data = fetch_message_body_and_attachments(message_id, staging_dir=staging_dir, service=gmail_service)
        
        email_data = staged_data['email_data']
        attachments = staged_data['attachments']
        raw_text = staged_data['raw_text']
        
        # Save raw email to S3
        s3_key = f"inbox/raw/{message_id}.json"
        with open(staged_data['email_json'], 'rb') as f:
            s3_client.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                Body=f.read(),
                ContentType='application/json'
            )
        
        # Process attachments and save to S3
        attachment_info = []
        for att_path in attachments:
            filename = os.path.basename(att_path)
            s3_att_key = f"inbox/attachments/{message_id}/{filename}"
            
            with open(att_path, 'rb') as f:
                s3_client.put_object(
                    Bucket=settings.s3_bucket,
                    Key=s3_att_key,
                    Body=f.read(),
                    ContentType='application/octet-stream'
                )
            
            attachment_info.append({
                "filename": filename,
                "url": f"s3://{settings.s3_bucket}/{s3_att_key}",
                "type": "application/pdf" if filename.lower().endswith('.pdf') else "application/octet-stream"
            })
        
        # Use the full extraction pipeline (includes categorization)
        extractor = InvoiceExtractor()
        
        # Process attachments for line items and text extraction
        all_line_items = []
        pdf_texts = []
        
        for att_path in attachments:
            filename = os.path.basename(att_path)
            if filename.lower().endswith('.pdf'):
                try:
                    with open(att_path, 'rb') as f:
                        file_bytes = f.read()
                    text, line_items = extractor.extract_text_from_pdf(file_bytes)
                    pdf_texts.append(f"--- Attachment: {filename} ---\n{text}")
                    all_line_items.extend(line_items)
                except Exception as e:
                    logger.warning(f"Error extracting from PDF {att_path}: {e}")
            elif filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff')):
                try:
                    with open(att_path, 'rb') as f:
                        file_bytes = f.read()
                    text = extractor.extract_text_from_image(file_bytes)
                    pdf_texts.append(f"--- Attachment: {filename} ---\n{text}")
                except Exception as e:
                    logger.warning(f"Error extracting from image {att_path}: {e}")
        
        # Combine PDF/attachment text for extraction
        pdf_only_text = "\n".join(pdf_texts) if pdf_texts else ""
        
        # Extract fields from PDF content only
        try:
            extracted = extractor.extract_all_fields(pdf_only_text, attachment_info)
        except Exception as extract_error:
            logger.error(f"Error in extract_all_fields: {extract_error}")
            extracted = {}
        
        # Ensure extracted is a dict (fallback if extraction fails)
        if not extracted or not isinstance(extracted, dict):
            extracted = {}
        
        # Add categorized line items if found
        if all_line_items:
            try:
                # Categorize items and assign BOM numbers
                from services.extractor.categorizer import categorize_items_with_ollama
                categorized_items = categorize_items_with_ollama(all_line_items)
                
                if not extracted:
                    extracted = {}
                
                extracted['line_items'] = {
                    "value": categorized_items,
                    "confidence": 0.85,
                    "provenance": {"method": "table_extraction_with_categorization"}
                }
            except Exception as cat_error:
                logger.warning(f"Error categorizing items, using uncategorized: {cat_error}")
                # Fallback: use uncategorized items
                if not extracted:
                    extracted = {}
                extracted['line_items'] = {
                    "value": all_line_items,
                    "confidence": 0.85,
                    "provenance": {"method": "table_extraction"}
                }
        
        # For raw_text, combine email body with PDF content
        full_text_with_email = raw_text + "\n" + pdf_only_text if pdf_only_text else raw_text
        
        # Calculate confidence
        confidences = [v.get('confidence', 0) for v in extracted.values() if isinstance(v, dict) and 'confidence' in v]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
        
        # Create invoice record
        invoice_id = uuid.uuid4()
        invoice = Invoice(
            invoice_id=invoice_id,
            source_email_id=message_id,
            raw_email_s3=f"s3://{settings.s3_bucket}/{s3_key}",
            attachments=attachment_info,
            raw_text=full_text_with_email,
            extracted=extracted,
            normalized={},
            tags=[],
            extractor_version=settings.extractor_version,
            reconciliation_status='needs_review',
            extra={"avg_confidence": avg_confidence}
        )
        
        db = SessionLocal()
        try:
            db.add(invoice)
            db.commit()
            
            # Save extraction JSON to S3
            extraction_json = {
                "invoice_id": str(invoice_id),
                "message_id": message_id,
                "extracted": extracted,
                "raw_text": raw_text[:1000],
                "extracted_at": datetime.now().isoformat()
            }
            
            s3_extraction_key = f"inbox/extraction/{invoice_id}.json"
            s3_client.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_extraction_key,
                Body=json.dumps(extraction_json).encode('utf-8'),
                ContentType='application/json'
            )
            
            # Build invoice records for response
            invoice_records = []
            vendor_name = extracted.get('vendor_name', {}).get('value') if isinstance(extracted.get('vendor_name'), dict) else None
            invoice_date = extracted.get('date', {}).get('value') if isinstance(extracted.get('date'), dict) else None
            total_amount = extracted.get('total_amount', {}).get('value') if isinstance(extracted.get('total_amount'), dict) else None
            currency = extracted.get('currency', {}).get('value') if isinstance(extracted.get('currency'), dict) else None
            line_items = extracted.get('line_items', {}).get('value', []) if isinstance(extracted.get('line_items'), dict) else []
            
            invoice_records.append({
                "vendor": vendor_name,
                "date": invoice_date,
                "total_amount": total_amount,
                "currency": currency,
                "line_items": line_items,
                "confidence": avg_confidence
            })
            
            # Build summary
            summary_parts = []
            if vendor_name:
                summary_parts.append(f"Vendor: {vendor_name}")
            if invoice_date:
                summary_parts.append(f"Date: {invoice_date}")
            if total_amount:
                summary_parts.append(f"Total: {currency or ''} {total_amount}")
            summary_text = " | ".join(summary_parts) if summary_parts else "Invoice extracted with low confidence"
            
            return {
                "message_id": message_id,
                "invoice_records": invoice_records,
                "summary_text": summary_text,
                "provenance_path": s3_extraction_key,
                "status": "success",
                "confidence": avg_confidence,
                "invoice_id": str(invoice_id)
            }
        
        finally:
            db.close()
    
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Error processing message {message_id}: {e}")
        logger.error(f"Full traceback:\n{error_trace}")
        return {
            "message_id": message_id,
            "invoice_records": [],
            "summary_text": f"Processing failed: {str(e)}",
            "provenance_path": None,
            "status": "failed",
            "confidence": 0.0
        }

