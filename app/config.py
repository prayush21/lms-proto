import os

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app_user:app@localhost:5432/assessment"
)
DATABASE_ADMIN_URL = os.getenv(
    "DATABASE_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/assessment"
)
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-secret")
SQS_ENDPOINT = os.getenv("SQS_ENDPOINT", "http://localhost:9324")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
SCORING_QUEUE = "fast-scoring"
REPORT_QUEUE = "report-generation"
