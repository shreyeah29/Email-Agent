"""Extraction worker - processes emails and extracts invoice fields."""
import os
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional
from decimal import Decimal

import pdfplumber
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image
import io

from sqlalchemy.orm import Session
from shared import (
    settings, s3_client, redis_client, SessionLocal,
    Invoice, ensure_s3_bucket
)

logging.basicConfig(level=getattr(logging, settings.log_level))
logger = logging.getLogger(__name__)

EXTRACTION_QUEUE = 'extraction_queue'


class InvoiceExtractor:
    """Extracts invoice fields from text and PDFs."""
    
    def __init__(self):
        self.patterns = {
            'invoice_number': [
                r'invoice\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',
                r'inv\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',
                r'bill\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',
                r'invoice\s+([A-Z0-9\-]+)',
            ],
            'date': [
                r'date\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                r'invoice\s+date\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            ],
            'total_amount': [
                r'total\s*(?:amount|due)?\s*:?\s*([\d,]+\.?\d*)',
                r'amount\s+due\s*:?\s*([\d,]+\.?\d*)',
                r'grand\s+total\s*:?\s*([\d,]+\.?\d*)',
                r'total\s*:?\s*([\d,]+\.?\d*)',
            ],
            'currency': [
                r'([A-Z]{3})\s*\d+\.?\d*',  # Currency code before amount
                r'[₹$€£]',  # Currency symbols
            ],
            'vendor_name': [
                r'^([A-Z][A-Za-z\s&.,]+(?:Pvt|Ltd|Inc|LLC|Corp|Corporation))',
            ],
        }
    
    def extract_text_from_pdf(self, pdf_bytes: bytes) -> tuple:
        """Extract text from PDF - tries digital first, then OCR."""
        text_parts = []
        line_items = []
        
        try:
            # Try digital PDF extraction first
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(f"--- Page {page_num} ---\n{page_text}")
                    
                    # Try to extract tables (for line items)
                    tables = page.extract_tables()
                    for table in tables:
                        if table and len(table) > 1:
                            # Assume first row is header
                            headers = [str(cell or '').strip().lower() for cell in table[0]]
                            for row in table[1:]:
                                if any(cell for cell in row):
                                    item = {}
                                    for i, cell in enumerate(row):
                                        if i < len(headers) and cell:
                                            item[headers[i]] = str(cell).strip()
                                    if item:
                                        line_items.append(item)
            
            full_text = "\n".join(text_parts)
            
            # If digital extraction yielded little text, try OCR
            if len(full_text.strip()) < 100:
                logger.info("Digital extraction yielded little text, trying OCR...")
                images = convert_from_bytes(pdf_bytes, dpi=200)
                ocr_texts = []
                for img in images:
                    ocr_text = pytesseract.image_to_string(img)
                    ocr_texts.append(ocr_text)
                full_text = "\n".join(ocr_texts)
            
            return full_text, line_items
            
        except Exception as e:
            logger.error(f"Error extracting text from PDF: {e}")
            # Fallback to OCR
            try:
                images = convert_from_bytes(pdf_bytes, dpi=200)
                ocr_texts = []
                for img in images:
                    ocr_text = pytesseract.image_to_string(img)
                    ocr_texts.append(ocr_text)
                return "\n".join(ocr_texts), []
            except Exception as e2:
                logger.error(f"OCR also failed: {e2}")
                return "", []
    
    def extract_text_from_image(self, image_bytes: bytes) -> str:
        """Extract text from image using OCR."""
        try:
            image = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(image)
            return text
        except Exception as e:
            logger.error(f"Error extracting text from image: {e}")
            return ""
    
    def extract_field(self, field_name: str, text: str, context: Dict = None) -> Optional[Dict]:
        """Extract a specific field using regex patterns."""
        patterns = self.patterns.get(field_name, [])
        context = context or {}
        
        for pattern in patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                value = match.group(1) if match.groups() else match.group(0)
                
                # Clean up value
                if field_name == 'total_amount':
                    value = value.replace(',', '')
                    try:
                        value = float(value)
                    except:
                        continue
                
                # Find snippet context
                start = max(0, match.start() - 50)
                end = min(len(text), match.end() + 50)
                snippet = text[start:end].strip()
                
                return {
                    "value": value,
                    "confidence": 0.85,  # Base confidence for regex matches
                    "provenance": {
                        "method": "regex",
                        "pattern": pattern,
                        "snippet": snippet,
                        **context
                    }
                }
        
        return None
    
    def extract_all_fields(self, text: str, attachments: List[Dict]) -> Dict[str, Any]:
        """Extract all invoice fields from text."""
        extracted = {}
        
        # Extract basic fields
        for field_name in ['invoice_number', 'date', 'total_amount', 'currency', 'vendor_name']:
            result = self.extract_field(field_name, text)
            if result:
                extracted[field_name] = result
        
        # Extract vendor name from first line (common pattern)
        lines = text.split('\n')
        for line in lines[:10]:  # Check first 10 lines
            vendor_match = re.match(r'^([A-Z][A-Za-z\s&.,]+(?:Pvt|Ltd|Inc|LLC|Corp|Corporation))', line.strip())
            if vendor_match and 'vendor_name' not in extracted:
                extracted['vendor_name'] = {
                    "value": vendor_match.group(1).strip(),
                    "confidence": 0.90,
                    "provenance": {"method": "header_pattern", "snippet": line.strip()}
                }
                break
        
        # Extract line items if available
        # This would be enhanced with table extraction from PDFs
        
        return extracted
    
    def process_email(self, email_data: Dict, attachments: List[Dict]) -> tuple:
        """Process email and attachments to extract invoice data."""
        # Extract email body text
        email_body = ""
        if 'payload' in email_data:  # Gmail
            def extract_body(part):
                text = ""
                if part.get('mimeType') == 'text/plain':
                    import base64
                    data = part.get('body', {}).get('data', '')
                    if data:
                        text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                parts = part.get('parts', [])
                for p in parts:
                    text += "\n" + extract_body(p)
                return text
            email_body = extract_body(email_data.get('payload', {}))
        elif 'body' in email_data:  # Outlook
            from bs4 import BeautifulSoup
            body_content = email_data.get('body', {}).get('content', '')
            soup = BeautifulSoup(body_content, 'html.parser')
            email_body = soup.get_text()
        
        all_text = [email_body]
        all_line_items = []
        
        # Process attachments
        for att_info in attachments:
            s3_url = att_info.get('url', '')
            if not s3_url.startswith('s3://'):
                continue
            
            # Parse S3 URL
            bucket = s3_url.split('/')[2]
            key = '/'.join(s3_url.split('/')[3:])
            
            try:
                response = s3_client.get_object(Bucket=bucket, Key=key)
                file_bytes = response['Body'].read()
                filename = att_info.get('filename', '')
                
                if filename.lower().endswith('.pdf'):
                    text, line_items = self.extract_text_from_pdf(file_bytes)
                    all_text.append(f"\n--- Attachment: {filename} ---\n{text}")
                    all_line_items.extend(line_items)
                elif filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff')):
                    text = self.extract_text_from_image(file_bytes)
                    all_text.append(f"\n--- Attachment: {filename} ---\n{text}")
            except Exception as e:
                logger.error(f"Error processing attachment {filename}: {e}")
        
        # Combine all text
        full_text = "\n".join(all_text)
        
        # Extract fields
        extracted = self.extract_all_fields(full_text, attachments)
        
        # Add line items if found
        if all_line_items:
            extracted['line_items'] = {
                "value": all_line_items,
                "confidence": 0.85,
                "provenance": {"method": "table_extraction"}
            }
        
        return full_text, extracted


def process_extraction_job(job_data: Dict, db: Session) -> bool:
    """Process a single extraction job."""
    try:
        email_id = job_data['email_id']
        s3_raw = job_data['s3_raw']
        
        # Download raw email from S3
        bucket = s3_raw.split('/')[2]
        key = '/'.join(s3_raw.split('/')[3:])
        
        response = s3_client.get_object(Bucket=bucket, Key=key)
        email_data = json.loads(response['Body'].read().decode('utf-8'))
        
        # Process attachments
        attachments = []
        for att_url in job_data.get('attachments', []):
            if att_url.startswith('s3://'):
                bucket = att_url.split('/')[2]
                key = '/'.join(att_url.split('/')[3:])
                filename = key.split('/')[-1]
                attachments.append({
                    "filename": filename,
                    "url": att_url,
                    "type": "application/pdf"  # Simplified
                })
        
        # Extract invoice data
        extractor = InvoiceExtractor()
        raw_text, extracted = extractor.process_email(email_data, attachments)
        
        # Calculate average confidence
        confidences = [v.get('confidence', 0) for v in extracted.values() if isinstance(v, dict)]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
        
        # Create invoice record
        invoice = Invoice(
            invoice_id=uuid.uuid4(),
            source_email_id=email_id,
            raw_email_s3=s3_raw,
            attachments=attachments,
            raw_text=raw_text,
            extracted=extracted,
            normalized={},
            tags=[],
            extractor_version=settings.extractor_version,
            reconciliation_status='needs_review',
            extra={"avg_confidence": avg_confidence}
        )
        
        db.add(invoice)
        db.commit()
        
        # Save extraction JSON to S3
        extraction_json = {
            "invoice_id": str(invoice.invoice_id),
            "extracted": extracted,
            "raw_text": raw_text[:1000],  # First 1000 chars
            "extracted_at": datetime.now().isoformat()
        }
        
        s3_key = f"inbox/extraction/{invoice.invoice_id}.json"
        s3_client.put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=json.dumps(extraction_json).encode('utf-8'),
            ContentType='application/json'
        )
        
        logger.info(f"Extracted invoice {invoice.invoice_id} with {len(extracted)} fields")
        return True
        
    except Exception as e:
        logger.error(f"Error processing extraction job: {e}")
        db.rollback()
        return False


def run_extractor_worker():
    """Main extraction worker loop."""
    ensure_s3_bucket()
    logger.info("Starting extraction worker...")
    
    while True:
        try:
            # Get job from queue (blocking)
            job_json = redis_client.brpop(EXTRACTION_QUEUE, timeout=10)
            
            if job_json:
                job_data = json.loads(job_json[1])
                logger.info(f"Processing extraction job for email {job_data.get('email_id')}")
                
                db = SessionLocal()
                try:
                    process_extraction_job(job_data, db)
                finally:
                    db.close()
            
        except KeyboardInterrupt:
            logger.info("Extraction worker stopped")
            break
        except Exception as e:
            logger.error(f"Error in extraction worker: {e}")


if __name__ == "__main__":
    run_extractor_worker()

