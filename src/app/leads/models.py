"""ORM models for the leads bounded context.

Moved verbatim from ``app.models.py`` during the R-STRUCT-1 migration.
Column definitions, comments, and relationships are unchanged; only the
declarative base moved, from the module-local ``declarative_base()`` to the
single shared ``app.core.db.Base`` (spec §3.2).

Relationships that point at another context resolve through the shared
registry, which ``app/models.py`` guarantees is fully populated.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)

from app.core.db import Base
from app.core.enums import (  # noqa: F401
    FileType,
    FileUploadContext,
    FileUsageType,
    MessageType,
    ReportStatus,
    SubscriptionStatus,
    SubscriptionTier,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UploadedFileStatus,
)


class Subscriber(Base):
    """
    Table representing subscribers with email information.

    Attributes:
        id (int): Unique identifier for each subscriber (primary key, auto-increment).
        email (str): Email address of the subscriber (non-unique).
        created_at (datetime): The timestamp when the subscriber was created (UTC).
    """

    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    email = Column(String(255), nullable=False, comment="Email address of the subscriber")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when subscriber was created in UTC",
    )

    def __repr__(self):
        return f"<Subscriber(id={self.id}, email='{self.email}')>"


class Request(Base):
    """
    Table representing requests from users/visitors.

    Attributes:
        id (int): Unique identifier for each request (primary key, auto-increment).
        name (str): Name of the person making the request (non-nullable, non-unique).
        email (str): Email address of the requester (non-nullable, non-unique).
        website (str): Website URL of the requester (nullable, non-unique).
        description (str): Description of the request (non-nullable, non-unique).
    """

    __tablename__ = "requests"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    name = Column(String(255), nullable=False, comment="Name of the person making the request")
    email = Column(String(255), nullable=False, comment="Email address of the requester")
    website = Column(String(500), nullable=True, comment="Website URL of the requester")
    description = Column(Text, nullable=True, comment="Description of the request")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when request was created",
    )

    def __repr__(self):
        return f"<Request(id={self.id}, name='{self.name}', email='{self.email}')>"


class CallBooking(Base):
    """
    Table representing call booking requests from users/visitors.

    Attributes:
        id (int): Unique identifier for each call booking (primary key, auto-increment).
        name (str): Name of the person booking the call (non-nullable).
        email (str): Email address of the requester (non-nullable).
        phone_country_code (str): Country code of the phone number (e.g. +1, +91).
        phone_number (str): Phone number without country code.
        brief (str): Optional brief/description from the user.
        created_at (datetime): Timestamp when the booking was created.
    """

    __tablename__ = "call_bookings"

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    name = Column(String(255), nullable=False, comment="Name of the person booking the call")
    email = Column(String(255), nullable=False, comment="Email address of the requester")
    phone_country_code = Column(
        String(20), nullable=False, comment="Country code of the phone number"
    )
    phone_number = Column(String(50), nullable=False, comment="Phone number without country code")
    brief = Column(Text, nullable=True, comment="Optional brief or description from the user")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the booking was created",
    )

    def __repr__(self):
        return f"<CallBooking(id={self.id}, name='{self.name}', email='{self.email}')>"


# Standalone indexes, moved with the models they index.
Index("idx_subscribers_email", Subscriber.email)
Index("idx_requests_email", Request.email)
Index("idx_call_bookings_email", CallBooking.email)
Index("idx_call_bookings_created_at", CallBooking.created_at)
