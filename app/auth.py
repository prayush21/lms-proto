from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException

from app.config import JWT_SECRET
from app.context import RequestIdentity


def issue_token(tenant_id: str, user_id: str, role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "tenant_id": tenant_id,
            "sub": user_id,
            "role": role,
            "iat": now,
            "exp": now + timedelta(hours=12),
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def decode_token(token: str) -> RequestIdentity:
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return RequestIdentity(
            tenant_id=claims["tenant_id"], user_id=claims["sub"], role=claims["role"]
        )
    except (jwt.PyJWTError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
