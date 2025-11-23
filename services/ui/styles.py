"""Professional styling for Streamlit dashboard - Clean White Theme."""
import streamlit as st
import time

def apply_custom_css():
    """Apply professional custom CSS styling with clean white theme."""
    # Generate cache-busting version
    version = int(time.time())
    
    # CSS content - using triple quotes to avoid f-string issues
    css_content = """
    <style>
        /* Import Google Fonts */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        
        /* ============================================
           GLOBAL THEME - Clean White Theme
           ============================================ */
        :root {{
            --primary: #0066FF;
            --primary-dark: #0052CC;
            --primary-soft: #E6F2FF;
            --bg-main: #FFFFFF;
            --bg-card: #FFFFFF;
            --text-primary: #000000;
            --text-secondary: #333333;
            --border-light: #DDDDDD;
            --border-medium: #CCCCCC;
        }}
        
        /* Main background - WHITE */
        .stApp {{
            background: #FFFFFF !important;
        }}
        
        .main .block-container {{
            background-color: #FFFFFF !important;
            padding-top: 2rem;
            padding-bottom: 3rem;
        }}
        
        /* Text colors - BLACK for visibility */
        body, p, div, span, h1, h2, h3, h4, h5, h6 {{
            color: #000000 !important;
        }}
        
        /* SELECTBOX - White background, BLACK text */
        .stSelectbox {{
            margin: 1rem 0;
        }}
        
        .stSelectbox > div > div {{
            background: #FFFFFF !important;
            border: 2px solid #CCCCCC !important;
            border-radius: 8px !important;
        }}
        
        .stSelectbox label {{
            color: #000000 !important;
            font-weight: 600 !important;
        }}
        
        .stSelectbox [data-baseweb="select"] {{
            color: #000000 !important;
            background: #FFFFFF !important;
        }}
        
        .stSelectbox [data-baseweb="select"] span {{
            color: #000000 !important;
        }}
        
        /* Dropdown menu items */
        [data-baseweb="select"] [data-baseweb="popover"] {{
            background: #FFFFFF !important;
            border: 2px solid #CCCCCC !important;
        }}
        
        [data-baseweb="select"] [data-baseweb="popover"] * {{
            color: #000000 !important;
        }}
        
        [data-baseweb="select"] [data-baseweb="popover"] li {{
            color: #000000 !important;
            background: #FFFFFF !important;
            padding: 0.75rem 1rem !important;
        }}
        
        [data-baseweb="select"] [data-baseweb="popover"] li * {{
            color: #000000 !important;
        }}
        
        [data-baseweb="select"] [data-baseweb="popover"] li:hover {{
            background: #F5F5F5 !important;
            color: #000000 !important;
        }}
        
        [data-baseweb="select"] [data-baseweb="popover"] li:hover * {{
            color: #000000 !important;
        }}
        
        /* Force ALL text in dropdown to be black */
        [data-baseweb="select"] [data-baseweb="popover"] span,
        [data-baseweb="select"] [data-baseweb="popover"] div,
        [data-baseweb="select"] [data-baseweb="popover"] p {{
            color: #000000 !important;
        }}
        
        /* BUTTONS - Proper text colors */
        .stButton > button {{
            font-weight: 600;
            border-radius: 8px;
            padding: 0.75rem 2rem;
        }}
        
        .stButton > button[data-baseweb="button"] {{
            background: var(--primary) !important;
            color: #FFFFFF !important;
        }}
        
        .stButton > button[data-baseweb="button"] p,
        .stButton > button[data-baseweb="button"] span,
        .stButton > button[data-baseweb="button"] div {{
            color: #FFFFFF !important;
        }}
        
        .stButton > button[data-baseweb="button"][type="secondary"] {{
            background: #FFFFFF !important;
            color: var(--primary) !important;
            border: 2px solid var(--primary) !important;
        }}
        
        .stButton > button[data-baseweb="button"][type="secondary"] p,
        .stButton > button[data-baseweb="button"][type="secondary"] span,
        .stButton > button[data-baseweb="button"][type="secondary"] div {{
            color: var(--primary) !important;
        }}
        
        /* Force all selectbox text to be black */
        .stSelectbox * {{
            color: #000000 !important;
        }}
        
        /* Override any white text */
        [style*="color: rgb(255, 255, 255)"],
        [style*="color: white"],
        [style*="color: rgb(26, 28, 36)"] {{
            color: #000000 !important;
        }}
    </style>
    </style>
    <script>
        // ULTRA AGGRESSIVE - Force text visibility
        function forceDropdownText() {
            // Target ALL possible dropdown elements
            var selectors = [
                '[data-baseweb="select"] [data-baseweb="popover"]',
                '[data-baseweb="popover"]',
                '.stSelectbox [data-baseweb="popover"]',
                'ul[role="listbox"]',
                'li[role="option"]'
            ];
            
            selectors.forEach(function(selector) {
                var elements = document.querySelectorAll(selector + ' *');
                elements.forEach(function(el) {
                    el.style.setProperty('color', '#000000', 'important');
                    el.style.setProperty('background-color', '#FFFFFF', 'important');
                });
            });
            
            // Also force on the selectbox itself
            var selects = document.querySelectorAll('[data-baseweb="select"]');
            selects.forEach(function(select) {
                var all = select.querySelectorAll('*');
                all.forEach(function(el) {
                    el.style.setProperty('color', '#000000', 'important');
                });
            });
        }
        
        // Run immediately
        forceDropdownText();
        
        // Run on DOM ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', forceDropdownText);
        }
        
        // Run repeatedly
        setInterval(forceDropdownText, 200);
        
        // Watch for clicks
        document.addEventListener('click', function() {
            setTimeout(forceDropdownText, 50);
            setTimeout(forceDropdownText, 200);
            setTimeout(forceDropdownText, 500);
        });
        
        // Watch for mouseover (when hovering over dropdown)
        document.addEventListener('mouseover', function(e) {
            if (e.target.closest('[data-baseweb="select"]')) {
                forceDropdownText();
            }
        });
    </script>
    """
    
    # Inject CSS with highest priority
    st.markdown(css_content, unsafe_allow_html=True)
    
    # Also inject as a component to ensure it loads
    st.components.v1.html("""
    <style>
        [data-baseweb="popover"] * { color: #000000 !important; }
    </style>
    """, height=0)
