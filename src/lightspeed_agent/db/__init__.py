"""Database module for persistence.

Provides SQLAlchemy async engine, session management, and ORM models
for PostgreSQL (production) or SQLite (development).
"""

from lightspeed_agent.db.base import (
    Base,
    close_database,
    get_engine,
    get_session,
    get_session_factory,
    init_database,
)
from lightspeed_agent.db.models import (
    DCRClientModel,
    MarketplaceAccountModel,
    MarketplaceEntitlementModel,
    UsageRecordModel,
)

__all__ = [
    # Base and session management
    "Base",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_database",
    "close_database",
    # Models
    "MarketplaceAccountModel",
    "MarketplaceEntitlementModel",
    "DCRClientModel",
    "UsageRecordModel",
]
