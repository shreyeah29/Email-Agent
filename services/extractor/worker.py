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
                r'order\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',  # For order numbers like H8551-451363
                r'order\s*#?\s*:?\s*([A-Z0-9\-]+)',
                r'receipt\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',
            ],
            'date': [
                r'date\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                r'invoice\s+date\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            ],
            'total_amount': [
                # PRIORITY 1: Order Total (most specific, highest priority for receipts)
                r'order\s+total\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'order\s+total\s+\$?\s*([\d,]+\.?\d*)',
                # PRIORITY 2: Grand Total, Amount Due, Balance Due (final totals)
                r'grand\s+total\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'amount\s+due\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'balance\s+due\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'charged\s*:?\s*\$?\s*([\d,]+\.?\d*)',  # "Charged: $326.18"
                r'paid\s*\$?\s*([\d,]+\.?\d*)',  # For receipts showing "Paid $485.00"
                # PRIORITY 3: Generic totals (lower priority - might match subtotal)
                r'total\s*(?:amount|due)?\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'invoice\s+total\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'total\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'\$\s*([\d,]+\.?\d*)\s*(?:total|paid|due)',  # Dollar amount followed by total/paid/due
            ],
            'subtotal': [
                r'subtotal\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'sub\s+total\s*:?\s*\$?\s*([\d,]+\.?\d*)',
            ],
            'tax': [
                r'sales\s+tax\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'tax\s*:?\s*\$?\s*([\d,]+\.?\d*)',
                r'tax\s+amount\s*:?\s*\$?\s*([\d,]+\.?\d*)',
            ],
            'currency': [
                r'([A-Z]{3})\s*\d+\.?\d*',  # Currency code before amount
                r'[₹$€£]',  # Currency symbols
            ],
            'vendor_name': [
                r'^([A-Z][A-Za-z\s&.,]+(?:Pvt|Ltd|Inc|LLC|Corp|Corporation|Company))',
                r'^([A-Z][A-Z\s&.,]+(?:DEPOT|RECON|CONSTRUCTION|RECYCLING|SUPPLIES|SERVICES))',  # For THE HOME DEPOT, NOVA RECON, etc.
                r'^([A-Z][A-Za-z\s&.,]{3,50})\s*(?:Customer|Receipt|Invoice)',  # Vendor name before "Customer Receipt"
                r'^([A-Z][A-Za-z\s&.,]{3,50})\s*$',  # Standalone vendor name in first line
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
                            # Try to identify header row (look for common column names)
                            header_row_idx = 0
                            for idx, row in enumerate(table[:3]):  # Check first 3 rows
                                row_text = ' '.join([str(cell or '').lower() for cell in row if cell])
                                if any(keyword in row_text for keyword in ['qty', 'quantity', 'price', 'amount', 'description', 'item', 'unit']):
                                    header_row_idx = idx
                                    break
                            
                            headers = [str(cell or '').strip().lower() for cell in table[header_row_idx]]
                            
                            # Normalize header names
                            normalized_headers = {}
                            for i, h in enumerate(headers):
                                h_lower = h.lower()
                                if 'description' in h_lower or 'item' in h_lower or 'product' in h_lower or 'service' in h_lower:
                                    normalized_headers[i] = 'description'
                                elif 'qty' in h_lower or 'quantity' in h_lower:
                                    normalized_headers[i] = 'quantity'
                                elif 'unit' in h_lower and 'price' in h_lower:
                                    normalized_headers[i] = 'unit_price'
                                elif 'price' in h_lower and 'unit' not in h_lower:
                                    normalized_headers[i] = 'unit_price'
                                elif 'rate' in h_lower:  # For receipts like "Rate: $485.00"
                                    normalized_headers[i] = 'unit_price'
                                elif 'subtotal' in h_lower or ('total' in h_lower and 'amount' not in h_lower):
                                    normalized_headers[i] = 'subtotal'
                                elif 'amount' in h_lower and 'total' not in h_lower:
                                    normalized_headers[i] = 'subtotal'
                                elif 'sku' in h_lower or 'model' in h_lower:
                                    normalized_headers[i] = 'sku'
                                else:
                                    normalized_headers[i] = h
                            
                            # Extract data rows
                            for row in table[header_row_idx + 1:]:
                                if any(cell for cell in row):
                                    item = {}
                                    for i, cell in enumerate(row):
                                        if i < len(headers) and cell:
                                            header_name = normalized_headers.get(i, headers[i] if i < len(headers) else f'col_{i}')
                                            value = str(cell).strip()
                                            
                                            # Try to parse numeric values
                                            if header_name in ['quantity', 'unit_price', 'subtotal']:
                                                # Remove currency symbols and commas
                                                value_clean = re.sub(r'[^\d.]', '', value)
                                                try:
                                                    item[header_name] = float(value_clean)
                                                except:
                                                    item[header_name] = value
                                            else:
                                                item[header_name] = value
                                    
                                    # Only add if it has meaningful data (at least description, quantity, or amount)
                                    if item and (item.get('description') or item.get('quantity') or item.get('subtotal') or item.get('unit_price')):
                                        # If no quantity but has amount/rate, set quantity to 1
                                        if not item.get('quantity') and (item.get('subtotal') or item.get('unit_price')):
                                            item['quantity'] = 1
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
        """Extract a specific field using regex patterns.
        
        For total_amount, prioritizes "Order Total" over "Subtotal" or generic "Total".
        """
        patterns = self.patterns.get(field_name, [])
        context = context or {}
        
        # For total_amount, collect all matches and prioritize
        if field_name == 'total_amount':
            all_matches = []
            for pattern in patterns:
                matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
                for match in matches:
                    value = match.group(1) if match.groups() else match.group(0)
                    value_clean = value.replace(',', '')
                    try:
                        value_float = float(value_clean)
                        # Calculate priority: "Order Total" = 100, "Grand Total" = 90, "Amount Due" = 85, generic "Total" = 50
                        priority = 50  # Default
                        match_text = match.group(0).lower()
                        if 'order total' in match_text:
                            priority = 100  # Highest priority
                        elif 'grand total' in match_text:
                            priority = 90
                        elif 'amount due' in match_text or 'balance due' in match_text:
                            priority = 85
                        elif 'charged' in match_text:
                            priority = 80
                        elif 'subtotal' in match_text:
                            priority = 30  # Lower priority - this is NOT the order total
                        
                        all_matches.append({
                            'value': value_float,
                            'priority': priority,
                            'match': match,
                            'pattern': pattern
                        })
                    except:
                        continue
            
            # Return the match with highest priority
            if all_matches:
                best_match = max(all_matches, key=lambda x: x['priority'])
                match = best_match['match']
                start = max(0, match.start() - 50)
                end = min(len(text), match.end() + 50)
                snippet = text[start:end].strip()
                
                return {
                    "value": best_match['value'],
                    "confidence": 0.95 if best_match['priority'] >= 80 else 0.85,  # Higher confidence for specific totals
                    "provenance": {
                        "method": "regex",
                        "pattern": best_match['pattern'],
                        "snippet": snippet,
                        "priority": best_match['priority'],
                        **context
                    }
                }
            return None
        
        # For other fields, use first match
        for pattern in patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                value = match.group(1) if match.groups() else match.group(0)
                
                # Clean up value for numeric fields
                if field_name in ['subtotal', 'tax']:
                    value_clean = value.replace(',', '')
                    try:
                        value = float(value_clean)
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
        """Extract all invoice fields from text.
        
        Prioritizes PDF content over email body for vendor extraction.
        """
        extracted = {}
        
        # Split text by attachment markers to separate email body from PDF content
        parts = text.split('--- Attachment:')
        email_body = parts[0] if parts else text
        pdf_content = ' '.join(parts[1:]) if len(parts) > 1 else ""
        
        # Extract basic fields - prioritize PDF content for vendor_name
        for field_name in ['invoice_number', 'date', 'total_amount', 'currency', 'subtotal', 'tax']:
            # Try PDF content first, then email body
            search_text = pdf_content if pdf_content else text
            result = self.extract_field(field_name, search_text)
            if not result:
                result = self.extract_field(field_name, email_body)
            if result:
                extracted[field_name] = result
        
        # For total_amount, also check the END of the document (where summary sections usually are)
        # This helps capture "Order Total" which is often at the bottom
        if 'total_amount' not in extracted or extracted['total_amount'].get('confidence', 0) < 0.9:
            # Look at the last 2000 characters (summary section)
            summary_section = text[-2000:] if len(text) > 2000 else text
            summary_result = self.extract_field('total_amount', summary_section)
            if summary_result:
                # Use summary result if it has higher priority/confidence
                if 'total_amount' not in extracted or summary_result.get('confidence', 0) > extracted['total_amount'].get('confidence', 0):
                    extracted['total_amount'] = summary_result
        
        # For vendor_name, ONLY use PDF content, not email body
        # This prevents extracting email greetings like "Good afternoon" or "Hi Pradeep"
        if pdf_content:
            # Extract vendor from PDF content using the line-by-line method
            vendor_name = self._extract_vendor_from_text(pdf_content)
            if vendor_name:
                extracted['vendor_name'] = vendor_name
        else:
            # Fallback to email body only if no PDF content, but with very strict filtering
            vendor_name = self._extract_vendor_from_text(email_body)
            if vendor_name:
                vendor_value = vendor_name.get('value', '')
                # Double-check it's not a greeting
                if not any(greeting in vendor_value.lower() for greeting in ['good', 'hello', 'hi', 'dear', 'thank', 'afternoon', 'morning']):
                    extracted['vendor_name'] = vendor_name
        
        return extracted
    
    def _extract_vendor_from_text(self, text: str) -> Optional[Dict]:
        """Extract vendor name from text using line-by-line analysis.
        
        This method specifically looks for vendor names in PDF content,
        skipping email greetings and common phrases.
        """
        # Skip email greetings and common non-vendor patterns
        skip_patterns = [
            r'^(good|hello|hi|dear|greetings|thank you|thanks|please find|attached)',
            r'^(from|to|subject|date|sent|received)',
            r'^[a-z]',  # Skip lines starting with lowercase (likely email body)
            r'^(hi|hello)\s+[a-z]',  # "Hi Pradeep" type patterns
        ]
        
        # Also skip common email phrases
        skip_phrases = [
            'good afternoon', 'good morning', 'good evening',
            'thank you for', 'please find', 'attached is',
            'hi ', 'hello ', 'dear ', 'greetings'
        ]
        
        lines = text.split('\n')
        for line in lines[:30]:  # Check first 30 lines
            line_clean = line.strip()
            if not line_clean:
                continue
            
            # Skip if contains common email phrases
            line_lower = line_clean.lower()
            if any(phrase in line_lower for phrase in skip_phrases):
                continue
            
            # Skip email greetings and common email patterns
            should_skip = False
            for skip_pattern in skip_patterns:
                if re.match(skip_pattern, line_clean, re.IGNORECASE):
                    should_skip = True
                    break
            if should_skip:
                continue
            
            # Skip if line is too short or looks like email metadata
            if len(line_clean) < 3:
                continue
            
            # Skip if contains email-like patterns (colons early in line suggest metadata)
            if ':' in line_clean[:15] and not any(keyword in line_clean.lower() for keyword in ['customer', 'receipt', 'invoice', 'order']):
                continue
            
            # Try various vendor name patterns
            vendor_match = None
            
            # Pattern 1: All caps company names (THE HOME DEPOT, NOVA RECON, etc.) - prioritize this
            vendor_match = re.match(r'^([A-Z][A-Z\s&.,]{5,50}(?:DEPOT|RECON|CONSTRUCTION|RECYCLING|SUPPLIES|SERVICES|STORE|LLC|INC|CORP))', line_clean)
            
            # Pattern 2: Company with suffix (Pvt, Ltd, Inc, etc.)
            if not vendor_match:
                vendor_match = re.match(r'^([A-Z][A-Za-z\s&.,]{5,50}(?:Pvt|Ltd|Inc|LLC|Corp|Corporation|Company))', line_clean)
            
            # Pattern 3: Company name before "Customer Receipt" or "Invoice"
            if not vendor_match:
                vendor_match = re.match(r'^([A-Z][A-Za-z\s&.,]{5,50})\s*(?:Customer|Receipt|Invoice)', line_clean)
            
            # Pattern 4: Standalone company name (all caps, 2+ words, at least 5 chars)
            if not vendor_match:
                words = line_clean.split()
                if len(words) >= 2 and len(line_clean) >= 5:
                    # Check if most words are uppercase
                    upper_words = sum(1 for w in words[:3] if w.isupper() and len(w) > 1)
                    if upper_words >= 2:
                        vendor_match = re.match(r'^([A-Z][A-Z\s&.,]{5,50})', line_clean)
            
            if vendor_match:
                vendor_name = vendor_match.group(1).strip()
                # Clean up common prefixes
                vendor_name = re.sub(r'^(THE|A|AN)\s+', '', vendor_name, flags=re.IGNORECASE)
                # Only use if it's a reasonable length and doesn't look like a greeting
                if len(vendor_name) >= 3 and not re.match(r'^(good|hello|hi|dear)', vendor_name, re.IGNORECASE):
                    return {
                        "value": vendor_name,
                        "confidence": 0.90,
                        "provenance": {"method": "header_pattern", "snippet": line_clean}
                    }
        
        # No vendor found
        return None
    
    def process_email(self, email_data: Dict, attachments: List[Dict]) -> tuple:
        """Process email and attachments to extract invoice data."""
        # Extract email body text (Gmail format)
        email_body = ""
        if 'payload' in email_data:
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
        
        # ONLY process PDF/attachment content, ignore email body for extraction
        pdf_texts = []
        all_line_items = []
        
        # Process attachments (PDFs and images only)
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
                    pdf_texts.append(f"--- Attachment: {filename} ---\n{text}")
                    all_line_items.extend(line_items)
                elif filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff')):
                    text = self.extract_text_from_image(file_bytes)
                    pdf_texts.append(f"--- Attachment: {filename} ---\n{text}")
            except Exception as e:
                logger.error(f"Error processing attachment {filename}: {e}")
        
        # Combine ONLY PDF/attachment text (no email body) for extraction
        pdf_only_text = "\n".join(pdf_texts) if pdf_texts else ""
        
        # Extract fields - ONLY from PDF content, email body is completely ignored
        # Prioritize PDF content for date extraction (use date from attachment, not email)
        extracted = self.extract_all_fields(pdf_only_text, attachments)
        
        # If date was found in PDF, use it; otherwise try email body as last resort
        if not extracted.get('date') and email_body:
            date_from_email = self.extract_field('date', email_body)
            if date_from_email:
                extracted['date'] = date_from_email
        
        # For raw_text storage, we still include email body for reference, but extraction uses PDF only
        full_text_with_email = email_body + "\n" + pdf_only_text if pdf_only_text else email_body
        
        # Add line items if found
        if all_line_items:
            # Categorize items and assign BOM numbers
            from services.extractor.categorizer import categorize_items_with_ollama
            categorized_items = categorize_items_with_ollama(all_line_items)
            
            extracted['line_items'] = {
                "value": categorized_items,
                "confidence": 0.85,
                "provenance": {"method": "table_extraction_with_categorization"}
            }
        
        return full_text_with_email, extracted


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

