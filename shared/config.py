"""Shared configuration and database setup."""
import os
from typing import Optional
from pydantic_settings import BaseSettings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
import redis
from botocore.config import Config as BotoConfig
import boto3

Base = declarative_base()


class Settings(BaseSettings):
    """Application settings from environment variables."""
    
    # Database
    database_url: str = os.getenv("DATABASE_URL", "postgresql://invoice_user:invoice_pass@localhost:5432/invoices")
    
    # Redis
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # S3
    s3_endpoint_url: Optional[str] = os.getenv("S3_ENDPOINT_URL")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "minioadmin")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "minioadmin")
    s3_bucket: str = os.getenv("S3_BUCKET", "inbox-bucket")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    
    # Gmail
    gmail_client_id: Optional[str] = os.getenv("GMAIL_CLIENT_ID")
    gmail_client_secret: Optional[str] = os.getenv("GMAIL_CLIENT_SECRET")
    gmail_refresh_token: Optional[str] = os.getenv("GMAIL_REFRESH_TOKEN")
    
    
    # Security
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    api_key: str = os.getenv("API_KEY", "dev-api-key")
    
    # Application
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    extractor_version: str = os.getenv("EXTRACTOR_VERSION", "v1.0.0")
    ui_password: str = os.getenv("UI_PASSWORD", "admin123")
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields in .env


settings = Settings()

# Database setup
engine = create_engine(
    settings.database_url,
    poolclass=NullPool,
    echo=False
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Database session dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Redis setup
redis_client = redis.from_url(settings.redis_url, decode_responses=True)

# S3 setup
s3_config = BotoConfig(
    signature_version='s3v4',
    region_name=settings.s3_region
)

s3_client = boto3.client(
    's3',
    endpoint_url=settings.s3_endpoint_url,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    config=s3_config
)


def ensure_s3_bucket():
    """Ensure S3 bucket exists."""
    try:
        s3_client.head_bucket(Bucket=settings.s3_bucket)
    except:
        s3_client.create_bucket(Bucket=settings.s3_bucket)

