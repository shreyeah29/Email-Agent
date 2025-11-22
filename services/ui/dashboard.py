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
        tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Summary", "📋 All Invoices", "💬 Query Agent", "📎 Attachments", "📧 Process Emails"])
        
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
            
            # Recent activity
            st.subheader("🕐 Recent Activity")
            recent_invoices = db.query(Invoice).order_by(Invoice.created_at.desc()).limit(10).all()
            if recent_invoices:
                for inv in recent_invoices:
                    extracted = inv.extracted or {}
                    normalized = inv.normalized or {}
                    vendor_name = normalized.get('vendor_name') or extracted.get('vendor_name', {}).get('value') or "Unknown"
                    amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value')
                    
                    col1, col2, col3 = st.columns([3, 2, 1])
                    with col1:
                        st.write(f"📄 {inv.created_at.strftime('%Y-%m-%d %H:%M')} - {vendor_name}")
                    with col2:
                        if amount:
                            st.write(f"💰 ${amount}")
                        else:
                            st.write("💰 N/A")
                    with col3:
                        status_color = "🟢" if inv.reconciliation_status == "auto_matched" else "🟡" if inv.reconciliation_status == "needs_review" else "⚪"
                        st.write(f"{status_color} {inv.reconciliation_status or 'N/A'}")
            else:
                st.info("No invoices processed yet.")
            
            st.divider()
            
            # Quick actions
            st.subheader("⚡ Quick Actions")
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("🔄 Refresh Data", use_container_width=True):
                    st.rerun()
            with col2:
                if st.button("📊 View All Invoices", use_container_width=True):
                    st.session_state.switch_tab = "All Invoices"
                    st.rerun()
            with col3:
                if st.button("💬 Ask Agent", use_container_width=True):
                    st.session_state.switch_tab = "Query Agent"
                    st.rerun()
        
        with tab2:
            st.header("All Processed Invoices")
            
            # Filters
            col_filter1, col_filter2 = st.columns(2)
            with col_filter1:
                status_filter = st.selectbox(
                    "Filter by Status",
                    ["All", "needs_review", "auto_matched", "manual"],
                    index=0
                )
            with col_filter2:
                sort_by = st.selectbox(
                    "Sort by",
                    ["Newest First", "Oldest First", "By Amount"],
                    index=0
                )
            
            # Get invoices
            query = db.query(Invoice)
            if status_filter != "All":
                query = query.filter(Invoice.reconciliation_status == status_filter)
            
            if sort_by == "Newest First":
                query = query.order_by(Invoice.created_at.desc())
            elif sort_by == "Oldest First":
                query = query.order_by(Invoice.created_at.asc())
            
            invoices = query.limit(100).all()
            
            if not invoices:
                st.info("No invoices found.")
            else:
                st.success(f"✅ Found {len(invoices)} invoice(s)")
                
                # Display invoices in cards
                for idx, invoice in enumerate(invoices):
                    # Create a card-like container
                    with st.container():
                        col_a, col_b, col_c, col_d = st.columns([2, 2, 2, 1])
                        
                        with col_a:
                            st.markdown(f"### 📄 Invoice #{idx+1}")
                            st.write(f"**Date:** {invoice.created_at.strftime('%Y-%m-%d %H:%M')}")
                            st.write(f"**Status:** `{invoice.reconciliation_status or 'N/A'}`")
                            st.write(f"**Source:** 📧 Gmail Email")
                            if invoice.source_email_id:
                                st.caption(f"Email ID: {invoice.source_email_id[:20]}...")
                        
                        with col_b:
                            # Extract key fields
                            extracted = invoice.extracted or {}
                            normalized = invoice.normalized or {}
                            
                            vendor_name = normalized.get('vendor_name') or extracted.get('vendor_name', {}).get('value') or "Not extracted"
                            total_amount = normalized.get('total_amount') or extracted.get('total_amount', {}).get('value')
                            currency = normalized.get('currency') or extracted.get('currency', {}).get('value') or ""
                            
                            st.write("**Vendor:**", vendor_name)
                            if total_amount:
                                st.write("**Amount:**", f"{currency} {total_amount}" if currency else str(total_amount))
                            else:
                                st.write("**Amount:** N/A")
                            
                            # Calculate confidence
                            if extracted:
                                confidences = [v.get('confidence', 0) for v in extracted.values() if isinstance(v, dict) and 'confidence' in v]
                                avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
                                st.write("**Confidence:**", f"{avg_confidence:.2f}")
                        
                        with col_c:
                            st.write("**Invoice ID:**")
                            st.code(str(invoice.invoice_id)[:36], language=None)
                            st.write("**Email ID:**")
                            st.text(invoice.source_email_id or "N/A")
                        
                        with col_d:
                            # Attachments
                            attachments = invoice.attachments or []
                            if attachments:
                                st.write("**Attachments:**")
                                for att in attachments:
                                    if att.get('url', '').startswith('s3://'):
                                        url = get_presigned_url(att['url'])
                                        if url:
                                            st.markdown(f"[📎 {att.get('filename', 'attachment')}]({url})")
                            else:
                                st.write("**Attachments:** None")
                        
                        # Expandable section for details
                        with st.expander(f"🔍 View Details for Invoice #{idx+1}", expanded=False):
                            # Show extracted fields
                            if extracted:
                                st.subheader("Extracted Fields")
                                st.json(extracted)
                            
                            # Show normalized fields
                            if normalized:
                                st.subheader("Normalized Fields")
                                st.json(normalized)
                            
                            # Show raw text preview
                            if invoice.raw_text:
                                st.subheader("Raw Text Preview")
                                st.text_area("Text", invoice.raw_text[:1000], height=200, disabled=True, key=f"raw_text_{invoice.invoice_id}")
                        
                        st.divider()
        
        with tab3:
            st.header("💬 Conversational Agent")
            st.markdown("Ask questions about your invoices using natural language. The AI agent will analyze your data and provide answers with sources.")
            
            # API configuration
            # Use 'api' service name when running in Docker, 'localhost' when running locally
            default_api_url = "http://api:8000" if os.getenv("DOCKER_ENV") else "http://localhost:8000"
            API_BASE_URL = os.getenv("API_BASE_URL", default_api_url)
            API_KEY = os.getenv("API_KEY", "dev-api-key")
            
            # Query input with chat-like interface
            if 'chat_history' not in st.session_state:
                st.session_state.chat_history = []
            
            # Display chat history
            if st.session_state.chat_history:
                st.markdown("### 💬 Conversation History")
                for chat in st.session_state.chat_history:
                    with st.chat_message("user"):
                        st.write(chat["query"])
                    with st.chat_message("assistant"):
                        st.write(chat["answer"])
                        if chat.get("sources"):
                            with st.expander(f"📎 Sources ({len(chat['sources'])} documents found)"):
                                for i, source in enumerate(chat["sources"], 1):
                                    st.markdown(f"**Document {i}:**")
                                    st.write(f"• ID: `{source.get('invoice_id', 'N/A')[:8]}...`")
                                    st.write(f"• Confidence: {source.get('confidence', 0):.2%}")
                                    
                                    # Show attachment names if available
                                    if source.get('attachment_names'):
                                        att_names = source.get('attachment_names', [])
                                        st.write(f"• Attachments: {', '.join(att_names[:3])}")
                                        if len(att_names) > 3:
                                            st.write(f"  ... and {len(att_names) - 3} more")
                                    
                                    # Show snippet if available
                                    if source.get('snippet'):
                                        with st.expander("📄 Preview snippet"):
                                            st.text(source['snippet'][:500])
                                    
                                    if source.get('url'):
                                        st.markdown(f"  [🔗 View Full Document]({source['url']})")
                                    st.divider()
                        if chat.get("caveats"):
                            st.warning("⚠️ " + " | ".join(chat["caveats"]))
                st.divider()
            
            # Example queries as buttons
            st.markdown("### 💡 Try These Queries:")
            col1, col2 = st.columns(2)
            example_queries = [
                "How many invoices are there?",
                "What's the total amount across all invoices?",
                "Search for devops related documents",
                "Summarize all attachments",
                "What documents are about construction?",
                "Find documents with PDF attachments"
            ]
            
            # Track which example was clicked
            selected_example = None
            for i, example in enumerate(example_queries):
                col = col1 if i % 2 == 0 else col2
                with col:
                    if st.button(f"💬 {example}", key=f"example_{i}", use_container_width=True):
                        selected_example = example
            
            st.divider()
            
            # Query input - use text_input instead of chat_input (chat_input can't be in tabs)
            query_text = st.text_input(
                "💬 Ask a question about your invoices:",
                value=selected_example if selected_example else "",
                placeholder="e.g., 'How much did Company A spend in October 2025?'",
                key="agent_query_input"
            )
            
            # Process query button
            if st.button("🔍 Ask Agent", type="primary", use_container_width=True):
                query_to_process = selected_example if selected_example else query_text
                if query_to_process:
                    with st.spinner("🤔 Thinking..."):
                        try:
                            # Call API agent endpoint
                            response = requests.post(
                                f"{API_BASE_URL}/agent",
                                json={"text": query_to_process},
                                headers={"Authorization": f"Bearer {API_KEY}"},
                                timeout=30
                            )
                            
                            if response.status_code == 200:
                                result = response.json()
                                
                                # Add to chat history
                                st.session_state.chat_history.append({
                                    "query": result["query"],
                                    "answer": result["answer_text"],
                                    "sources": result.get("sources", []),
                                    "caveats": result.get("caveats", [])
                                })
                                
                                st.rerun()
                            else:
                                st.error(f"Error: {response.text}")
                        except Exception as e:
                            st.error(f"Error connecting to API: {e}")
                            st.info("Make sure the API is running on http://localhost:8000")
                else:
                    st.warning("Please enter a question first")
            
            # Auto-process if example was selected
            if selected_example:
                with st.spinner("🤔 Thinking..."):
                    try:
                        response = requests.post(
                            f"{API_BASE_URL}/agent",
                            json={"text": selected_example},
                            headers={"Authorization": f"Bearer {API_KEY}"},
                            timeout=30
                        )
                        if response.status_code == 200:
                            result = response.json()
                            st.session_state.chat_history.append({
                                "query": result["query"],
                                "answer": result["answer_text"],
                                "sources": result.get("sources", []),
                                "caveats": result.get("caveats", [])
                            })
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
        
        with tab4:
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
        
        with tab5:
            st.header("📧 Process New Emails")
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
                if st.button("🔍 Scan Gmail for Invoices", type="primary"):
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

