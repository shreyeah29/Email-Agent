.PHONY: up down migrate seed worker ui test clean

up:
	docker-compose -f infra/docker-compose.yml up -d

down:
	docker-compose -f infra/docker-compose.yml down

logs:
	docker-compose -f infra/docker-compose.yml logs -f

migrate:
	docker-compose -f infra/docker-compose.yml exec postgres psql -U invoice_user -d invoices -f /migrations/001_create_core_tables.sql || \
	psql $(DATABASE_URL) -f migrations/001_create_core_tables.sql

seed:
	docker-compose -f infra/docker-compose.yml exec postgres psql -U invoice_user -d invoices -f /migrations/002_seed_sample_data.sql || \
	psql $(DATABASE_URL) -f migrations/002_seed_sample_data.sql

worker:
	python services/extractor/worker.py

reconciler:
	python services/reconciler/worker.py

ingestion:
	python services/ingestion/main.py

ui:
	streamlit run services/ui/review.py --server.port 8501

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

clean:
	docker-compose -f infra/docker-compose.yml down -v
	find . -type d -name __pycache__ -exec rm -r {} +
	find . -type f -name "*.pyc" -delete

build:
	docker-compose -f infra/docker-compose.yml build

