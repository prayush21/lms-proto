from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestIdentity:
    tenant_id: str
    user_id: str
    role: str


identity_context: ContextVar[RequestIdentity | None] = ContextVar(
    "identity", default=None
)


def identity() -> RequestIdentity:
    value = identity_context.get()
    if value is None:
        raise RuntimeError("request identity is not set")
    return value
