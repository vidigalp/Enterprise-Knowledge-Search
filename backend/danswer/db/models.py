import datetime
from enum import Enum as PyEnum
from typing import Any
from typing import List
from typing import Literal
from typing import NotRequired
from typing import Optional
from typing import TypedDict
from uuid import UUID

from fastapi_users.db import SQLAlchemyBaseOAuthAccountTableUUID
from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import func
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import Sequence
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from danswer.auth.schemas import UserRole
from danswer.configs.constants import DEFAULT_BOOST
from danswer.configs.constants import DocumentSource
from danswer.configs.constants import MessageType
from danswer.configs.constants import SearchFeedbackType
from danswer.connectors.models import InputType
from danswer.search.models import RecencyBiasSetting
from danswer.search.models import SearchType


class IndexingStatus(str, PyEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


# these may differ in the future, which is why we're okay with this duplication
class DeletionStatus(str, PyEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


# Consistent with Celery task statuses
class TaskStatus(str, PyEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class IndexModelStatus(str, PyEnum):
    PAST = "PAST"
    PRESENT = "PRESENT"
    FUTURE = "FUTURE"


class Base(DeclarativeBase):
    pass


"""
Auth/Authz (users, permissions, access) Tables
"""


class OAuthAccount(SQLAlchemyBaseOAuthAccountTableUUID, Base):
    # even an almost empty token from keycloak will not fit the default 1024 bytes
    access_token: Mapped[str] = mapped_column(Text, nullable=False)  # type: ignore


class User(SQLAlchemyBaseUserTableUUID, Base):
    oauth_accounts: Mapped[List[OAuthAccount]] = relationship(
        "OAuthAccount", lazy="joined"
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, default=UserRole.BASIC)
    )
    credentials: Mapped[List["Credential"]] = relationship(
        "Credential", back_populates="user", lazy="joined"
    )
    chat_sessions: Mapped[List["ChatSession"]] = relationship(
        "ChatSession", back_populates="user"
    )
    prompts: Mapped[List["Prompt"]] = relationship("Prompt", back_populates="user")
    personas: Mapped[List["Persona"]] = relationship("Persona", back_populates="user")


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    pass


class ApiKey(Base):
    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hashed_api_key: Mapped[str] = mapped_column(String, unique=True)
    api_key_display: Mapped[str] = mapped_column(String, unique=True)
    # the ID of the "user" who represents the access credentials for the API key
    user_id: Mapped[UUID] = mapped_column(ForeignKey("user.id"), nullable=False)
    # the ID of the user who owns the key
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


"""
Association Tables
NOTE: must be at the top since they are referenced by other tables
"""


class Persona__DocumentSet(Base):
    __tablename__ = "persona__document_set"

    persona_id: Mapped[int] = mapped_column(ForeignKey("persona.id"), primary_key=True)
    document_set_id: Mapped[int] = mapped_column(
        ForeignKey("document_set.id"), primary_key=True
    )


class Persona__Prompt(Base):
    __tablename__ = "persona__prompt"

    persona_id: Mapped[int] = mapped_column(ForeignKey("persona.id"), primary_key=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompt.id"), primary_key=True)


class DocumentSet__ConnectorCredentialPair(Base):
    __tablename__ = "document_set__connector_credential_pair"

    document_set_id: Mapped[int] = mapped_column(
        ForeignKey("document_set.id"), primary_key=True
    )
    connector_credential_pair_id: Mapped[int] = mapped_column(
        ForeignKey("connector_credential_pair.id"), primary_key=True
    )
    # if `True`, then is part of the current state of the document set
    # if `False`, then is a part of the prior state of the document set
    # rows with `is_current=False` should be deleted when the document
    # set is updated and should not exist for a given document set if
    # `DocumentSet.is_up_to_date == True`
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        primary_key=True,
    )

    document_set: Mapped["DocumentSet"] = relationship("DocumentSet")


class ChatMessage__SearchDoc(Base):
    __tablename__ = "chat_message__search_doc"

    chat_message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_message.id"), primary_key=True
    )
    search_doc_id: Mapped[int] = mapped_column(
        ForeignKey("search_doc.id"), primary_key=True
    )


class Document__Tag(Base):
    __tablename__ = "document__tag"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("document.id"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"), primary_key=True)


"""
Documents/Indexing Tables
"""


class ConnectorCredentialPair(Base):
    """Connectors and Credentials can have a many-to-many relationship
    I.e. A Confluence Connector may have multiple admin users who can run it with their own credentials
    I.e. An admin user may use the same credential to index multiple Confluence Spaces
    """

    __tablename__ = "connector_credential_pair"
    # NOTE: this `id` column has to use `Sequence` instead of `autoincrement=True`
    # due to some SQLAlchemy quirks + this not being a primary key column
    id: Mapped[int] = mapped_column(
        Integer,
        Sequence("connector_credential_pair_id_seq"),
        unique=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("connector.id"), primary_key=True
    )
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("credential.id"), primary_key=True
    )
    # controls whether the documents indexed by this CC pair are visible to all
    # or if they are only visible to those with that are given explicit access
    # (e.g. via owning the credential or being a part of a group that is given access)
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    # Time finished, not used for calculating backend jobs which uses time started (created)
    last_successful_index_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_attempt_status: Mapped[IndexingStatus | None] = mapped_column(
        Enum(IndexingStatus)
    )
    total_docs_indexed: Mapped[int] = mapped_column(Integer, default=0)

    connector: Mapped["Connector"] = relationship(
        "Connector", back_populates="credentials"
    )
    credential: Mapped["Credential"] = relationship(
        "Credential", back_populates="connectors"
    )
    document_sets: Mapped[List["DocumentSet"]] = relationship(
        "DocumentSet",
        secondary=DocumentSet__ConnectorCredentialPair.__table__,
        back_populates="connector_credential_pairs",
        overlaps="document_set",
    )


class Document(Base):
    __tablename__ = "document"

    # this should correspond to the ID of the document
    # (as is passed around in Danswer)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    from_ingestion_api: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=True
    )
    # 0 for neutral, positive for mostly endorse, negative for mostly reject
    boost: Mapped[int] = mapped_column(Integer, default=DEFAULT_BOOST)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    semantic_id: Mapped[str] = mapped_column(String)
    # First Section's link
    link: Mapped[str | None] = mapped_column(String, nullable=True)
    # The updated time is also used as a measure of the last successful state of the doc
    # pulled from the source (to help skip reindexing already updated docs in case of
    # connector retries)
    doc_updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The following are not attached to User because the account/email may not be known
    # within Danswer
    # Something like the document creator
    primary_owners: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(String), nullable=True
    )
    # Something like assignee or space owner
    secondary_owners: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(String), nullable=True
    )
    # TODO if more sensitive data is added here for display, make sure to add user/group permission

    retrieval_feedbacks: Mapped[List["DocumentRetrievalFeedback"]] = relationship(
        "DocumentRetrievalFeedback", back_populates="document"
    )
    tags = relationship(
        "Tag",
        secondary="document__tag",
        back_populates="documents",
    )


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_key: Mapped[str] = mapped_column(String)
    tag_value: Mapped[str] = mapped_column(String)
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource))

    documents = relationship(
        "Document",
        secondary="document__tag",
        back_populates="tags",
    )

    __table_args__ = (
        UniqueConstraint(
            "tag_key", "tag_value", "source", name="_tag_key_value_source_uc"
        ),
    )


class Connector(Base):
    __tablename__ = "connector"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    source: Mapped[DocumentSource] = mapped_column(
        Enum(DocumentSource, native_enum=False)
    )
    input_type = mapped_column(Enum(InputType, native_enum=False))
    connector_specific_config: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB()
    )
    refresh_freq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_created: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    time_updated: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    credentials: Mapped[List["ConnectorCredentialPair"]] = relationship(
        "ConnectorCredentialPair",
        back_populates="connector",
        cascade="all, delete-orphan",
    )
    documents_by_connector: Mapped[
        List["DocumentByConnectorCredentialPair"]
    ] = relationship("DocumentByConnectorCredentialPair", back_populates="connector")
    index_attempts: Mapped[List["IndexAttempt"]] = relationship(
        "IndexAttempt", back_populates="connector"
    )


class Credential(Base):
    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    credential_json: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB())
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    # if `true`, then all Admins will have access to the credential
    admin_public: Mapped[bool] = mapped_column(Boolean, default=True)
    time_created: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    time_updated: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    connectors: Mapped[List["ConnectorCredentialPair"]] = relationship(
        "ConnectorCredentialPair",
        back_populates="credential",
        cascade="all, delete-orphan",
    )
    documents_by_credential: Mapped[
        List["DocumentByConnectorCredentialPair"]
    ] = relationship("DocumentByConnectorCredentialPair", back_populates="credential")
    index_attempts: Mapped[List["IndexAttempt"]] = relationship(
        "IndexAttempt", back_populates="credential"
    )
    user: Mapped[User | None] = relationship("User", back_populates="credentials")


class EmbeddingModel(Base):
    __tablename__ = "embedding_model"
    # ID is used also to indicate the order that the models are configured by the admin
    id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(String)
    model_dim: Mapped[int] = mapped_column(Integer)
    normalize: Mapped[bool] = mapped_column(Boolean)
    query_prefix: Mapped[str] = mapped_column(String)
    passage_prefix: Mapped[str] = mapped_column(String)
    status: Mapped[IndexModelStatus] = mapped_column(Enum(IndexModelStatus))
    index_name: Mapped[str] = mapped_column(String)

    index_attempts: Mapped[List["IndexAttempt"]] = relationship(
        "IndexAttempt", back_populates="embedding_model"
    )

    __table_args__ = (
        Index(
            "ix_embedding_model_present_unique",
            "status",
            unique=True,
            postgresql_where=(status == IndexModelStatus.PRESENT),
        ),
        Index(
            "ix_embedding_model_future_unique",
            "status",
            unique=True,
            postgresql_where=(status == IndexModelStatus.FUTURE),
        ),
    )


class IndexAttempt(Base):
    """
    Represents an attempt to index a group of 1 or more documents from a
    source. For example, a single pull from Google Drive, a single event from
    slack event API, or a single website crawl.
    """

    __tablename__ = "index_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connector.id"),
        nullable=True,
    )
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credential.id"),
        nullable=True,
    )
    # Some index attempts that run from beginning will still have this as False
    # This is only for attempts that are explicitly marked as from the start via
    # the run once API
    from_beginning: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[IndexingStatus] = mapped_column(Enum(IndexingStatus))
    # The two below may be slightly out of sync if user switches Embedding Model
    new_docs_indexed: Mapped[int | None] = mapped_column(Integer, default=0)
    total_docs_indexed: Mapped[int | None] = mapped_column(Integer, default=0)
    # only filled if status = "failed"
    error_msg: Mapped[str | None] = mapped_column(Text, default=None)
    # only filled if status = "failed" AND an unhandled exception caused the failure
    full_exception_trace: Mapped[str | None] = mapped_column(Text, default=None)
    # Nullable because in the past, we didn't allow swapping out embedding models live
    embedding_model_id: Mapped[int] = mapped_column(
        ForeignKey("embedding_model.id"),
        nullable=False,
    )
    time_created: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    # when the actual indexing run began
    # NOTE: will use the api_server clock rather than DB server clock
    time_started: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    time_updated: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    connector: Mapped[Connector] = relationship(
        "Connector", back_populates="index_attempts"
    )
    credential: Mapped[Credential] = relationship(
        "Credential", back_populates="index_attempts"
    )
    embedding_model: Mapped[EmbeddingModel] = relationship(
        "EmbeddingModel", back_populates="index_attempts"
    )

    __table_args__ = (
        Index(
            "ix_index_attempt_latest_for_connector_credential_pair",
            "connector_id",
            "credential_id",
            "time_created",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<IndexAttempt(id={self.id!r}, "
            f"connector_id={self.connector_id!r}, "
            f"status={self.status!r}, "
            f"error_msg={self.error_msg!r})>"
            f"time_created={self.time_created!r}, "
            f"time_updated={self.time_updated!r}, "
        )


class DocumentByConnectorCredentialPair(Base):
    """Represents an indexing of a document by a specific connector / credential pair"""

    __tablename__ = "document_by_connector_credential_pair"

    id: Mapped[str] = mapped_column(ForeignKey("document.id"), primary_key=True)
    # TODO: transition this to use the ConnectorCredentialPair id directly
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("connector.id"), primary_key=True
    )
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("credential.id"), primary_key=True
    )

    connector: Mapped[Connector] = relationship(
        "Connector", back_populates="documents_by_connector"
    )
    credential: Mapped[Credential] = relationship(
        "Credential", back_populates="documents_by_credential"
    )


"""
Messages Tables
"""


class SearchDoc(Base):
    """Different from Document table. This one stores the state of a document from a retrieval.
    This allows chat sessions to be replayed with the searched docs

    Notably, this does not include the contents of the Document/Chunk, during inference if a stored
    SearchDoc is selected, an inference must be remade to retrieve the contents
    """

    __tablename__ = "search_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(String)
    chunk_ind: Mapped[int] = mapped_column(Integer)
    semantic_id: Mapped[str] = mapped_column(String)
    link: Mapped[str | None] = mapped_column(String, nullable=True)
    blurb: Mapped[str] = mapped_column(String)
    boost: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource))
    hidden: Mapped[bool] = mapped_column(Boolean)
    doc_metadata: Mapped[dict[str, str | list[str]]] = mapped_column(postgresql.JSONB())
    score: Mapped[float] = mapped_column(Float)
    match_highlights: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String))
    # This is for the document, not this row in the table
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    primary_owners: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(String), nullable=True
    )
    secondary_owners: Mapped[list[str] | None] = mapped_column(
        postgresql.ARRAY(String), nullable=True
    )

    chat_messages = relationship(
        "ChatMessage",
        secondary="chat_message__search_doc",
        back_populates="search_docs",
    )


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("persona.id"))
    description: Mapped[str] = mapped_column(Text)
    # One-shot direct answering, currently the two types of chats are not mixed
    one_shot: Mapped[bool] = mapped_column(Boolean, default=False)
    # Only ever set to True if system is set to not hard-delete chats
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    time_updated: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    time_created: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship("User", back_populates="chat_sessions")
    messages: Mapped[List["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="chat_session", cascade="delete"
    )
    persona: Mapped["Persona"] = relationship("Persona")


class ChatMessage(Base):
    """Note, the first message in a chain has no contents, it's a workaround to allow edits
    on the first message of a session, an empty root node basically

    Since every user message is followed by a LLM response, chat messages generally come in pairs.
    Keeping them as separate messages however for future Agentification extensions
    Fields will be largely duplicated in the pair.
    """

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_session_id: Mapped[int] = mapped_column(ForeignKey("chat_session.id"))
    parent_message: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_child_message: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    rephrased_query: Mapped[str] = mapped_column(Text, nullable=True)
    # If None, then there is no answer generation, it's the special case of only
    # showing the user the retrieved docs
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompt.id"))
    # If prompt is None, then token_count is 0 as this message won't be passed into
    # the LLM's context (not included in the history of messages)
    token_count: Mapped[int] = mapped_column(Integer)
    message_type: Mapped[MessageType] = mapped_column(Enum(MessageType))
    # Maps the citation numbers to a SearchDoc id
    citations: Mapped[dict[int, int]] = mapped_column(postgresql.JSONB(), nullable=True)
    # Only applies for LLM
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_sent: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    chat_session: Mapped[ChatSession] = relationship("ChatSession")
    prompt: Mapped[Optional["Prompt"]] = relationship("Prompt")
    chat_message_feedbacks: Mapped[List["ChatMessageFeedback"]] = relationship(
        "ChatMessageFeedback", back_populates="chat_message"
    )
    document_feedbacks: Mapped[List["DocumentRetrievalFeedback"]] = relationship(
        "DocumentRetrievalFeedback", back_populates="chat_message"
    )
    search_docs = relationship(
        "SearchDoc",
        secondary="chat_message__search_doc",
        back_populates="chat_messages",
    )


"""
Feedback, Logging, Metrics Tables
"""


class DocumentRetrievalFeedback(Base):
    __tablename__ = "document_retrieval_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_message_id: Mapped[int] = mapped_column(ForeignKey("chat_message.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id"))
    # How high up this document is in the results, 1 for first
    document_rank: Mapped[int] = mapped_column(Integer)
    clicked: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback: Mapped[SearchFeedbackType | None] = mapped_column(
        Enum(SearchFeedbackType), nullable=True
    )

    chat_message: Mapped[ChatMessage] = relationship(
        "ChatMessage", back_populates="document_feedbacks"
    )
    document: Mapped[Document] = relationship(
        "Document", back_populates="retrieval_feedbacks"
    )


class ChatMessageFeedback(Base):
    __tablename__ = "chat_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_message_id: Mapped[int] = mapped_column(ForeignKey("chat_message.id"))
    is_positive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    required_followup: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    chat_message: Mapped[ChatMessage] = relationship(
        "ChatMessage", back_populates="chat_message_feedbacks"
    )


"""
Structures, Organizational, Configurations Tables
"""


class DocumentSet(Base):
    __tablename__ = "document_set"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str] = mapped_column(String)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    # Whether changes to the document set have been propagated
    is_up_to_date: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    connector_credential_pairs: Mapped[list[ConnectorCredentialPair]] = relationship(
        "ConnectorCredentialPair",
        secondary=DocumentSet__ConnectorCredentialPair.__table__,
        back_populates="document_sets",
        overlaps="document_set",
    )
    personas: Mapped[list["Persona"]] = relationship(
        "Persona",
        secondary=Persona__DocumentSet.__table__,
        back_populates="document_sets",
    )


class Prompt(Base):
    __tablename__ = "prompt"

    id: Mapped[int] = mapped_column(primary_key=True)
    # If not belong to a user, then it's shared
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    system_prompt: Mapped[str] = mapped_column(Text)
    task_prompt: Mapped[str] = mapped_column(Text)
    include_citations: Mapped[bool] = mapped_column(Boolean, default=True)
    datetime_aware: Mapped[bool] = mapped_column(Boolean, default=True)
    # Default prompts are configured via backend during deployment
    # Treated specially (cannot be user edited etc.)
    default_prompt: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship("User", back_populates="prompts")
    personas: Mapped[list["Persona"]] = relationship(
        "Persona",
        secondary=Persona__Prompt.__table__,
        back_populates="prompts",
    )


class Persona(Base):
    __tablename__ = "persona"

    id: Mapped[int] = mapped_column(primary_key=True)
    # If not belong to a user, then it's shared
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    # Currently stored but unused, all flows use hybrid
    search_type: Mapped[SearchType] = mapped_column(
        Enum(SearchType), default=SearchType.HYBRID
    )
    # Number of chunks to pass to the LLM for generation.
    num_chunks: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Pass every chunk through LLM for evaluation, fairly expensive
    # Can be turned off globally by admin, in which case, this setting is ignored
    llm_relevance_filter: Mapped[bool] = mapped_column(Boolean)
    # Enables using LLM to extract time and source type filters
    # Can also be admin disabled globally
    llm_filter_extraction: Mapped[bool] = mapped_column(Boolean)
    recency_bias: Mapped[RecencyBiasSetting] = mapped_column(Enum(RecencyBiasSetting))
    # Allows the Persona to specify a different LLM version than is controlled
    # globablly via env variables. For flexibility, validity is not currently enforced
    # NOTE: only is applied on the actual response generation - is not used for things like
    # auto-detected time filters, relevance filters, etc.
    llm_model_version_override: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    # Default personas are configured via backend during deployment
    # Treated specially (cannot be user edited etc.)
    default_persona: Mapped[bool] = mapped_column(Boolean, default=False)
    # controls whether the persona is available to be selected by users
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    # controls the ordering of personas in the UI
    # higher priority personas are displayed first, ties are resolved by the ID,
    # where lower value IDs (e.g. created earlier) are displayed first
    display_priority: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    # These are only defaults, users can select from all if desired
    prompts: Mapped[list[Prompt]] = relationship(
        "Prompt",
        secondary=Persona__Prompt.__table__,
        back_populates="personas",
    )
    # These are only defaults, users can select from all if desired
    document_sets: Mapped[list[DocumentSet]] = relationship(
        "DocumentSet",
        secondary=Persona__DocumentSet.__table__,
        back_populates="personas",
    )
    user: Mapped[User] = relationship("User", back_populates="personas")

    # Default personas loaded via yaml cannot have the same name
    __table_args__ = (
        Index(
            "_default_persona_name_idx",
            "name",
            unique=True,
            postgresql_where=(default_persona == True),  # noqa: E712
        ),
    )


AllowedAnswerFilters = (
    Literal["well_answered_postfilter"] | Literal["questionmark_prefilter"]
)


class ChannelConfig(TypedDict):
    """NOTE: is a `TypedDict` so it can be used as a type hint for a JSONB column
    in Postgres"""

    channel_names: list[str]
    respond_tag_only: NotRequired[bool]  # defaults to False
    respond_to_bots: NotRequired[bool]  # defaults to False
    respond_team_member_list: NotRequired[list[str]]
    answer_filters: NotRequired[list[AllowedAnswerFilters]]
    # If None then no follow up
    # If empty list, follow up with no tags
    follow_up_tags: NotRequired[list[str]]


class SlackBotConfig(Base):
    __tablename__ = "slack_bot_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("persona.id"), nullable=True
    )
    # JSON for flexibility. Contains things like: channel name, team members, etc.
    channel_config: Mapped[ChannelConfig] = mapped_column(
        postgresql.JSONB(), nullable=False
    )

    persona: Mapped[Persona | None] = relationship("Persona")


class TaskQueueState(Base):
    # Currently refers to Celery Tasks
    __tablename__ = "task_queue_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Celery task id
    task_id: Mapped[str] = mapped_column(String)
    # For any job type, this would be the same
    task_name: Mapped[str] = mapped_column(String)
    # Note that if the task dies, this won't necessarily be marked FAILED correctly
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus))
    start_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    register_time: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
