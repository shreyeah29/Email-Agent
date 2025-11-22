"""Streamlit Dashboard - View all invoices and query them."""
import streamlit as st
import json
import os
from datetime import datetime
from typing import Dict, Any, List
from uuid import UUID
import requests

from sqlalchemy.orm import Session
from sqlalchemy import func
from shared import SessionLocal, Invoice, Vendor, Project, s3_client, settings
import requests

# Page config
st.set_page_config(page_title="Invoice Dashboard", layout="wide", initial_sidebar_state="expanded")

# Authentication disabled - no password required
def check_password():
    """Password check disabled - always return True."""
    return True


def get_presigned_url(s3_path: str) -> str:
    """Generate presigned URL for S3 object, replacing internal hostname with localhost for browser access."""
    if not s3_path or not s3_path.startswith('s3://'):
        return ""
    try:
        bucket = s3_path.split('/')[2]
        key = '/'.join(s3_path.split('/')[3:])
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=3600
        )
        # Replace internal Docker hostname with localhost for browser access
        # This handles cases where S3_ENDPOINT_URL is set to http://minio:9000 (internal)
        # but we need http://localhost:9000 for browser access
        if url and 'minio:9000' in url:
            url = url.replace('minio:9000', 'localhost:9000')
        return url
    except Exception as e:
        return ""


def query_invoices_natural_language(query_text: str, db: Session) -> Dict:
    """Simple natural language query processing."""
    query_lower = query_text.lower()
    results = []
    
    # Get all invoices
    invoices = db.query(Invoice).order_by(Invoice.created_at.desc()).all()
    
    # Simple keyword matching
    if "total" in query_lower or "amount" in query_lower or "spend" in query_lower:
        total = 0.0
        count = 0
        for inv in invoices:
            extracted = inv.extracted or {}
            normalized = inv.normalized or {}
            amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value')
            if amount:
                try:
                    total += float(amount)
                    count += 1
                except:
                    pass
        return {
            "answer": f"Total amount across all invoices: ${total:,.2f} (from {count} invoices with amounts)",
            "invoices": []
        }
    
    elif "count" in query_lower or "how many" in query_lower:
        return {
            "answer": f"Total invoices processed: {len(invoices)}",
            "invoices": []
        }
    
    elif "needs review" in query_lower or "pending" in query_lower:
        pending = [inv for inv in invoices if inv.reconciliation_status == 'needs_review']
        return {
            "answer": f"Found {len(pending)} invoices that need review",
            "invoices": pending[:10]  # Limit to 10
        }
    
    elif "vendor" in query_lower:
        # Try to extract vendor name
        vendors = db.query(Vendor).all()
        vendor_names = [v.canonical_name.lower() for v in vendors]
        matched_vendors = [v for v in vendors if v.canonical_name.lower() in query_lower]
        
        if matched_vendors:
            vendor = matched_vendors[0]
            vendor_invoices = [inv for inv in invoices 
                             if inv.normalized and inv.normalized.get('vendor_id') == vendor.vendor_id]
            return {
                "answer": f"Found {len(vendor_invoices)} invoices for vendor '{vendor.canonical_name}'",
                "invoices": vendor_invoices[:10]
            }
    
    # Default: show recent invoices
    return {
        "answer": f"Showing recent invoices. Found {len(invoices)} total invoices.",
        "invoices": invoices[:10]
    }


def main():
    """Main dashboard app."""
    # Initialize session state early to avoid "SessionInfo before initialization" error
    if 'chat_history' not in st.session_state:
        st.session_state.chat_history = []
    if 'last_sync' not in st.session_state:
        st.session_state.last_sync = None
    if 'show_clear_confirm' not in st.session_state:
        st.session_state.show_clear_confirm = False
    if 'switch_tab' not in st.session_state:
        st.session_state.switch_tab = None
    
    if not check_password():
        return
    
    st.title("📊 Invoice Dashboard")
    st.markdown("View and query all processed invoices from your Gmail")
    
    # Info banner
    st.info("📧 **All invoices shown here were automatically processed from your Gmail inbox.** The ingestion service continuously monitors your email and processes new invoices every 60 seconds.")
    
    db = SessionLocal()
    try:
        # Get statistics
        total_invoices = db.query(Invoice).count()
        needs_review = db.query(Invoice).filter(Invoice.reconciliation_status == 'needs_review').count()
        auto_matched = db.query(Invoice).filter(Invoice.reconciliation_status == 'auto_matched').count()
        
        # Stats row
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Invoices", total_invoices)
        with col2:
            st.metric("Needs Review", needs_review)
        with col3:
            st.metric("Auto Matched", auto_matched)
        with col4:
            invoices_with_attachments = db.query(Invoice).filter(
                func.jsonb_array_length(Invoice.attachments) > 0
            ).count()
            st.metric("With Attachments", invoices_with_attachments)
        
        st.divider()
        
        # Tabs for different views
        tab1, tab2, tab3, tab4 = st.tabs(["📊 Summary", "💬 Query Agent", "📎 Attachments", "📧 Process Emails"])
        
        with tab1:
            st.header("📊 Data Summary & Management")
            
            # Overall statistics
            st.subheader("📈 Overall Statistics")
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("Total Invoices", total_invoices)
            with col2:
                st.metric("Needs Review", needs_review, delta=f"-{total_invoices - needs_review}" if total_invoices > 0 else None)
            with col3:
                st.metric("Auto Matched", auto_matched, delta=f"{auto_matched}" if auto_matched > 0 else None)
            with col4:
                invoices_with_attachments = db.query(Invoice).filter(
                    func.jsonb_array_length(Invoice.attachments) > 0
                ).count()
                st.metric("With Attachments", invoices_with_attachments)
            
            st.divider()
            
            # Financial summary
            st.subheader("💰 Financial Summary")
            all_invoices = db.query(Invoice).all()
            total_amount = 0.0
            currency_counts = {}
            vendor_totals = {}
            
            for inv in all_invoices:
                extracted = inv.extracted or {}
                normalized = inv.normalized or {}
                
                amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value')
                currency = normalized.get('currency') or extracted.get('currency', {}).get('value') or "Unknown"
                vendor_name = normalized.get('vendor_name') or extracted.get('vendor_name', {}).get('value') or "Unknown"
                
                if amount:
                    try:
                        total_amount += float(amount)
                        currency_counts[currency] = currency_counts.get(currency, 0) + float(amount)
                        vendor_totals[vendor_name] = vendor_totals.get(vendor_name, 0) + float(amount)
                    except:
                        pass
            
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Total Amount (All Currencies)", f"${total_amount:,.2f}")
                st.write("**By Currency:**")
                for curr, amt in currency_counts.items():
                    st.write(f"  • {curr}: ${amt:,.2f}")
            
            with col2:
                st.write("**Top Vendors by Amount:**")
                sorted_vendors = sorted(vendor_totals.items(), key=lambda x: x[1], reverse=True)[:5]
                for vendor, amt in sorted_vendors:
                    st.write(f"  • {vendor}: ${amt:,.2f}")
            
            st.divider()
            
            # Recent activity with attachments
            st.subheader("🕐 Recent Invoices & Attachments")
            recent_invoices = db.query(Invoice).order_by(Invoice.created_at.desc()).limit(10).all()
            if recent_invoices:
                for inv in recent_invoices:
                    extracted = inv.extracted or {}
                    normalized = inv.normalized or {}
                    vendor_name = normalized.get('vendor_name') or extracted.get('vendor_name', {}).get('value') or "Unknown"
                    amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value')
                    attachments = inv.attachments or []
                    
                    col1, col2, col3, col4 = st.columns([2, 2, 1, 2])
                    with col1:
                        st.write(f"📄 {inv.created_at.strftime('%Y-%m-%d %H:%M')}")
                        st.caption(vendor_name)
                    with col2:
                        if amount:
                            st.write(f"💰 ${amount}")
                        else:
                            st.write("💰 N/A")
                    with col3:
                        status_color = "🟢" if inv.reconciliation_status == "auto_matched" else "🟡" if inv.reconciliation_status == "needs_review" else "⚪"
                        st.write(f"{status_color} {inv.reconciliation_status or 'N/A'}")
                    with col4:
                        if attachments:
                            for att in attachments[:2]:  # Show first 2 attachments
                                if att.get('url', '').startswith('s3://'):
                                    url = get_presigned_url(att['url'])
                                    if url:
                                        st.markdown(f"[📎 {att.get('filename', 'attachment')[:30]}]({url})")
                        else:
                            st.caption("No attachments")
            else:
                st.info("No invoices processed yet.")
            
            st.divider()
            
            # Quick actions
            st.subheader("⚡ Quick Actions")
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                if st.button("🔄 Refresh Data", use_container_width=True):
                    st.rerun()
            with col2:
                if st.button("📎 View Attachments", use_container_width=True):
                    if 'switch_tab' in st.session_state:
                        st.session_state.switch_tab = "Attachments"
                    st.rerun()
            with col3:
                if st.button("💬 Ask Agent", use_container_width=True):
                    if 'switch_tab' in st.session_state:
                        st.session_state.switch_tab = "Query Agent"
                    st.rerun()
            with col4:
                if st.button("🗑️ Clear All Data", use_container_width=True, type="secondary"):
                    if 'show_clear_confirm' in st.session_state:
                        st.session_state.show_clear_confirm = True
                    st.rerun()
            
            # Clear data confirmation
            if st.session_state.get('show_clear_confirm', False):
                st.warning("⚠️ **Warning: This will delete ALL invoices and cannot be undone!**")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("✅ Yes, Delete Everything", type="primary", use_container_width=True):
                        API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8000")
                        API_KEY = os.getenv("API_KEY", "dev-api-key")
                        try:
                            response = requests.delete(
                                f"{API_BASE_URL}/invoices/all",
                                headers={"Authorization": f"Bearer {API_KEY}"},
                                timeout=30
                            )
                            if response.status_code == 200:
                                result = response.json()
                                st.success(f"✅ Cleared {result.get('deleted_invoices', 0)} invoices!")
                                st.session_state.show_clear_confirm = False
                                st.rerun()
                            else:
                                st.error(f"Error: {response.text}")
                        except Exception as e:
                            st.error(f"Error clearing data: {e}")
                with col_no:
                    if st.button("❌ Cancel", use_container_width=True):
                        st.session_state.show_clear_confirm = False
                        st.rerun()
        
        with tab2:
            st.header("💬 AI Document Assistant")
            st.markdown("Ask questions about any content in your PDFs. The AI analyzes all documents and provides clear answers.")
            
            # API configuration
            default_api_url = "http://api:8000" if os.getenv("DOCKER_ENV") else "http://localhost:8000"
            API_BASE_URL = os.getenv("API_BASE_URL", default_api_url)
            API_KEY = os.getenv("API_KEY", "dev-api-key")
            
            # Initialize chat history
            if 'chat_history' not in st.session_state:
                st.session_state.chat_history = []
            
            # Display chat history in a clean, modern way
            if st.session_state.chat_history:
                st.markdown("### 💬 Conversation")
                for idx, chat in enumerate(st.session_state.chat_history):
                    # User message
                    with st.chat_message("user"):
                        st.write(chat["query"])
                    
                    # Assistant response
                    with st.chat_message("assistant"):
                        # Clean answer (remove verbose prefixes)
                        answer = chat["answer"]
                        # Remove document tags from start if present
                        if answer.startswith("[Document:"):
                            parts = answer.split("]", 1)
                            if len(parts) > 1:
                                answer = parts[1].strip()
                        
                        # Display clean answer
                        st.markdown(answer)
                        
                        # Only show visualizations if answer doesn't already contain a clear answer
                        # (to avoid showing random numbers when we have a good answer)
                        if not any(word in answer.lower() for word in ['is', 'was', 'are', 'were', 'the', 'unit price', 'cost']):
                            import re
                            # Check if answer contains numbers/amounts
                            amounts = re.findall(r'[\$₹€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?', answer)
                            if amounts and len(amounts) > 1:
                                # Multiple amounts - show as a simple list
                                st.markdown("**Amounts found:**")
                                for amt in amounts[:5]:
                                    st.markdown(f"• {amt}")
                            
                            # Check for counts
                            count_match = re.search(r'(\d+)\s+(?:invoice|document|item|record)', answer, re.IGNORECASE)
                            if count_match:
                                count = int(count_match.group(1))
                                if count > 0:
                                    st.metric("Total Count", count)
                        
                        # Show sources in a clean way
                        if chat.get("sources"):
                            st.markdown("---")
                            st.markdown("**📎 Sources:**")
                            for i, source in enumerate(chat["sources"][:3], 1):
                                att_names = source.get('attachment_names', [])
                                att_name = att_names[0] if att_names else "Document"
                                
                                col1, col2 = st.columns([3, 1])
                                with col1:
                                    st.markdown(f"**{i}.** {att_name}")
                                with col2:
                                    if source.get('url'):
                                        st.markdown(f"[View PDF →]({source['url']})")
                            
                            if len(chat["sources"]) > 3:
                                st.caption(f"... and {len(chat['sources']) - 3} more document(s)")
                        
                        # Show caveats if any
                        if chat.get("caveats"):
                            for caveat in chat["caveats"]:
                                st.info(f"ℹ️ {caveat}")
            
            st.divider()
            
            # Query input - clean and simple
            query_text = st.text_input(
                "💬 Ask a question:",
                placeholder="e.g., 'What is the unit price of Steel City 4 in. Octagon Box?' or 'Summarize all documents'",
                key="agent_query_input",
                label_visibility="collapsed"
            )
            
            col1, col2 = st.columns([1, 4])
            with col1:
                ask_button = st.button("🔍 Ask", type="primary", use_container_width=True)
            
            # Process query
            if ask_button and query_text:
                with st.spinner("🤔 Analyzing documents..."):
                    try:
                        response = requests.post(
                            f"{API_BASE_URL}/agent",
                            json={"text": query_text},
                            headers={"Authorization": f"Bearer {API_KEY}"},
                            timeout=120  # Increased timeout for Ollama LLM processing
                        )
                        
                        if response.status_code == 200:
                            result = response.json()
                            
                            # Clean up the answer
                            answer = result["answer_text"]
                            if answer.startswith("[Document:"):
                                parts = answer.split("]", 1)
                                if len(parts) > 1:
                                    answer = parts[1].strip()
                            
                            # Add to chat history
                            st.session_state.chat_history.append({
                                "query": query_text,
                                "answer": answer,
                                "sources": result.get("sources", []),
                                "caveats": result.get("caveats", [])
                            })
                            
                            st.rerun()
                        else:
                            st.error(f"Error: {response.text}")
                    except Exception as e:
                        st.error(f"Error connecting to API: {e}")
            elif ask_button and not query_text:
                st.warning("Please enter a question first")
        
        with tab3:
            st.header("📎 All Attachments")
            
            # Get all invoices with attachments
            all_invoices = db.query(Invoice).all()
            invoices_with_attachments = []
            
            for invoice in all_invoices:
                attachments = invoice.attachments or []
                if attachments:
                    invoices_with_attachments.append((invoice, attachments))
            
            if not invoices_with_attachments:
                st.info("No attachments found in any invoices.")
            else:
                st.write(f"Found **{len(invoices_with_attachments)}** invoices with attachments")
                
                for invoice, attachments in invoices_with_attachments:
                    st.subheader(f"📧 Invoice from {invoice.created_at.strftime('%Y-%m-%d %H:%M')}")
                    st.write(f"**Invoice ID:** `{str(invoice.invoice_id)[:36]}`")
                    st.write(f"**Status:** {invoice.reconciliation_status or 'N/A'}")
                    
                    for att in attachments:
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            filename = att.get('filename', 'Unknown')
                            st.write(f"📎 **{filename}**")
                        with col2:
                            if att.get('url', '').startswith('s3://'):
                                url = get_presigned_url(att['url'])
                                if url:
                                    st.markdown(f"[⬇️ Download]({url})")
                    
                    st.divider()
        
        with tab4:
            st.header("📧 Process New Emails")
            
            # Sync Inbox section (for receipts-only account)
            st.subheader("🔄 Sync Inbox (Receipts-Only Account)")
            st.markdown("""
            **Sync Inbox Feature:**
            - Processes ALL emails with attachments from your receipts-only Gmail account
            - Automatically extracts invoice/receipt data
            - Marks processed emails with 'ProcessedByAgent' label
            - Safe and non-destructive (only adds labels, never deletes)
            """)
            
            col1, col2 = st.columns([2, 1])
            
            with col1:
                max_emails = st.number_input(
                    "Maximum emails to process per sync:",
                    min_value=1,
                    max_value=500,
                    value=100,
                    key="sync_max_emails"
                )
            
            with col2:
                # Option to include already processed emails
                include_processed = st.checkbox(
                    "Include already processed emails",
                    value=False,
                    help="If checked, will sync emails even if they have the ProcessedByAgent label (useful after clearing data)"
                )
                
                if st.button("🔄 Sync Inbox", type="primary", use_container_width=True):
                    API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8000")
                    API_KEY = os.getenv("API_KEY", "dev-api-key")
                    
                    with st.spinner("Syncing inbox... This may take a few minutes."):
                        try:
                            response = requests.post(
                                f"{API_BASE_URL}/sync_inbox",
                                json={"max": int(max_emails), "include_processed": include_processed},
                                headers={"Authorization": f"Bearer {API_KEY}"},
                                timeout=600  # 10 minute timeout for large syncs
                            )
                            
                            if response.status_code == 200:
                                result = response.json()
                                st.success("✅ Sync completed!")
                                
                                # Store result in session state
                                st.session_state.last_sync = {
                                    "timestamp": datetime.now().isoformat(),
                                    "total_found": result.get("total_found", 0),
                                    "processed": result.get("processed", 0),
                                    "skipped": result.get("skipped", 0),
                                    "errors": result.get("errors", 0),
                                    "new_invoices": result.get("new_invoices", 0)
                                }
                                
                                # Display results
                                st.markdown("### 📊 Sync Results")
                                col1, col2, col3, col4 = st.columns(4)
                                with col1:
                                    st.metric("Total Found", result.get("total_found", 0))
                                with col2:
                                    st.metric("Processed", result.get("processed", 0))
                                with col3:
                                    st.metric("New Invoices", result.get("new_invoices", 0))
                                with col4:
                                    st.metric("Errors", result.get("errors", 0))
                                
                                if result.get("skipped", 0) > 0:
                                    st.info(f"ℹ️ {result.get('skipped')} emails were skipped (already processed)")
                                
                                # Show message IDs if available
                                if result.get("message_ids"):
                                    with st.expander("📧 Processed Email IDs"):
                                        for msg_id in result.get("message_ids", [])[:10]:
                                            st.code(msg_id, language=None)
                                
                                # Show warning if found but not processed
                                if result.get("total_found", 0) > 0 and result.get("processed", 0) == 0:
                                    st.warning("⚠️ **Emails were found but none were processed.** Check the API logs for errors.")
                                
                                st.rerun()
                            else:
                                st.error(f"Sync failed: {response.text}")
                        except Exception as e:
                            st.error(f"Error syncing inbox: {e}")
                            st.info("Make sure the API is running and Gmail credentials are configured.")
            
            # Show last sync status
            if 'last_sync' in st.session_state and st.session_state.last_sync is not None:
                sync_info = st.session_state.last_sync
                st.divider()
                st.markdown(f"**Last Sync:** {sync_info.get('timestamp', 'Unknown')}")
                st.write(f"Found: {sync_info.get('total_found', 0)} | "
                        f"Processed: {sync_info.get('processed', 0)} | "
                        f"New Invoices: {sync_info.get('new_invoices', 0)}")
            
            st.divider()
            
            # Original email processing section
            st.subheader("📧 Manual Email Processing")
            st.markdown("Manually trigger processing of invoice-related emails from Gmail")
            
            col1, col2 = st.columns([2, 1])
            
            with col1:
                st.write("""
                **How it works:**
                1. Click "Scan Gmail" to fetch unread emails
                2. The system will automatically filter for invoice-related emails
                3. Only emails with keywords (invoice, receipt, bill) or PDF/Excel attachments will be shown
                4. Click "Process Selected" to process them
                """)
            
            with col2:
                if st.button("🔍 Scan Gmail for Invoices", type="secondary", use_container_width=True):
                    st.info("""
                    **To process emails:**
                    
                    Option 1: Use the command line:
                    ```bash
                    python services/ingestion/main.py
                    ```
                    (This will process all invoice-related unread emails)
                    
                    Option 2: Use the Email Selector UI:
                    Run: `streamlit run services/ui/email_selector.py`
                    """)
            
            st.divider()
            st.subheader("📊 Processing Status")
            
            # Show recent processing activity
            recent_invoices = db.query(Invoice).order_by(Invoice.created_at.desc()).limit(5).all()
            if recent_invoices:
                st.write("**Recently Processed Invoices:**")
                for inv in recent_invoices:
                    st.write(f"• {inv.created_at.strftime('%Y-%m-%d %H:%M')} - Invoice {str(inv.invoice_id)[:8]}...")
            else:
                st.info("No invoices processed yet.")
    
    finally:
        db.close()


if __name__ == "__main__":
    main()

