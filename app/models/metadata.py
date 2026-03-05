from datetime import datetime, timezone

from pydantic import BaseModel, HttpUrl, Field
from typing import Dict, Any

# Schema for incoming API requests
class URLRequest(BaseModel):
    url: HttpUrl = Field(..., description="The fully qualified URL to scrape")

# Schema representing the logical structure in MongoDB
class MetadataDocument(BaseModel):
    url: str
    headers: Dict[str, Any]
    cookies: Dict[str, Any]
    page_source: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))