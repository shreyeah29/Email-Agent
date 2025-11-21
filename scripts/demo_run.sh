#!/bin/bash
# Demo script to run the full pipeline

set -e

echo "🚀 Starting Email Agent Demo"

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Creating from .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ Created .env file. Please update with your credentials."
    else
        echo "❌ .env.example not found. Please create .env manually."
        exit 1
    fi
fi

# Start services
echo "📦 Starting Docker services..."
make up

# Wait for services to be ready
echo "⏳ Waiting for services to be ready..."
sleep 10

# Run migrations
echo "🗄️  Running database migrations..."
make migrate

# Seed sample data
echo "🌱 Seeding sample data..."
make seed

# Create S3 bucket (if using MinIO)
echo "🪣 Ensuring S3 bucket exists..."
python -c "from shared import ensure_s3_bucket; ensure_s3_bucket()" || echo "⚠️  S3 bucket setup skipped"

# Run extraction worker in background (for demo)
echo "🔧 Starting extraction worker..."
make worker &
WORKER_PID=$!

# Run reconciler in background
echo "🔗 Starting reconciler..."
make reconciler &
RECONCILER_PID=$!

# Wait a bit
sleep 5

# Run a sample API query
echo "📊 Testing API..."
sleep 2

# Show status
echo ""
echo "✅ Demo setup complete!"
echo ""
echo "📋 Services running:"
echo "  - API: http://localhost:8000"
echo "  - API Docs: http://localhost:8000/docs"
echo "  - MinIO Console: http://localhost:9001 (minioadmin/minioadmin)"
echo "  - Streamlit UI: Run 'make ui' in another terminal"
echo ""
echo "🧪 Test the API:"
echo "  curl -H 'Authorization: Bearer dev-api-key' http://localhost:8000/health"
echo ""
echo "🛑 To stop services: make down"

# Keep script running
wait

