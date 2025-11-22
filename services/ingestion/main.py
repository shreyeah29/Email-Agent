"""Email ingestion service - polls Gmail and enqueues extraction jobs."""
import os
import json
import logging
import time
from datetime import datetime
from typing import List, Dict, Any
import base64

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared import settings, s3_client, redis_client, ensure_s3_bucket

logging.basicConfig(level=getattr(logging, settings.log_level))
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
EXTRACTION_QUEUE = 'extraction_queue'


class GmailIngester:
    """Gmail email ingester."""
    
    def __init__(self):
        self.service = None
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with Gmail API."""
        creds = None
        token_file = 'token.json'
        
        if os.path.exists(token_file) and os.path.isfile(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not settings.gmail_client_id or not settings.gmail_client_secret:
                    logger.warning("Gmail credentials not configured. Skipping Gmail ingestion.")
                    return
                
                flow = InstalledAppFlow.from_client_config(
                    {
                        "installed": {
                            "client_id": settings.gmail_client_id,
                            "client_secret": settings.gmail_client_secret,
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "redirect_uris": ["http://localhost"]
                        }
                    },
                    SCOPES
                )
                try:
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    # If browser can't be opened (e.g., in Docker), print auth URL
                    logger.warning(f"Could not open browser: {e}")
                    auth_url, _ = flow.authorization_url(prompt='consent')
                    logger.info(f"Please visit this URL to authorize the application:")
                    logger.info(f"{auth_url}")
                    logger.info("After authorization, you will be redirected. Copy the 'code' parameter from the URL.")
                    logger.info("Then run: python -c \"from services.ingestion.main import *; flow.fetch_token(code='YOUR_CODE')\"")
                    return
            
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        
        try:
            self.service = build('gmail', 'v1', credentials=creds)
            logger.info("Gmail authentication successful")
        except Exception as e:
            logger.error(f"Gmail authentication failed: {e}")
    
    def fetch_messages(self, query: str = "is:unread", max_results: int = 50) -> List[Dict]:
        """Fetch messages from Gmail."""
        if not self.service:
            return []
        
        try:
            results = self.service.users().messages().list(
                userId='me', q=query, maxResults=max_results
            ).execute()
            messages = results.get('messages', [])
            return messages
        except HttpError as e:
            logger.error(f"Error fetching Gmail messages: {e}")
            return []
    
    def is_invoice_related(self, message: Dict) -> bool:
        """Check if message is likely an invoice/receipt/bill."""
        # Get message details
        full_message = self.get_message(message.get('id', ''))
        if not full_message:
            return False
        
        # Check subject and snippet for keywords
        subject = ""
        snippet = full_message.get('snippet', '').lower()
        
        headers = full_message.get('payload', {}).get('headers', [])
        for header in headers:
            if header.get('name', '').lower() == 'subject':
                subject = header.get('value', '').lower()
                break
        
        # Keywords that indicate invoices/receipts
        invoice_keywords = [
            'invoice', 'receipt', 'bill', 'payment', 'statement',
            'purchase order', 'po', 'quotation', 'quote', 'estimate',
            'expense', 'voucher', 'tax', 'invoice number', 'inv-',
            'bill to', 'amount due', 'total', 'subtotal'
        ]
        
        # Check subject and snippet
        text_to_check = f"{subject} {snippet}"
        has_keyword = any(keyword in text_to_check for keyword in invoice_keywords)
        
        # Check for attachments (PDF, Excel, etc.)
        has_attachments = False
        parts = full_message.get('payload', {}).get('parts', [])
        if not parts:
            parts = [full_message.get('payload', {})]
        
        for part in parts:
            filename = part.get('filename', '').lower()
            mime_type = part.get('mimeType', '').lower()
            
            # Check for PDF, Excel, or image attachments
            if filename.endswith(('.pdf', '.xlsx', '.xls', '.csv')) or \
               mime_type in ('application/pdf', 'application/vnd.ms-excel', 
                            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
                has_attachments = True
                break
            
            # Check nested parts
            nested_parts = part.get('parts', [])
            for nested in nested_parts:
                nested_filename = nested.get('filename', '').lower()
                nested_mime = nested.get('mimeType', '').lower()
                if nested_filename.endswith(('.pdf', '.xlsx', '.xls', '.csv')) or \
                   nested_mime in ('application/pdf', 'application/vnd.ms-excel',
                                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
                    has_attachments = True
                    break
        
        # Return True if has keywords OR has relevant attachments
        return has_keyword or has_attachments
    
    def get_message(self, msg_id: str) -> Dict[str, Any]:
        """Get full message details."""
        if not self.service:
            return {}
        
        try:
            message = self.service.users().messages().get(
                userId='me', id=msg_id, format='full'
            ).execute()
            return message
        except HttpError as e:
            logger.error(f"Error fetching message {msg_id}: {e}")
            return {}
    
    def download_attachments(self, message: Dict, email_id: str) -> List[Dict]:
        """Download attachments from Gmail message."""
        attachments_info = []
        
        if not self.service:
            return attachments_info
        
        parts = message.get('payload', {}).get('parts', [])
        if not parts:
            # Single part message
            parts = [message.get('payload', {})]
        
        for part in parts:
            if part.get('filename'):
                att_id = part.get('body', {}).get('attachmentId')
                if att_id:
                    try:
                        att = self.service.users().messages().attachments().get(
                            userId='me', messageId=email_id, id=att_id
                        ).execute()
                        data = base64.urlsafe_b64decode(att['data'])
                        
                        filename = part['filename']
                        s3_key = f"inbox/attachments/{email_id}/{filename}"
                        s3_client.put_object(
                            Bucket=settings.s3_bucket,
                            Key=s3_key,
                            Body=data,
                            ContentType=part.get('mimeType', 'application/octet-stream')
                        )
                        
                        attachments_info.append({
                            "filename": filename,
                            "url": f"s3://{settings.s3_bucket}/{s3_key}",
                            "type": part.get('mimeType', 'application/octet-stream')
                        })
                        logger.info(f"Downloaded attachment: {filename}")
                    except Exception as e:
                        logger.error(f"Error downloading attachment: {e}")
        
        return attachments_info


def extract_email_body(message: Dict) -> str:
    """Extract email body text from Gmail message."""
    payload = message.get('payload', {})
    body = ""
    
    def extract_from_part(part):
        text = ""
        if part.get('mimeType') == 'text/plain':
            data = part.get('body', {}).get('data', '')
            if data:
                text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        elif part.get('mimeType') == 'text/html':
            data = part.get('body', {}).get('data', '')
            if data:
                html = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')
                text = soup.get_text()
        
        parts = part.get('parts', [])
        for p in parts:
            text += "\n" + extract_from_part(p)
        return text
    
    body = extract_from_part(payload)
    return body


def process_message(message: Dict, ingester) -> bool:
    """Process a single email message: save to S3 and enqueue extraction job."""
    try:
        email_id = message.get('id')
        if not email_id:
            return False
        
        # Get full message
        full_message = ingester.get_message(email_id)
        if not full_message:
            return False
        
        # Save raw email JSON to S3
        s3_key = f"inbox/raw/{email_id}.json"
        s3_client.put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=json.dumps(full_message, default=str).encode('utf-8'),
            ContentType='application/json'
        )
        
        # Download attachments
        attachments = ingester.download_attachments(full_message, email_id)
        
        # Extract received date
        received_at = datetime.fromtimestamp(int(full_message.get('internalDate', 0)) / 1000).isoformat()
        
        # Enqueue extraction job
        job_payload = {
            "email_id": email_id,
            "source": "gmail",
            "s3_raw": f"s3://{settings.s3_bucket}/{s3_key}",
            "attachments": [att["url"] for att in attachments],
            "received_at": received_at
        }
        
        redis_client.lpush(EXTRACTION_QUEUE, json.dumps(job_payload))
        logger.info(f"Enqueued extraction job for email {email_id}")
        
        return True
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        return False


def run_ingestion(auto_mode: bool = False):
    """Main ingestion loop - processes emails.
    
    Args:
        auto_mode: If True, continuously polls Gmail. If False, runs once and exits.
    """
    ensure_s3_bucket()
    
    gmail_ingester = GmailIngester()
    processed_ids = set()
    
    if auto_mode:
        logger.info("Starting email ingestion service (AUTO MODE - continuous polling)...")
    else:
        logger.info("Starting email ingestion service (MANUAL MODE - single run)...")
    
    while True:
        try:
            # Process Gmail
            if gmail_ingester.service:
                # Fetch unread messages
                gmail_messages = gmail_ingester.fetch_messages(query="is:unread", max_results=50)
                
                # Filter for invoice-related emails only
                invoice_messages = []
                for msg in gmail_messages:
                    # Get full message to check if it's invoice-related
                    full_msg = gmail_ingester.get_message(msg['id'])
                    if full_msg and gmail_ingester.is_invoice_related(full_msg):
                        invoice_messages.append(msg)
                
                logger.info(f"Found {len(gmail_messages)} unread emails, {len(invoice_messages)} are invoice-related")
                
                # Process only invoice-related emails
                for msg in invoice_messages:
                    msg_id = msg['id']
                    if msg_id not in processed_ids:
                        if process_message(msg, gmail_ingester):
                            processed_ids.add(msg_id)
                            logger.info(f"✅ Processed invoice email: {msg_id}")
            
            if not auto_mode:
                logger.info(f"Manual run complete. Processed {len(processed_ids)} invoice emails.")
                break
            
            logger.info(f"Processed {len(processed_ids)} messages. Sleeping for 60 seconds...")
            time.sleep(60)  # Poll every minute
            
        except KeyboardInterrupt:
            logger.info("Ingestion service stopped")
            break
        except Exception as e:
            logger.error(f"Error in ingestion loop: {e}")
            if not auto_mode:
                break
            time.sleep(60)


if __name__ == "__main__":
    # Run in manual mode (processes once and exits)
    # To run in auto mode, change to: run_ingestion(auto_mode=True)
    run_ingestion(auto_mode=False)

