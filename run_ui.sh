#!/bin/bash
# Script to run Streamlit UI for review candidates

echo "🚀 Starting Review Candidates UI..."
echo ""

# Check if streamlit is installed
if command -v streamlit &> /dev/null; then
    echo "✅ Using local streamlit installation"
    streamlit run services/ui/review_candidates.py
elif python3 -m streamlit --version &> /dev/null; then
    echo "✅ Using python3 -m streamlit"
    python3 -m streamlit run services/ui/review_candidates.py
else
    echo "⚠️  Streamlit not found locally. Using Docker..."
    echo ""
    echo "Starting UI in Docker container..."
    docker-compose -f infra/docker-compose.yml run --rm \
        -p 8502:8501 \
        streamlit_ui \
        streamlit run services/ui/review_candidates.py \
        --server.port=8501 --server.address=0.0.0.0
    echo ""
    echo "✅ UI should be available at: http://localhost:8502"
fi

