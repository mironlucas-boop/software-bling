from pydantic import BaseModel
from typing import Any, Optional


class WebhookPayload(BaseModel):
    event: Optional[str] = None
    data: Optional[dict[str, Any]] = None
