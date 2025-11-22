"""Gmail helper functions for candidate message fetching and processing.

Additive — does not modify existing behavior.
This module provides helper functions for Gmail API operations.
"""
import os
import json
import logging
import base64
from typing import List, Dict, Any, Optional
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared import settings

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.modify']


def get_gmail_service():
    """Get authenticated Gmail service, using receipts-only account credentials.
    
    Updated to use new receipts-only account credentials.
    """
    creds = None
    # Use receipts-only account credentials - same as gmail_sync
    default_token = os.getenv('GMAIL_TOKEN_PATH', 'token_receiptagent.pickle')
    # If default token is a directory, use alternative filename (same logic as gmail_sync)
    if os.path.exists(default_token) and os.path.isdir(default_token):
        token_file = "token_receiptagent_new.pickle"
        logger.warning(f"{default_token} is a directory, using {token_file} instead")
    else:
        token_file = default_token
    client_secrets_path = os.getenv('GMAIL_CLIENT_SECRETS_PATH') or settings.gmail_client_secrets_path or "/app/client_secret_729224522226-scuue33esjetj7b3qpkmjuekpmj036e5.apps.googleusercontent.com.json"
    
    # Try to load existing token (supports both pickle and JSON formats)
    if os.path.exists(token_file) and os.path.isfile(token_file):
        try:
            # Try pickle format first (for receipts account)
            if token_file.endswith('.pickle'):
                import pickle
                with open(token_file, 'rb') as f:
                    loaded_obj = pickle.load(f)
                    # Check if it's a Credentials object, not OAuth2Token
                    if isinstance(loaded_obj, Credentials):
                        creds = loaded_obj
                        logger.info(f"Loaded existing Gmail token from {token_file} (pickle format)")
                    else:
                        logger.warning(f"Token file contains {type(loaded_obj).__name__} instead of Credentials. Please regenerate token.")
                        creds = None
            else:
                # Try JSON format (json already imported at top)
                with open(token_file, 'r') as f:
                    token_data = json.load(f)
                token_scopes = token_data.get('scopes', SCOPES)
                creds = Credentials.from_authorized_user_file(token_file, token_scopes)
                logger.info(f"Loaded existing Gmail token from {token_file} with scopes: {token_scopes}")
        except Exception as e:
            logger.warning(f"Could not load existing token from {token_file}: {e}")
            creds = None
    
    # Refresh or get new token
    if not creds or (creds.expired if hasattr(creds, 'expired') else True):
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired Gmail token...")
                creds.refresh(Request())
                # Save refreshed token
                if token_file.endswith('.pickle'):
                    import pickle
                    with open(token_file, 'wb') as token:
                        pickle.dump(creds, token)
                else:
                    with open(token_file, 'w') as token:
                        token.write(creds.to_json())
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                if not creds.refresh_token:
                    creds = None
        
        if not creds:
            # Use receipts-only account credentials from client secrets file
            if client_secrets_path and os.path.exists(client_secrets_path):
                try:
                    with open(client_secrets_path, 'r') as f:
                        client_config = json.load(f)
                    
                    # Handle both 'installed' and 'web' client configs
                    if 'installed' in client_config:
                        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                    elif 'web' in client_config:
                        flow = InstalledAppFlow.from_client_config(
                            {"installed": client_config['web']}, SCOPES
                        )
                    else:
                        raise ValueError("Invalid client secrets format")
                    
                    logger.info(f"Starting OAuth flow with client secrets from {client_secrets_path}")
                    try:
                        creds = flow.run_local_server(port=0)
                    except Exception as e:
                        logger.error(f"OAuth flow failed: {e}")
                        # Provide manual URL if browser not available
                        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                        auth_url, _ = flow.authorization_url(prompt='consent')
                        logger.info(f"Manual OAuth URL: {auth_url}")
                        raise
                    
                    # Save token
                    if token_file.endswith('.pickle'):
                        import pickle
                        with open(token_file, 'wb') as token:
                            pickle.dump(creds, token)
                    else:
                        with open(token_file, 'w') as token:
                            token.write(creds.to_json())
                    logger.info(f"Saved new token to {token_file}")
                except Exception as e:
                    logger.error(f"Error loading client secrets from {client_secrets_path}: {e}")
                    raise ValueError(f"Gmail credentials error: {e}")
            else:
                raise ValueError("Gmail credentials not found. Set GMAIL_CLIENT_SECRETS_PATH")
    
    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Error building Gmail service: {e}")
        return None


def get_candidate_messages(query: str = "has:attachment subject:(invoice OR receipt OR bill)", max_results: int = 50) -> List[Dict[str, Any]]:
    """Fetch candidate messages from Gmail with metadata only (no body/attachments downloaded).
    
    Additive — does not modify existing behavior.
    
    Args:
        query: Gmail search query (default: "has:attachment subject:(invoice OR receipt OR bill)")
        max_results: Maximum number of messages to return
        
    Returns:
        List of message previews with metadata
    """
    service = get_gmail_service()
    if not service:
        return []
    
    try:
        # List messages
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=max_results
        ).execute()
        
        messages = results.get('messages', [])
        if not messages:
            return []
        
        # Get metadata for each message
        previews = []
        for msg in messages:
            try:
                # First, get basic metadata
                message = service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='metadata',
                    metadataHeaders=['From', 'Subject', 'Date']
                ).execute()
                
                # Extract headers
                headers = message.get('payload', {}).get('headers', [])
                subject = ""
                from_addr = ""
                date = ""
                
                for header in headers:
                    name = header.get('name', '').lower()
                    if name == 'subject':
                        subject = header.get('value', '')
                    elif name == 'from':
                        from_addr = header.get('value', '')
                    elif name == 'date':
                        date = header.get('value', '')
                
                # Check for attachments - use hasAttachment field if available
                has_attachment = message.get('payload', {}).get('hasAttachment', False)
                attachment_filenames = []
                
                # If hasAttachment is True or we need filenames, get full message parts
                if has_attachment or True:  # Always check for filenames
                    # Get full message to extract attachment filenames
                    full_message = service.users().messages().get(
                        userId='me',
                        id=msg['id'],
                        format='full'
                    ).execute()
                    
                    parts = full_message.get('payload', {}).get('parts', [])
                    if not parts:
                        parts = [full_message.get('payload', {})]
                    
                    def extract_attachments(part_list):
                        """Recursively extract attachment filenames."""
                        filenames = []
                        for part in part_list:
                            filename = part.get('filename', '')
                            if filename:
                                filenames.append(filename)
                            nested = part.get('parts', [])
                            if nested:
                                filenames.extend(extract_attachments(nested))
                        return filenames
                    
                    attachment_filenames = extract_attachments(parts)
                    has_attachment = len(attachment_filenames) > 0 or has_attachment
                
                previews.append({
                    "message_id": msg['id'],
                    "subject": subject,
                    "from": from_addr,
                    "date": date,
                    "snippet": message.get('snippet', ''),
                    "has_attachment": has_attachment,
                    "attachment_filenames": attachment_filenames
                })
            except HttpError as e:
                logger.error(f"Error fetching message {msg['id']}: {e}")
                continue
        
        return previews
    
    except HttpError as e:
        logger.error(f"Error listing messages: {e}")
        return []


def fetch_message_body_and_attachments(message_id: str, staging_dir: Optional[str] = None, service: Any = None) -> Dict[str, Any]:
    """Download message body and attachments to staging directory.
    
    Additive — does not modify existing behavior.
    
    Args:
        message_id: Gmail message ID
        staging_dir: Directory to save attachments (default: temp directory)
        service: Optional Gmail service object (if provided, will use this instead of creating new one)
        
    Returns:
        Dict with 'email_json', 'email_data', 'attachments' (list of file paths), 'raw_text', 'staging_dir'
    """
    if service is None:
        service = get_gmail_service()
    if not service:
        raise ValueError("Gmail service not available")
    
    import tempfile
    if staging_dir is None:
        staging_dir = tempfile.mkdtemp(prefix='gmail_attachments_')
    else:
        os.makedirs(staging_dir, exist_ok=True)
    
    try:
        # Get full message
        message = service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        # Save email JSON
        email_json_path = os.path.join(staging_dir, f"{message_id}.json")
        with open(email_json_path, 'w') as f:
            json.dump(message, f, default=str)
        
        # Extract body text
        raw_text = ""
        parts = message.get('payload', {}).get('parts', [])
        if not parts:
            parts = [message.get('payload', {})]
        
        def extract_text(part):
            text = ""
            mime_type = part.get('mimeType', '').lower()
            body = part.get('body', {})
            data = body.get('data', '')
            
            if data and mime_type in ('text/plain', 'text/html'):
                try:
                    text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.warning(f"Error decoding text: {e}")
            
            # Recursively check nested parts
            nested = part.get('parts', [])
            for nested_part in nested:
                text += "\n" + extract_text(nested_part)
            
            return text
        
        raw_text = extract_text(message.get('payload', {}))
        
        # Download attachments
        attachment_paths = []
        for part in parts:
            filename = part.get('filename', '')
            if filename:
                att_id = part.get('body', {}).get('attachmentId')
                if att_id:
                    try:
                        att = service.users().messages().attachments().get(
                            userId='me',
                            messageId=message_id,
                            id=att_id
                        ).execute()
                        data = base64.urlsafe_b64decode(att['data'])
                        
                        file_path = os.path.join(staging_dir, filename)
                        with open(file_path, 'wb') as f:
                            f.write(data)
                        attachment_paths.append(file_path)
                    except Exception as e:
                        logger.error(f"Error downloading attachment {filename}: {e}")
            
            # Check nested parts
            nested = part.get('parts', [])
            for nested_part in nested:
                nested_filename = nested_part.get('filename', '')
                if nested_filename:
                    nested_att_id = nested_part.get('body', {}).get('attachmentId')
                    if nested_att_id:
                        try:
                            att = service.users().messages().attachments().get(
                                userId='me',
                                messageId=message_id,
                                id=nested_att_id
                            ).execute()
                            data = base64.urlsafe_b64decode(att['data'])
                            
                            file_path = os.path.join(staging_dir, nested_filename)
                            with open(file_path, 'wb') as f:
                                f.write(data)
                            attachment_paths.append(file_path)
                        except Exception as e:
                            logger.error(f"Error downloading nested attachment {nested_filename}: {e}")
        
        return {
            "email_json": email_json_path,
            "email_data": message,
            "attachments": attachment_paths,
            "raw_text": raw_text,
            "staging_dir": staging_dir
        }
    
    except HttpError as e:
        logger.error(f"Error fetching message {message_id}: {e}")
        raise


def apply_label(message_id: str, label_name: str = "ProcessedByAgent") -> bool:
    """Create label if missing and apply it to message (non-destructive).
    
    Additive — does not modify existing behavior.
    Non-destructive: only adds labels, never deletes messages.
    
    Args:
        message_id: Gmail message ID
        label_name: Label name to apply
        
    Returns:
        True if successful, False otherwise
    """
    service = get_gmail_service()
    if not service:
        return False
    
    try:
        # Check if label exists, create if not
        labels = service.users().labels().list(userId='me').execute().get('labels', [])
        label_id = None
        
        for label in labels:
            if label['name'] == label_name:
                label_id = label['id']
                break
        
        if not label_id:
            # Create label
            new_label = service.users().labels().create(
                userId='me',
                body={'name': label_name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
            ).execute()
            label_id = new_label['id']
        
        # Apply label to message
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]}
        ).execute()
        
        logger.info(f"Applied label '{label_name}' to message {message_id}")
        return True
    
    except HttpError as e:
        logger.error(f"Error applying label to message {message_id}: {e}")
        return False

