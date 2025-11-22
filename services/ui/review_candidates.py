"""Streamlit UI for reviewing and processing candidate emails."""
import streamlit as st
import requests
import time
from typing import List, Dict, Any
import os

# Page config
st.set_page_config(page_title="Review Candidates", layout="wide", initial_sidebar_state="expanded")

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "dev-api-key")


def check_password():
    """Password check disabled - always return True."""
    return True


def fetch_candidates(query: str, max_results: int = 50) -> List[Dict]:
    """Fetch candidate messages from API."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/candidates/messages",
            params={"q": query, "max": max_results},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        st.error(f"Error fetching candidates: {e}")
        return []


def process_messages(message_ids: List[str], label_after: bool = False) -> Dict:
    """Process selected messages via API."""
    try:
        response = requests.post(
            f"{API_BASE_URL}/candidates/process",
            json={"message_ids": message_ids, "label_after": label_after},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=60
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        st.error(f"Error processing messages: {e}")
        return {}


def get_job_status(job_id: str) -> Dict:
    """Get job status from API."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/candidates/process_status",
            params={"job_id": job_id},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


def main():
    """Main review candidates app."""
    if not check_password():
        return
    
    st.title("📧 Review & Process Approved Emails")
    st.markdown("Select specific emails to process through the invoice extraction pipeline")
    
    # Sidebar
    st.sidebar.header("Search Options")
    
    query_preset = st.sidebar.selectbox(
        "Quick Search",
        [
            "has:attachment subject:(invoice OR receipt OR bill)",
            "has:attachment",
            "subject:(invoice OR receipt)",
            "label:ToProcessByAgent",
            "Custom"
        ],
        index=0
    )
    
    if query_preset == "Custom":
        custom_query = st.sidebar.text_input("Custom Gmail Query", placeholder="e.g., from:vendor@example.com")
        query = custom_query if custom_query else "has:attachment"
    else:
        query = query_preset
    
    max_results = st.sidebar.slider("Max Results", 10, 100, 50)
    
    # Main content
    if st.button("🔍 Find Candidates", type="primary"):
        with st.spinner("Fetching candidate messages from Gmail..."):
            candidates = fetch_candidates(query, max_results)
            st.session_state.candidates = candidates
            st.session_state.selected_ids = []
    
    # Display candidates
    if 'candidates' in st.session_state and st.session_state.candidates:
        candidates = st.session_state.candidates
        st.success(f"Found {len(candidates)} candidate message(s)")
        
        # Initialize selected_ids if not exists
        if 'selected_ids' not in st.session_state:
            st.session_state.selected_ids = []
        
        # Selection controls
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            if st.button("✅ Select All"):
                st.session_state.selected_ids = [c['message_id'] for c in candidates]
                st.rerun()
        with col2:
            if st.button("❌ Deselect All"):
                st.session_state.selected_ids = []
                st.rerun()
        with col3:
            if st.session_state.selected_ids:
                st.info(f"📧 {len(st.session_state.selected_ids)} message(s) selected")
        
        # Candidate table
        st.subheader("Candidate Messages")
        
        # Track checkbox changes
        for idx, candidate in enumerate(candidates):
            col_check, col_info, col_att = st.columns([1, 4, 2])
            
            with col_check:
                msg_id = candidate['message_id']
                checkbox_key = f"select_{msg_id}"
                
                # Get current selection state
                is_selected = msg_id in st.session_state.selected_ids
                
                # Create checkbox
                checked = st.checkbox("", value=is_selected, key=checkbox_key)
                
                # Update selection based on checkbox state
                if checked and msg_id not in st.session_state.selected_ids:
                    st.session_state.selected_ids.append(msg_id)
                    st.rerun()
                elif not checked and msg_id in st.session_state.selected_ids:
                    st.session_state.selected_ids.remove(msg_id)
                    st.rerun()
            
            with col_info:
                st.write(f"**{candidate.get('subject', 'No Subject')}**")
                st.caption(f"From: {candidate.get('from', 'Unknown')} | Date: {candidate.get('date', 'Unknown')}")
                
                # Initialize preview state if not exists
                preview_key = f"preview_{candidate['message_id']}"
                if preview_key not in st.session_state:
                    st.session_state[preview_key] = False
                
                if candidate.get('snippet'):
                    snippet = candidate['snippet']
                    if len(snippet) > 200:
                        st.caption(snippet[:200] + "...")
                    else:
                        st.caption(snippet)
                
                # Preview button - use on_click to avoid state modification issues
                if st.button("👁️ Preview", key=f"btn_preview_{candidate['message_id']}"):
                    st.session_state[preview_key] = not st.session_state[preview_key]
                    st.rerun()
                
                # Preview modal
                if st.session_state.get(preview_key, False):
                    with st.expander("📄 Full Preview", expanded=True):
                        st.write("**Subject:**", candidate.get('subject', 'No Subject'))
                        st.write("**From:**", candidate.get('from', 'Unknown'))
                        st.write("**Date:**", candidate.get('date', 'Unknown'))
                        st.write("**Snippet:**")
                        st.text(candidate.get('snippet', 'No snippet available'))
                        if candidate.get('attachment_filenames'):
                            st.write("**Attachments:**")
                            for att in candidate['attachment_filenames']:
                                st.write(f"  • {att}")
                        if st.button("Close", key=f"close_preview_{candidate['message_id']}"):
                            st.session_state[preview_key] = False
                            st.rerun()
            
            with col_att:
                if candidate.get('has_attachment'):
                    st.markdown("📎 **Attachments**")
                    if candidate.get('attachment_filenames'):
                        st.caption(", ".join(candidate['attachment_filenames'][:3]))
                        if len(candidate['attachment_filenames']) > 3:
                            st.caption(f"+ {len(candidate['attachment_filenames']) - 3} more")
                else:
                    st.caption("No attachments")
            
            st.divider()
        
        # Process selected section - make it more visible
        if st.session_state.selected_ids:
            st.markdown("---")
            st.markdown("### 🚀 Process Selected Messages")
            st.markdown(f"**{len(st.session_state.selected_ids)} message(s) ready to process**")
            
            col_opt, col_btn = st.columns([2, 1])
            with col_opt:
                label_after = st.checkbox("Apply 'ProcessedByAgent' label after processing", value=False)
            
            with col_btn:
                process_clicked = st.button("🚀 Process Selected Messages", type="primary", use_container_width=True)
            
            # Process messages when button is clicked
            if process_clicked:
                with st.spinner("Processing messages..."):
                    result = process_messages(st.session_state.selected_ids, label_after)
                
                if result and 'jobs' in result:
                    st.success(f"✅ Queued {result['queued_count']} job(s) for processing")
                    
                    # Store job IDs in session state for progress tracking
                    job_ids = [job['job_id'] for job in result['jobs']]
                    st.session_state.processing_jobs = job_ids
                    st.session_state.processing_started = True
                    st.rerun()
        
        # Show progress and results if processing has started
        if st.session_state.get('processing_jobs') and st.session_state.get('processing_started'):
            st.markdown("---")
            st.subheader("📊 Processing Status")
            
            job_ids = st.session_state.processing_jobs
            progress_placeholder = st.empty()
            status_placeholder = st.empty()
            results_placeholder = st.empty()
            
            # Check job statuses
            all_complete = True
            job_statuses = []
            
            for job_id in job_ids:
                status = get_job_status(job_id)
                job_statuses.append(status)
                
                if status.get('status') not in ('success', 'failed'):
                    all_complete = False
            
            # Update progress
            completed = sum(1 for s in job_statuses if s.get('status') in ('success', 'failed'))
            progress = (completed / len(job_ids)) * 100 if job_ids else 0
            
            progress_placeholder.progress(progress / 100)
            status_placeholder.info(f"Processing: {completed}/{len(job_ids)} jobs complete")
            
            # Show results
            if all_complete:
                st.session_state.processing_started = False
                st.success("✅ All processing complete!")
                
                with results_placeholder.container():
                    st.subheader("📋 Processing Results")
                    
                    for job_status in job_statuses:
                        status_icon = "✅" if job_status.get('status') == 'success' else "❌"
                        with st.expander(f"{status_icon} Job {job_status.get('job_id', 'Unknown')[:8]}... - {job_status.get('status', 'Unknown')}"):
                            result_data = job_status.get('result', {})
                            
                            if job_status.get('status') == 'success':
                                st.success("✅ Processing successful")
                                
                                if result_data.get('summary_text'):
                                    st.write(f"**Summary:** {result_data['summary_text']}")
                                
                                if result_data.get('invoice_records'):
                                    st.write("**Invoice Records:**")
                                    for inv in result_data['invoice_records']:
                                        st.json(inv)
                                
                                if result_data.get('confidence'):
                                    st.metric("Confidence", f"{result_data['confidence']:.2f}")
                            
                            else:
                                st.error(f"❌ Processing failed: {result_data.get('summary_text', 'Unknown error')}")
                
                # Clear selected after showing results
                if st.button("🔄 Process More Messages"):
                    st.session_state.selected_ids = []
                    st.session_state.processing_jobs = []
                    st.session_state.processing_started = False
                    st.rerun()
            else:
                # Auto-refresh to check status
                time.sleep(2)
                st.rerun()
    
    else:
        st.info("👆 Click 'Find Candidates' to search for invoice-related emails in your Gmail inbox.")


if __name__ == "__main__":
    main()

