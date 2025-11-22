"""Email Selector UI - Choose which emails to process."""
import streamlit as st
import json
import os
from datetime import datetime
from typing import List, Dict
import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import base64

from shared import settings

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Page config
st.set_page_config(page_title="Email Selector", layout="wide")

def get_gmail_service():
    """Get authenticated Gmail service."""
    creds = None
    token_file = 'token.json'
    
    if os.path.exists(token_file) and os.path.isfile(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            st.error("Gmail authentication required. Please run get_gmail_token.py first.")
            return None
    
    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        st.error(f"Error building Gmail service: {e}")
        return None


def fetch_emails(service, query: str = "is:unread", max_results: int = 50) -> List[Dict]:
    """Fetch emails from Gmail."""
    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=max_results
        ).execute()
        messages = results.get('messages', [])
        return messages
    except HttpError as e:
        st.error(f"Error fetching emails: {e}")
        return []


def get_email_details(service, msg_id: str) -> Dict:
    """Get full email details."""
    try:
        message = service.users().messages().get(
            userId='me', id=msg_id, format='full'
        ).execute()
        return message
    except HttpError as e:
        st.error(f"Error fetching message {msg_id}: {e}")
        return {}


def is_invoice_related(message: Dict) -> bool:
    """Check if email is likely an invoice/receipt."""
    # Get subject
    subject = ""
    snippet = message.get('snippet', '').lower()
    
    headers = message.get('payload', {}).get('headers', [])
    for header in headers:
        if header.get('name', '').lower() == 'subject':
            subject = header.get('value', '').lower()
            break
    
    # Keywords
    invoice_keywords = [
        'invoice', 'receipt', 'bill', 'payment', 'statement',
        'purchase order', 'po', 'quotation', 'quote', 'estimate',
        'expense', 'voucher', 'tax', 'invoice number', 'inv-',
        'bill to', 'amount due', 'total', 'subtotal'
    ]
    
    text_to_check = f"{subject} {snippet}"
    has_keyword = any(keyword in text_to_check for keyword in invoice_keywords)
    
    # Check attachments
    has_attachments = False
    parts = message.get('payload', {}).get('parts', [])
    if not parts:
        parts = [message.get('payload', {})]
    
    for part in parts:
        filename = part.get('filename', '').lower()
        mime_type = part.get('mimeType', '').lower()
        
        if filename.endswith(('.pdf', '.xlsx', '.xls', '.csv')) or \
           mime_type in ('application/pdf', 'application/vnd.ms-excel',
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
            has_attachments = True
            break
        
        nested_parts = part.get('parts', [])
        for nested in nested_parts:
            nested_filename = nested.get('filename', '').lower()
            nested_mime = nested.get('mimeType', '').lower()
            if nested_filename.endswith(('.pdf', '.xlsx', '.xls', '.csv')) or \
               nested_mime in ('application/pdf', 'application/vnd.ms-excel',
                              'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
                has_attachments = True
                break
    
    return has_keyword or has_attachments


def trigger_processing(email_ids: List[str]):
    """Trigger processing of selected emails via API."""
    try:
        # Call ingestion API endpoint (if we add one) or directly process
        # For now, we'll use a simple approach
        api_url = "http://api:8000/process-emails"  # We'll need to add this endpoint
        response = requests.post(api_url, json={"email_ids": email_ids})
        return response.status_code == 200
    except Exception as e:
        st.error(f"Error triggering processing: {e}")
        return False


def main():
    """Main email selector app."""
    st.title("📧 Select Emails to Process")
    st.markdown("Choose which emails contain invoices/receipts to process")
    
    # Get Gmail service
    service = get_gmail_service()
    if not service:
        return
    
    # Sidebar controls
    st.sidebar.header("Filters")
    query_type = st.sidebar.selectbox(
        "Email Type",
        ["Unread", "All", "Custom Query"],
        index=0
    )
    
    custom_query = ""
    if query_type == "Custom Query":
        custom_query = st.sidebar.text_input("Gmail Query", placeholder="e.g., from:vendor@example.com")
    
    # Build query
    if query_type == "Unread":
        query = "is:unread"
    elif query_type == "All":
        query = ""
    else:
        query = custom_query
    
    # Fetch emails
    if st.button("🔍 Fetch Emails", type="primary"):
        with st.spinner("Fetching emails..."):
            messages = fetch_emails(service, query=query, max_results=50)
            
            if not messages:
                st.info("No emails found.")
                return
            
            # Get full details and filter
            invoice_emails = []
            other_emails = []
            
            for msg in messages:
                full_msg = get_email_details(service, msg['id'])
                if is_invoice_related(full_msg):
                    invoice_emails.append(full_msg)
                else:
                    other_emails.append(full_msg)
            
            st.session_state.invoice_emails = invoice_emails
            st.session_state.other_emails = other_emails
    
    # Display invoice-related emails
    if 'invoice_emails' in st.session_state and st.session_state.invoice_emails:
        st.header(f"📄 Invoice-Related Emails ({len(st.session_state.invoice_emails)})")
        st.info("These emails appear to contain invoices, receipts, or bills. Select which ones to process.")
        
        selected_emails = []
        
        for idx, email in enumerate(st.session_state.invoice_emails):
            # Get subject
            subject = "No Subject"
            headers = email.get('payload', {}).get('headers', [])
            for header in headers:
                if header.get('name', '').lower() == 'subject':
                    subject = header.get('value', '')
                    break
            
            # Get date
            date = ""
            for header in headers:
                if header.get('name', '').lower() == 'date':
                    date = header.get('value', '')
                    break
            
            # Check for attachments
            has_pdf = False
            attachments = []
            parts = email.get('payload', {}).get('parts', [])
            if not parts:
                parts = [email.get('payload', {})]
            
            for part in parts:
                filename = part.get('filename', '')
                if filename:
                    attachments.append(filename)
                    if filename.lower().endswith(('.pdf', '.xlsx', '.xls')):
                        has_pdf = True
            
            # Display email card
            col1, col2, col3 = st.columns([1, 4, 1])
            with col1:
                checkbox = st.checkbox("Process", key=f"email_{email['id']}")
                if checkbox:
                    selected_emails.append(email['id'])
            
            with col2:
                st.write(f"**{subject}**")
                st.caption(f"Date: {date}")
                st.caption(f"Email ID: {email['id']}")
                if attachments:
                    st.caption(f"Attachments: {', '.join(attachments)}")
                else:
                    st.caption("No attachments")
            
            with col3:
                if has_pdf:
                    st.badge("📎 PDF/Excel", type="success")
            
            st.divider()
        
        # Process selected emails
        if selected_emails:
            st.header("🚀 Process Selected Emails")
            st.write(f"You've selected {len(selected_emails)} email(s) to process.")
            
            if st.button("✅ Process Selected Emails", type="primary"):
                with st.spinner("Processing emails..."):
                    # Here we would trigger the processing
                    # For now, we'll show a message
                    st.success(f"Processing {len(selected_emails)} emails...")
                    st.info("Note: You'll need to run the ingestion service with these email IDs, or we can add an API endpoint for this.")
        
        # Show other emails (collapsed)
        if st.session_state.other_emails:
            with st.expander(f"📬 Other Emails ({len(st.session_state.other_emails)}) - Not invoice-related"):
                for email in st.session_state.other_emails[:10]:  # Show first 10
                    headers = email.get('payload', {}).get('headers', [])
                    subject = "No Subject"
                    for header in headers:
                        if header.get('name', '').lower() == 'subject':
                            subject = header.get('value', '')
                            break
                    st.write(f"• {subject}")


if __name__ == "__main__":
    main()

