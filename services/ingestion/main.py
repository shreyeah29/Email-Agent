"""Email ingestion service - polls Gmail/Outlook and enqueues extraction jobs."""
import os
import json
import logging
import time
from datetime import datetime
from typing import List, Dict, Any
from email.utils import parsedate_to_datetime
import base64

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import requests
from msal import ConfidentialClientApplication

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
        
        if os.path.exists(token_file):
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
                creds = flow.run_local_server(port=0)
            
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        
        try:
            self.service = build('gmail', 'v1', credentials=creds)
            logger.info("Gmail authentication successful")
        except Exception as e:
            logger.error(f"Gmail authentication failed: {e}")
    
    def fetch_messages(self, query: str = "is:unread", max_results: int = 10) -> List[Dict]:
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


class OutlookIngester:
    """Microsoft Outlook email ingester."""
    
    def __init__(self):
        self.app = None
        self.token = None
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with Microsoft Graph API."""
        if not all([settings.microsoft_client_id, settings.microsoft_client_secret, settings.microsoft_tenant_id]):
            logger.warning("Microsoft credentials not configured. Skipping Outlook ingestion.")
            return
        
        try:
            self.app = ConfidentialClientApplication(
                settings.microsoft_client_id,
                authority=f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}",
                client_credential=settings.microsoft_client_secret
            )
            
            result = self.app.acquire_token_for_client(
                scopes=["https://graph.microsoft.com/.default"]
            )
            
            if "access_token" in result:
                self.token = result["access_token"]
                logger.info("Microsoft Graph authentication successful")
            else:
                logger.error(f"Microsoft authentication failed: {result.get('error_description')}")
        except Exception as e:
            logger.error(f"Microsoft authentication error: {e}")
    
    def fetch_messages(self, max_results: int = 10) -> List[Dict]:
        """Fetch unread messages from Outlook."""
        if not self.token:
            return []
        
        try:
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            }
            url = f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
            params = {
                '$filter': 'isRead eq false',
                '$top': max_results,
                '$select': 'id,subject,receivedDateTime,hasAttachments'
            }
            
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get('value', [])
        except Exception as e:
            logger.error(f"Error fetching Outlook messages: {e}")
            return []
    
    def get_message(self, msg_id: str) -> Dict[str, Any]:
        """Get full message details."""
        if not self.token:
            return {}
        
        try:
            headers = {'Authorization': f'Bearer {self.token}'}
            url = f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}"
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching Outlook message {msg_id}: {e}")
            return {}
    
    def download_attachments(self, message: Dict, email_id: str) -> List[Dict]:
        """Download attachments from Outlook message."""
        attachments_info = []
        
        if not self.token or not message.get('hasAttachments'):
            return attachments_info
        
        try:
            headers = {'Authorization': f'Bearer {self.token}'}
            url = f"https://graph.microsoft.com/v1.0/me/messages/{email_id}/attachments"
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            attachments = response.json().get('value', [])
            
            for att in attachments:
                filename = att.get('name', 'attachment')
                content_bytes = base64.b64decode(att.get('contentBytes', ''))
                
                s3_key = f"inbox/attachments/{email_id}/{filename}"
                s3_client.put_object(
                    Bucket=settings.s3_bucket,
                    Key=s3_key,
                    Body=content_bytes,
                    ContentType=att.get('contentType', 'application/octet-stream')
                )
                
                attachments_info.append({
                    "filename": filename,
                    "url": f"s3://{settings.s3_bucket}/{s3_key}",
                    "type": att.get('contentType', 'application/octet-stream')
                })
                logger.info(f"Downloaded attachment: {filename}")
        except Exception as e:
            logger.error(f"Error downloading Outlook attachments: {e}")
        
        return attachments_info


def extract_email_body(message: Dict, source: str) -> str:
    """Extract email body text from Gmail or Outlook message."""
    if source == 'gmail':
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
    
    elif source == 'outlook':
        return message.get('body', {}).get('content', '')
    
    return ""


def process_message(message: Dict, source: str, ingester) -> bool:
    """Process a single email message: save to S3 and enqueue extraction job."""
    try:
        email_id = message.get('id') if source == 'gmail' else message.get('id')
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
        if source == 'gmail':
            received_at = datetime.fromtimestamp(int(full_message.get('internalDate', 0)) / 1000).isoformat()
        else:
            received_at = full_message.get('receivedDateTime', datetime.now().isoformat())
        
        # Enqueue extraction job
        job_payload = {
            "email_id": email_id,
            "source": source,
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


def run_ingestion():
    """Main ingestion loop - polls email accounts and processes new messages."""
    ensure_s3_bucket()
    
    gmail_ingester = GmailIngester()
    outlook_ingester = OutlookIngester()
    
    processed_ids = set()
    
    logger.info("Starting email ingestion service...")
    
    while True:
        try:
            # Process Gmail
            if gmail_ingester.service:
                gmail_messages = gmail_ingester.fetch_messages(query="is:unread", max_results=10)
                for msg in gmail_messages:
                    msg_id = msg['id']
                    if msg_id not in processed_ids:
                        if process_message(msg, 'gmail', gmail_ingester):
                            processed_ids.add(msg_id)
            
            # Process Outlook
            if outlook_ingester.token:
                outlook_messages = outlook_ingester.fetch_messages(max_results=10)
                for msg in outlook_messages:
                    msg_id = msg['id']
                    if msg_id not in processed_ids:
                        if process_message(msg, 'outlook', outlook_ingester):
                            processed_ids.add(msg_id)
            
            logger.info(f"Processed {len(processed_ids)} messages. Sleeping for 60 seconds...")
            time.sleep(60)  # Poll every minute
            
        except KeyboardInterrupt:
            logger.info("Ingestion service stopped")
            break
        except Exception as e:
            logger.error(f"Error in ingestion loop: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_ingestion()

