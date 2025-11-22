"""Streamlit review UI for invoice corrections."""
import streamlit as st
import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
from uuid import UUID
import requests

from sqlalchemy.orm import Session
from shared import SessionLocal, Invoice, Vendor, Project, InvoiceAudit, TrainingExample, s3_client, settings

# Page config
st.set_page_config(page_title="Invoice Review", layout="wide")

# Simple authentication (MVP)
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
        if url and 'minio:9000' in url:
            url = url.replace('minio:9000', 'localhost:9000')
        return url
    except Exception as e:
        st.error(f"Error generating URL: {e}")
        return ""


def save_training_example(invoice_id: UUID, original_extracted: Dict, corrected_extracted: Dict, 
                         corrected_normalized: Dict, user_name: str, db: Session):
    """Save training example."""
    example = TrainingExample(
        invoice_id=invoice_id,
        original_extracted=original_extracted,
        corrected_extracted=corrected_extracted,
        corrected_normalized=corrected_normalized,
        user_name=user_name
    )
    db.add(example)
    db.commit()
    
    # Also save to file
    os.makedirs("training_examples", exist_ok=True)
    filename = f"training_examples/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{invoice_id}.json"
    with open(filename, 'w') as f:
        json.dump({
            "invoice_id": str(invoice_id),
            "original_extracted": original_extracted,
            "corrected_extracted": corrected_extracted,
            "corrected_normalized": corrected_normalized,
            "user_name": user_name,
            "created_at": datetime.now().isoformat()
        }, f, indent=2)
    
    return filename


def create_audit_record(invoice_id: UUID, field_name: str, old_value: Any, new_value: Any, 
                       user_name: str, db: Session):
    """Create audit record for field change."""
    audit = InvoiceAudit(
        invoice_id=invoice_id,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        user_name=user_name
    )
    db.add(audit)
    db.commit()


def main():
    """Main Streamlit app."""
    if not check_password():
        return
    
    st.title("📧 Invoice Review Interface")
    
    db = SessionLocal()
    try:
        # Sidebar filters
        st.sidebar.header("Filters")
        status_filter = st.sidebar.selectbox(
            "Reconciliation Status",
            ["needs_review", "auto_matched", "manual", "All"],
            index=0
        )
        
        # Get invoices
        query = db.query(Invoice)
        if status_filter != "All":
            query = query.filter(Invoice.reconciliation_status == status_filter)
        else:
            query = query.filter(Invoice.reconciliation_status != None)
        
        invoices = query.order_by(Invoice.created_at.desc()).limit(50).all()
        
        if not invoices:
            st.info("No invoices found matching the criteria.")
            return
        
        # Invoice selector
        invoice_options = {
            f"{inv.invoice_id} - {inv.created_at.strftime('%Y-%m-%d')}": inv.invoice_id
            for inv in invoices
        }
        
        selected_invoice_id = st.selectbox(
            "Select Invoice",
            options=list(invoice_options.keys()),
            index=0
        )
        
        invoice_id = invoice_options[selected_invoice_id]
        invoice = db.query(Invoice).filter(Invoice.invoice_id == invoice_id).first()
        
        if not invoice:
            st.error("Invoice not found")
            return
        
        # Main content area
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.header("Invoice Details")
            
            # Show attachments
            if invoice.attachments:
                st.subheader("Attachments")
                for att in invoice.attachments:
                    if att.get('url', '').startswith('s3://'):
                        url = get_presigned_url(att['url'])
                        if url:
                            st.markdown(f"[📎 {att.get('filename', 'attachment')}]({url})")
            
            # Show raw text preview
            if invoice.raw_text:
                with st.expander("Raw Text Preview"):
                    st.text(invoice.raw_text[:2000])
            
            # Extracted fields editor
            st.subheader("Extracted Fields")
            extracted = invoice.extracted or {}
            corrected_extracted = json.loads(json.dumps(extracted))  # Deep copy
            
            for field_name, field_data in extracted.items():
                if isinstance(field_data, dict):
                    value = field_data.get('value', '')
                    confidence = field_data.get('confidence', 0.0)
                    provenance = field_data.get('provenance', {})
                    
                    col_a, col_b = st.columns([3, 1])
                    with col_a:
                        new_value = st.text_input(
                            f"{field_name}",
                            value=str(value),
                            key=f"extracted_{field_name}_{invoice_id}"
                        )
                        corrected_extracted[field_name]['value'] = new_value
                    
                    with col_b:
                        st.metric("Confidence", f"{confidence:.2f}")
                    
                    if provenance:
                        with st.expander(f"Provenance for {field_name}"):
                            st.json(provenance)
                else:
                    st.text_input(f"{field_name}", value=str(field_data), key=f"extracted_{field_name}_{invoice_id}")
        
        with col2:
            st.header("Normalized Fields")
            normalized = invoice.normalized or {}
            corrected_normalized = json.loads(json.dumps(normalized))
            
            # Vendor selection
            vendors = db.query(Vendor).all()
            vendor_options = {None: "None"}
            vendor_options.update({v.vendor_id: v.canonical_name for v in vendors})
            
            current_vendor_id = normalized.get('vendor_id')
            selected_vendor = st.selectbox(
                "Vendor",
                options=list(vendor_options.keys()),
                format_func=lambda x: vendor_options.get(x, "None"),
                index=list(vendor_options.keys()).index(current_vendor_id) if current_vendor_id in vendor_options else 0
            )
            if selected_vendor:
                corrected_normalized['vendor_id'] = selected_vendor
                corrected_normalized['vendor_name'] = vendor_options[selected_vendor]
            
            # Project selection
            projects = db.query(Project).all()
            project_options = {None: "None"}
            project_options.update({p.project_id: p.name for p in projects})
            
            current_project_id = normalized.get('project_id')
            selected_project = st.selectbox(
                "Project",
                options=list(project_options.keys()),
                format_func=lambda x: project_options.get(x, "None"),
                index=list(project_options.keys()).index(current_project_id) if current_project_id in project_options else 0
            )
            if selected_project:
                corrected_normalized['project_id'] = selected_project
                corrected_normalized['project_name'] = project_options[selected_project]
            
            # Show suggestions if available
            if invoice.extra and invoice.extra.get('suggestions'):
                st.subheader("Suggestions")
                suggestions = invoice.extra['suggestions']
                
                if 'vendors' in suggestions:
                    st.write("**Vendor Suggestions:**")
                    for sug in suggestions['vendors']:
                        if st.button(f"Accept: {sug['name']} (score: {sug['score']:.0f})", 
                                   key=f"vendor_sug_{sug['vendor_id']}"):
                            corrected_normalized['vendor_id'] = sug['vendor_id']
                            corrected_normalized['vendor_name'] = sug['name']
                            st.rerun()
                
                if 'projects' in suggestions:
                    st.write("**Project Suggestions:**")
                    for sug in suggestions['projects']:
                        if st.button(f"Accept: {sug['name']} (score: {sug['score']:.0f})",
                                   key=f"project_sug_{sug['project_id']}"):
                            corrected_normalized['project_id'] = sug['project_id']
                            corrected_normalized['project_name'] = sug['name']
                            st.rerun()
            
            # Show current normalized values
            st.subheader("Current Normalized Values")
            st.json(normalized)
        
        # Action buttons
        st.divider()
        col_save, col_ignore, col_audit = st.columns(3)
        
        user_name = st.session_state.get('user_name', 'admin')
        
        with col_save:
            if st.button("💾 Save Corrections", type="primary"):
                # Create audit records for changes
                for field_name in corrected_extracted:
                    old_val = extracted.get(field_name, {}).get('value') if isinstance(extracted.get(field_name), dict) else extracted.get(field_name)
                    new_val = corrected_extracted.get(field_name, {}).get('value') if isinstance(corrected_extracted.get(field_name), dict) else corrected_extracted.get(field_name)
                    
                    if old_val != new_val:
                        create_audit_record(invoice_id, field_name, old_val, new_val, user_name, db)
                
                # Update invoice
                invoice.extracted = corrected_extracted
                invoice.normalized = corrected_normalized
                invoice.reconciliation_status = 'manual'
                db.commit()
                
                # Save training example
                save_training_example(invoice_id, extracted, corrected_extracted, 
                                    corrected_normalized, user_name, db)
                
                st.success("✅ Corrections saved!")
                st.rerun()
        
        with col_ignore:
            if st.button("🚫 Mark as Ignored"):
                invoice.reconciliation_status = 'ignored'
                db.commit()
                st.success("Invoice marked as ignored")
                st.rerun()
        
        with col_audit:
            if st.button("📋 View Audit Trail"):
                audit_records = db.query(InvoiceAudit).filter(
                    InvoiceAudit.invoice_id == invoice_id
                ).order_by(InvoiceAudit.changed_at.desc()).all()
                
                if audit_records:
                    st.subheader("Audit Trail")
                    for audit in audit_records:
                        st.write(f"**{audit.field_name}** changed by {audit.user_name} at {audit.changed_at}")
                        st.write(f"  Old: {audit.old_value}")
                        st.write(f"  New: {audit.new_value}")
                else:
                    st.info("No audit records found")
    
    finally:
        db.close()


if __name__ == "__main__":
    main()

