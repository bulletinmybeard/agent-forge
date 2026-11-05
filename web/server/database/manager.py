"""ChatDatabase — persistence manager for chat sessions and messages.

Adapted from py-rentwatch-dev's DatabaseManager. Same patterns:
  - sessionmaker(autocommit=False, autoflush=False)
  - Context-managed sessions with expunge() before return
  - Naive local datetimes
  - Schema via Alembic (``create_tables`` → ``migrate.upgrade``)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, func, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from web.server.database.migrate import upgrade as alembic_upgrade
from web.server.identity import (
    ADMIN_USER_ID,
    admin_username,
    current_user_id,
    hash_secret,
    new_api_key,
    new_session_token,
    verify_password,
)

from .models import (
    ApiKey,
    AuthSession,
    Base,
    ChatMessage,
    ChatSession,
    CommandNote,
    CommandPermissionActive,
    CommandPermissionProfileRow,
    CommandPolicyOverride,
    Connection,
    MonitorCheck,
    MonitorJob,
    MonitorSnapshot,
    ScheduledJob,
    ScheduledJobRun,
    SessionInstruction,
    User,
    UserFact,
)

logger = logging.getLogger(__name__)


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite pragmas on every new connection.

    - ``journal_mode=DELETE`` — required when the native worker and
      the Docker agentforge-web container share the same SQLite file via bind
      mount.  WAL's shared-memory files (-wal, -shm) don't sync across
      the Docker filesystem boundary.
    - ``foreign_keys=ON`` — SQLite does NOT enforce foreign keys by
      default.  Without this, ``ON DELETE CASCADE`` on monitor_snapshots
      / monitor_checks is silently ignored.
    """
    cursor = dbapi_conn.execute("PRAGMA journal_mode=DELETE")
    cursor.close()
    cursor = dbapi_conn.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class ChatDatabase:
    """SQLite-backed storage for chat sessions and messages."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # NullPool: no connection reuse — each session opens a fresh SQLite
        # connection.  This guarantees the poll loop in the Docker container
        # always sees the latest rows committed by the native worker.
        # Without this, the default QueuePool can hold a connection whose
        # read snapshot is stale.
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        # Force DELETE journal mode on every new connection
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        logger.info("ChatDatabase initialised at %s (journal_mode=DELETE)", self.db_path)

    def create_tables(self) -> None:
        """Apply Alembic migrations to head (idempotent).

        Replaces the old ``create_all`` + ad-hoc ``ALTER TABLE`` path.
        See ``web.server.database.migrate`` and ``agentforge-db`` CLI.
        """
        alembic_upgrade(self.db_path)
        self.ensure_admin()
        logger.info("Database schema ready (Alembic head) at %s", self.db_path)

    def drop_tables(self) -> None:
        """Drop all tables (destructive — for testing/reset)."""
        Base.metadata.drop_all(bind=self.engine)

    # -- Owner -----------------------------------------------------------------

    def _actor(self) -> str | None:
        """User id to filter on. ``None`` means auth is off and rows are shared."""
        return current_user_id()

    def _owner_id(self) -> str:
        return self._actor() or ADMIN_USER_ID

    def _scoped(self, query, column):
        """Keep ``query`` as-is when auth is off. Otherwise only this user's rows."""
        actor = self._actor()
        if actor:
            return query.filter(column == actor)
        return query

    def _visible(self, session, session_id: str) -> bool:
        actor = self._actor()
        if not actor:
            return True
        owner = session.query(ChatSession.user_id).filter(ChatSession.id == session_id).scalar()
        return owner == actor

    def list_session_ids(self) -> list[str]:
        """Every session id the current actor can see. Unfiltered when auth is off."""
        with self.SessionLocal() as session:
            query = session.query(ChatSession.id)
            actor = self._actor()
            if actor:
                query = query.filter(ChatSession.user_id == actor)
            return [row[0] for row in query.all()]

    def get_session_owner(self, session_id: str) -> str | None:
        """Owner id, ignoring the actor filter. ``None`` when the session does not exist."""
        with self.SessionLocal() as session:
            return session.query(ChatSession.user_id).filter(ChatSession.id == session_id).scalar()

    # -- Users and credentials -------------------------------------------------

    def ensure_admin(self) -> None:
        """Insert the fixed admin row when it is missing.

        The name comes from ``AGENTFORGE_ADMIN_USERNAME`` (default ``admin``).
        An existing row is left alone. Does not touch the password.
        """
        with self.SessionLocal() as session:
            if session.get(User, ADMIN_USER_ID) is not None:
                return
            session.add(
                User(
                    id=ADMIN_USER_ID,
                    username=admin_username(),
                    password_hash="",
                    is_admin=True,
                    created_at=datetime.now(),
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("username already exists") from exc

    def set_admin_username(self, username: str) -> None:
        """Rename the fixed admin row. No-op when the name already matches."""
        name = (username or "").strip().lower()
        with self.SessionLocal() as session:
            user = session.get(User, ADMIN_USER_ID)
            if user is None or user.username == name:
                return
            user.username = name
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("username already exists") from exc

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        with self.SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                return
            user.password_hash = password_hash
            session.commit()

    def authenticate(self, username: str, password: str) -> User | None:
        name = (username or "").strip().lower()
        if not name or not password:
            return None
        with self.SessionLocal() as session:
            user = session.query(User).filter(User.username == name).first()
            if user is None or not user.password_hash:
                return None
            if not verify_password(password, user.password_hash):
                return None
            session.expunge(user)
            return user

    def get_user(self, user_id: str) -> User | None:
        with self.SessionLocal() as session:
            user = session.get(User, user_id)
            if user is not None:
                session.expunge(user)
            return user

    def user_exists(self, user_id: str) -> bool:
        with self.SessionLocal() as session:
            return session.get(User, user_id) is not None

    def list_users(self) -> list[User]:
        with self.SessionLocal() as session:
            users = session.query(User).order_by(User.created_at.asc()).all()
            session.expunge_all()
            return users

    def create_user(self, username: str, password_hash: str) -> User:
        """Create a non-admin user. Raises ``ValueError`` when the name is taken."""
        name = (username or "").strip().lower()
        with self.SessionLocal() as session:
            user = User(
                id=str(uuid.uuid4()),
                username=name,
                password_hash=password_hash,
                is_admin=False,
                created_at=datetime.now(),
            )
            session.add(user)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("username already exists") from exc
            session.refresh(user)
            session.expunge(user)
            return user

    def update_user(self, user_id: str, *, username: str | None = None, password_hash: str | None = None) -> User:
        """Rename a user or replace the password hash.

        The admin row is refused. The name and password come from the
        environment and are applied again on the next start. Raises
        ``ValueError`` with ``user not found``, ``cannot edit the admin user``,
        or ``username already exists``.
        """
        with self.SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                raise ValueError("user not found")
            if user.is_admin:
                raise ValueError("cannot edit the admin user")
            if username:
                user.username = username.strip().lower()
            if password_hash:
                user.password_hash = password_hash
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("username already exists") from exc
            session.refresh(user)
            session.expunge(user)
            return user

    def delete_user(self, user_id: str) -> list[str]:
        """Delete a non-admin user and the rows that belong to them.

        Returns the session ids that were removed, so the caller can drop
        upload directories. Raises ``ValueError`` with ``user not found`` or
        ``cannot delete the admin user``.
        """
        with self.SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                raise ValueError("user not found")
            if user.is_admin:
                raise ValueError("cannot delete the admin user")
            owned_ids = [row[0] for row in session.query(ChatSession.id).filter(ChatSession.user_id == user_id).all()]
            session.query(UserFact).filter(UserFact.user_id == user_id).delete(synchronize_session=False)
            session.query(SessionInstruction).filter(SessionInstruction.user_id == user_id).delete(
                synchronize_session=False
            )
            session.query(CommandNote).filter(CommandNote.user_id == user_id).delete(synchronize_session=False)
            if "canvas_items" in inspect(session.get_bind()).get_table_names():
                for sid in owned_ids:
                    session.execute(text("DELETE FROM canvas_items WHERE session_id = :sid"), {"sid": sid})
            roots = (
                session.query(ChatSession)
                .filter(ChatSession.user_id == user_id, ChatSession.parent_session_id.is_(None))
                .all()
            )
            for chat in roots:
                self._delete_session_tree(session, chat)
            for chat in session.query(ChatSession).filter(ChatSession.user_id == user_id).all():
                session.delete(chat)
            session.query(ApiKey).filter(ApiKey.user_id == user_id).delete(synchronize_session=False)
            session.query(AuthSession).filter(AuthSession.user_id == user_id).delete(synchronize_session=False)
            session.query(Connection).filter(Connection.user_id == user_id).delete(synchronize_session=False)
            scheduled_ids = [
                row[0] for row in session.query(ScheduledJob.id).filter(ScheduledJob.user_id == user_id).all()
            ]
            if scheduled_ids:
                session.query(ScheduledJobRun).filter(ScheduledJobRun.job_id.in_(scheduled_ids)).delete(
                    synchronize_session=False
                )
                session.query(ScheduledJob).filter(ScheduledJob.user_id == user_id).delete(synchronize_session=False)
            monitor_ids = [row[0] for row in session.query(MonitorJob.id).filter(MonitorJob.user_id == user_id).all()]
            if monitor_ids:
                session.query(MonitorCheck).filter(MonitorCheck.job_id.in_(monitor_ids)).delete(
                    synchronize_session=False
                )
                session.query(MonitorSnapshot).filter(MonitorSnapshot.job_id.in_(monitor_ids)).delete(
                    synchronize_session=False
                )
                session.query(MonitorJob).filter(MonitorJob.user_id == user_id).delete(synchronize_session=False)
            session.delete(user)
            session.commit()
            return owned_ids

    def create_api_key(self, user_id: str, name: str) -> tuple[ApiKey, str]:
        """Insert a key for ``user_id``. Returns the row and the secret (once)."""
        secret, prefix, digest = new_api_key()
        with self.SessionLocal() as session:
            row = ApiKey(
                id=str(uuid.uuid4()),
                user_id=user_id,
                name=(name or "").strip()[:128],
                prefix=prefix,
                key_hash=digest,
                created_at=datetime.now(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row, secret

    def list_api_keys(self, user_id: str) -> list[ApiKey]:
        with self.SessionLocal() as session:
            rows = (
                session.query(ApiKey)
                .filter(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
                .order_by(ApiKey.created_at.desc())
                .all()
            )
            session.expunge_all()
            return rows

    def revoke_api_key(self, key_id: str, *, actor_id: str, as_admin: bool) -> bool:
        with self.SessionLocal() as session:
            row = session.get(ApiKey, key_id)
            if row is None or row.revoked_at is not None:
                return False
            if row.user_id != actor_id and not as_admin:
                return False
            row.revoked_at = datetime.now()
            session.commit()
            return True

    def user_id_for_api_key(self, raw: str) -> str | None:
        """Resolve a presented user key. Config keys are not stored here."""
        if not raw:
            return None
        digest = hash_secret(raw)
        now = datetime.now()
        with self.SessionLocal() as session:
            row = session.query(ApiKey).filter(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None)).first()
            if row is None:
                return None
            if row.last_used_at is None or (now - row.last_used_at) > timedelta(minutes=5):
                row.last_used_at = now
                session.commit()
            return row.user_id

    def create_auth_session(self, user_id: str, *, impersonator_user_id: str | None = None) -> str:
        """Persist a WebUI session and return the cookie secret.

        ``impersonator_user_id`` is the admin whose browser session was
        replaced. A normal login leaves it empty.
        """
        raw, digest = new_session_token()
        with self.SessionLocal() as session:
            session.add(
                AuthSession(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    impersonator_user_id=impersonator_user_id,
                    token_hash=digest,
                    created_at=datetime.now(),
                    expires_at=datetime.now() + timedelta(days=30),
                )
            )
            session.commit()
            return raw

    def get_live_auth_session(self, raw: str) -> AuthSession | None:
        """The session for a cookie value, or None when it is missing or expired."""
        if not raw:
            return None
        digest = hash_secret(raw)
        now = datetime.now()
        with self.SessionLocal() as session:
            row = (
                session.query(AuthSession)
                .filter(AuthSession.token_hash == digest, AuthSession.revoked_at.is_(None))
                .first()
            )
            if row is None or row.expires_at <= now:
                return None
            session.expunge(row)
            return row

    def user_id_for_session_token(self, raw: str) -> str | None:
        row = self.get_live_auth_session(raw)
        return row.user_id if row else None

    def revoke_session_token(self, raw: str) -> None:
        if not raw:
            return
        digest = hash_secret(raw)
        with self.SessionLocal() as session:
            row = session.query(AuthSession).filter(AuthSession.token_hash == digest).first()
            if row is None or row.revoked_at is not None:
                return
            row.revoked_at = datetime.now()
            session.commit()

    # -- Sessions --------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        title: str = "New chat",
        provider_override: str | None = None,
        source: str = "web",
        parent_session_id: str | None = None,
    ) -> ChatSession:
        """Create a new chat session.

        ``provider_override`` is stamped on the row at creation time and is
        considered write-once — :meth:`update_session` ignores attempts to
        change it. Use ``None`` to defer to the global default provider.

        ``source`` marks the originating client ("web" for the Agent Chat UI;
        external apps send their own). Used to filter external sessions out of
        the human sidebar. Also write-once.

        ``parent_session_id`` ties this call to a root task session. Write-once.
        The parent must already exist and must itself be a root. One level only.
        """
        session_id = (session_id or "").strip()
        if not session_id or len(session_id) > 36:
            raise ValueError("session id is empty or too long")
        parent_session_id = (parent_session_id or "").strip() or None
        if parent_session_id and len(parent_session_id) > 36:
            raise ValueError("parent session id is too long")
        if parent_session_id == session_id:
            raise ValueError("session cannot be its own parent")
        title = (title or "New chat").strip() or "New chat"
        if len(title) > 255:
            title = title[:255]

        owner = self._owner_id()
        with self.SessionLocal() as session:
            if parent_session_id:
                parent_query = session.query(ChatSession).filter_by(id=parent_session_id)
                actor = self._actor()
                if actor:
                    parent_query = parent_query.filter(ChatSession.user_id == actor)
                parent = parent_query.first()
                if parent is None:
                    raise ValueError("parent session does not exist")
                if parent.parent_session_id:
                    raise ValueError("parent session is already a child")
                if parent.user_id != owner:
                    raise ValueError("parent session does not exist")
            chat = ChatSession(
                id=session_id,
                user_id=owner,
                title=title,
                provider_override=(provider_override.strip().lower() or None) if provider_override else None,
                source=(source.strip().lower() or "web") if source else "web",
                parent_session_id=parent_session_id,
            )
            session.add(chat)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("session already exists") from exc
            session.refresh(chat)
            session.expunge(chat)
            return chat

    def get_session(self, session_id: str) -> ChatSession | None:
        """Get a session by ID, or None if not found."""
        with self.SessionLocal() as session:
            query = session.query(ChatSession).filter_by(id=session_id)
            actor = self._actor()
            if actor:
                query = query.filter(ChatSession.user_id == actor)
            chat = query.first()
            if chat:
                session.expunge(chat)
            return chat

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        sources: tuple[str, ...] | None = ("web",),
        parent_id: str | None = None,
    ) -> list[ChatSession]:
        """List sessions ordered by most recently created (stable ordering).

        ``sources`` filters by originating client; defaults to ("web",) so
        external-app sessions stay out of the human sidebar. Pass ``None`` to
        list every source.

        Without ``parent_id``, only root sessions are returned. Pass a parent
        id to list the calls that belong to that task.
        """
        parent_id = (parent_id or "").strip() or None
        with self.SessionLocal() as session:
            query = session.query(ChatSession)
            actor = self._actor()
            if actor:
                query = query.filter(ChatSession.user_id == actor)
            if sources is not None:
                query = query.filter(ChatSession.source.in_(sources))
            if parent_id:
                query = query.filter(ChatSession.parent_session_id == parent_id)
            else:
                query = query.filter(ChatSession.parent_session_id.is_(None))
            chats = query.order_by(ChatSession.created_at.desc()).offset(offset).limit(limit).all()
            session.expunge_all()
            return chats

    def search_message_content(self, query: str, limit: int = 50) -> list[str]:
        """Return distinct session_ids whose message content matches ``query``."""
        q = (query or "").strip()
        if not q:
            return []
        with self.SessionLocal() as session:
            rows_query = session.query(ChatMessage.session_id).join(
                ChatSession, ChatMessage.session_id == ChatSession.id
            )
            actor = self._actor()
            if actor:
                rows_query = rows_query.filter(ChatSession.user_id == actor)
            rows = rows_query.filter(ChatMessage.content.ilike(f"%{q}%")).distinct().limit(limit).all()
            return [r[0] for r in rows]

    def update_session(self, session_id: str, **fields) -> ChatSession | None:
        """Update session fields (title, profile, model, message_count).

        ``provider_override``, ``source``, and ``parent_session_id`` are
        write-once at session creation and are silently dropped here so callers
        passing **session.to_dict() back can't clobber them.
        """
        for write_once in ("provider_override", "source", "parent_session_id"):
            if write_once in fields:
                logger.debug(
                    "update_session: dropping %s (write-once at create_session)",
                    write_once,
                )
                fields.pop(write_once, None)
        with self.SessionLocal() as session:
            query = session.query(ChatSession).filter_by(id=session_id)
            actor = self._actor()
            if actor:
                query = query.filter(ChatSession.user_id == actor)
            chat = query.first()
            if not chat:
                return None
            for key, value in fields.items():
                if hasattr(chat, key):
                    setattr(chat, key, value)
            chat.updated_at = datetime.now()
            session.commit()
            session.refresh(chat)
            session.expunge(chat)
            return chat

    def add_token_usage(
        self,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Atomically increment cumulative token counters for a session."""
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return
            chat = session.query(ChatSession).filter_by(id=session_id).first()
            if not chat:
                return
            chat.prompt_tokens = (chat.prompt_tokens or 0) + prompt_tokens
            chat.completion_tokens = (chat.completion_tokens or 0) + completion_tokens
            chat.total_tokens = (chat.total_tokens or 0) + prompt_tokens + completion_tokens
            chat.updated_at = datetime.now()
            session.commit()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session, its messages, and any child sessions."""
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return False
            chat = session.query(ChatSession).filter_by(id=session_id).first()
            if not chat:
                return False
            self._delete_session_tree(session, chat)
            session.commit()
            return True

    def _delete_session_tree(self, session, chat: ChatSession) -> None:
        children = session.query(ChatSession).filter(
            ChatSession.parent_session_id == chat.id,
            ChatSession.user_id == chat.user_id,
        )
        for child in children:
            self._delete_session_tree(session, child)
        session.delete(chat)

    def delete_user_chat_data(self) -> dict[str, int]:
        """Delete chat sessions, facts, instructions, and canvas items.

        Messages go with their session. Connections, bookmarks, command
        permissions, scheduled jobs, and monitor jobs stay. Bookmark rows
        lose their session id via ON DELETE SET NULL.
        """
        with self.SessionLocal() as session:
            actor = self._actor()
            sessions_query = session.query(ChatSession)
            facts_query = session.query(UserFact)
            instructions_query = session.query(SessionInstruction)
            if actor:
                sessions_query = sessions_query.filter(ChatSession.user_id == actor)
                facts_query = facts_query.filter(UserFact.user_id == actor)
                instructions_query = instructions_query.filter(SessionInstruction.user_id == actor)
            owned_ids = [row[0] for row in sessions_query.with_entities(ChatSession.id).all()]
            sessions = len(owned_ids)
            facts = facts_query.delete(synchronize_session=False)
            instructions = instructions_query.delete(synchronize_session=False)
            canvas_items = 0
            if "canvas_items" in inspect(session.get_bind()).get_table_names():
                if actor:
                    for sid in owned_ids:
                        canvas_items += (
                            session.execute(
                                text("SELECT COUNT(*) FROM canvas_items WHERE session_id = :sid"),
                                {"sid": sid},
                            ).scalar()
                            or 0
                        )
                        session.execute(
                            text("DELETE FROM canvas_items WHERE session_id = :sid"),
                            {"sid": sid},
                        )
                else:
                    canvas_items = session.execute(text("SELECT COUNT(*) FROM canvas_items")).scalar() or 0
                    session.execute(text("DELETE FROM canvas_items"))
            roots = sessions_query.filter(ChatSession.parent_session_id.is_(None)).all()
            for chat in roots:
                self._delete_session_tree(session, chat)
            leftovers = session.query(ChatSession)
            if actor:
                leftovers = leftovers.filter(ChatSession.user_id == actor)
            for chat in leftovers.all():
                session.delete(chat)
            session.commit()
        return {
            "sessions": int(sessions),
            "facts": int(facts),
            "instructions": int(instructions),
            "canvas_items": int(canvas_items),
        }

    # -- Messages --------------------------------------------------------------

    def add_message(
        self,
        session_id: str,
        role: str,
        msg_type: str,
        content: str | None = None,
        metadata: dict | None = None,
        tool_calls: list[dict] | None = None,
        is_incognito: bool = False,
        is_volatile: bool = False,
    ) -> ChatMessage:
        """Add a message to a session. Auto-increments sequence and message_count.

        Content is automatically scanned for secrets and redacted before
        storage when the secret-redaction feature is enabled.
        """
        # --- Secret redaction (before persistence) ---
        if content:
            try:
                from agentforge.secret_redactor import get_redactor

                result = get_redactor().redact(content)
                content = result.text
            except Exception:
                pass  # graceful fallback — never block persistence

        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                raise ValueError("session not found")
            # Get next sequence number
            max_seq = session.query(func.max(ChatMessage.sequence)).filter_by(session_id=session_id).scalar()
            next_seq = (max_seq or 0) + 1

            msg = ChatMessage(
                session_id=session_id,
                role=role,
                type=msg_type,
                content=content,
                metadata_json=json.dumps(metadata) if metadata else None,
                tool_calls_json=json.dumps(tool_calls) if tool_calls else None,
                sequence=next_seq,
                is_incognito=is_incognito,
                is_volatile=is_volatile,
            )
            session.add(msg)

            # Update session message count and timestamp
            chat = session.query(ChatSession).filter_by(id=session_id).first()
            if chat:
                chat.message_count = next_seq
                chat.updated_at = datetime.now()

            session.commit()
            session.refresh(msg)
            session.expunge(msg)
            return msg

    def get_messages(self, session_id: str) -> list[ChatMessage]:
        """Get all messages for a session, ordered by sequence."""
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return []
            messages = (
                session.query(ChatMessage).filter_by(session_id=session_id).order_by(ChatMessage.sequence.asc()).all()
            )
            session.expunge_all()
            return messages

    def get_messages_page(
        self,
        session_id: str,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> tuple[list[ChatMessage], bool]:
        """Get a page of messages for a session, newest first.

        Returns ``(messages, has_more)`` where *messages* are in ascending
        sequence order (oldest → newest within the page) and *has_more* is
        ``True`` when older messages exist beyond this page.
        """
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return [], False
            q = session.query(ChatMessage).filter_by(session_id=session_id)
            if before_sequence is not None:
                q = q.filter(ChatMessage.sequence < before_sequence)

            # Fetch limit+1 to detect whether more exist
            rows = q.order_by(ChatMessage.sequence.desc()).limit(limit + 1).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            page.reverse()  # restore ascending order
            session.expunge_all()
            return page, has_more

    def get_messages_after(self, session_id: str, after_sequence: int) -> list[ChatMessage]:
        """Get messages with sequence > after_sequence, ordered by sequence.

        Used by the WebSocket poll loop to stream new messages that were
        persisted by the worker while the client was disconnected.
        """
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return []
            messages = (
                session.query(ChatMessage)
                .filter_by(session_id=session_id)
                .filter(ChatMessage.sequence > after_sequence)
                .order_by(ChatMessage.sequence.asc())
                .all()
            )
            session.expunge_all()
            return messages

    def get_messages_around(
        self,
        session_id: str,
        epoch_ms: int,
        window: int = 25,
    ) -> tuple[list[ChatMessage], bool, bool]:
        """Return *window* messages centred on the message nearest to *epoch_ms*.

        Uses ``created_at`` as a proxy for the client-side ``_ts`` timestamp.
        Returns ``(messages, has_more_before, has_more_after)`` where the two
        booleans indicate whether older / newer messages exist outside the window.
        """
        target_dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        half = window // 2

        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return [], False, False
            # Find the message whose created_at is closest to (and >= ) target_dt
            anchor = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.created_at >= target_dt,
                )
                .order_by(ChatMessage.created_at.asc())
                .first()
            )
            # Fallback: target is after all messages → use the last one
            if anchor is None:
                anchor = (
                    session.query(ChatMessage)
                    .filter_by(session_id=session_id)
                    .order_by(ChatMessage.sequence.desc())
                    .first()
                )
            if anchor is None:
                return [], False, False

            anchor_seq = anchor.sequence
            lo = max(0, anchor_seq - half)
            hi = anchor_seq + half

            rows = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.sequence >= lo,
                    ChatMessage.sequence <= hi,
                )
                .order_by(ChatMessage.sequence.asc())
                .all()
            )

            has_more_before = (
                session.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id, ChatMessage.sequence < lo)
                .limit(1)
                .count()
            ) > 0

            has_more_after = (
                session.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id, ChatMessage.sequence > hi)
                .limit(1)
                .count()
            ) > 0

            session.expunge_all()
            return rows, has_more_before, has_more_after

    def get_max_sequence(self, session_id: str) -> int:
        """Return the highest sequence number for a session, or 0 if empty."""
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return 0
            result = session.query(func.max(ChatMessage.sequence)).filter_by(session_id=session_id).scalar()
            return result or 0

    def update_message_metadata(
        self,
        session_id: str,
        msg_type: str,
        metadata_patch: dict,
    ) -> bool:
        """Merge *metadata_patch* into the metadata_json of the most recent
        message with the given *msg_type* in the session.

        Used to persist accumulated progress state (e.g., research plan
        progress) back to the original message so it survives page reload.
        Returns ``True`` if a message was found and updated.
        """
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return False
            msg = (
                session.query(ChatMessage)
                .filter_by(session_id=session_id, type=msg_type)
                .order_by(ChatMessage.sequence.desc())
                .first()
            )
            if not msg:
                return False
            existing = json.loads(msg.metadata_json) if msg.metadata_json else {}
            existing.update(metadata_patch)
            msg.metadata_json = json.dumps(existing)
            session.commit()
            return True

    def delete_messages(self, session_id: str) -> int:
        """Delete all messages for a session. Returns number deleted."""
        with self.SessionLocal() as session:
            count = session.query(ChatMessage).filter_by(session_id=session_id).delete()
            # Reset the message_count on the session row
            sess = session.query(ChatSession).get(session_id)
            if sess:
                sess.message_count = 0
            session.commit()
            return count

    def get_last_user_query(self, session_id: str) -> ChatMessage | None:
        """Return the highest-sequence user query row for a session, or None."""
        with self.SessionLocal() as session:
            msg = (
                session.query(ChatMessage)
                .filter_by(session_id=session_id, role="user", type="query")
                .order_by(ChatMessage.sequence.desc())
                .first()
            )
            if msg is None:
                return None
            session.expunge(msg)
            return msg

    def delete_messages_from_sequence(self, session_id: str, sequence: int) -> int:
        """Delete every message at or after *sequence* for this session.

        Updates the parent session's message_count in the same transaction.
        Returns the number of rows removed.
        """
        with self.SessionLocal() as session:
            count = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.sequence >= sequence,
                )
                .delete(synchronize_session=False)
            )
            remaining = (session.query(func.max(ChatMessage.sequence)).filter_by(session_id=session_id).scalar()) or 0
            chat = session.query(ChatSession).filter_by(id=session_id).first()
            if chat:
                chat.message_count = remaining
            session.commit()
            return count

    def get_command_policy_override(self, tool: str) -> dict | None:
        """Return the runtime override for *tool*, or None if not set."""
        with self.SessionLocal() as session:
            row = session.query(CommandPolicyOverride).filter_by(tool=tool).first()
            if not row:
                return None
            session.expunge(row)
            return row.to_dict()

    def upsert_command_policy_override(self, tool: str, data: dict) -> dict:
        """Insert or update a runtime command policy override for *tool*."""
        with self.SessionLocal() as session:
            row = session.query(CommandPolicyOverride).filter_by(tool=tool).first()
            if row:
                row.mode = data.get("mode", row.mode)
                row.allowed_commands = data.get("allowed_commands", [])
                row.allowed_patterns = data.get("allowed_patterns", [])
                row.blocked_patterns = data.get("blocked_patterns", [])
                row.updated_at = datetime.now()
            else:
                row = CommandPolicyOverride(
                    tool=tool,
                    mode=data.get("mode", "confirm"),
                    allowed_commands=data.get("allowed_commands", []),
                    allowed_patterns=data.get("allowed_patterns", []),
                    blocked_patterns=data.get("blocked_patterns", []),
                )
                session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row.to_dict()

    def delete_command_policy_override(self, tool: str | None = None) -> int:
        """Delete override(s). Pass *tool* to delete one row, or None for all."""
        with self.SessionLocal() as session:
            q = session.query(CommandPolicyOverride)
            if tool is not None:
                q = q.filter_by(tool=tool)
            count = q.delete(synchronize_session=False)
            session.commit()
            return int(count)

    # -- Named permission profiles ---------------------------------------------

    def list_command_permission_profiles(self) -> list[dict]:
        with self.SessionLocal() as session:
            rows = session.query(CommandPermissionProfileRow).order_by(CommandPermissionProfileRow.id).all()
            out = []
            for row in rows:
                session.expunge(row)
                out.append(row.to_dict())
            return out

    def upsert_command_permission_profile(self, profile_id: str, data: dict) -> dict:
        with self.SessionLocal() as session:
            row = session.query(CommandPermissionProfileRow).filter_by(id=profile_id).first()
            if row:
                row.description = data.get("description", row.description) or ""
                row.shell = data.get("shell")
                row.ssh = data.get("ssh")
                row.updated_at = datetime.now()
            else:
                row = CommandPermissionProfileRow(
                    id=profile_id,
                    description=data.get("description") or "",
                    shell=data.get("shell"),
                    ssh=data.get("ssh"),
                )
                session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row.to_dict()

    def delete_command_permission_profile(self, profile_id: str) -> int:
        with self.SessionLocal() as session:
            count = (
                session.query(CommandPermissionProfileRow).filter_by(id=profile_id).delete(synchronize_session=False)
            )
            session.commit()
            return int(count)

    def get_active_command_permission_profile(self) -> str | None:
        with self.SessionLocal() as session:
            row = session.query(CommandPermissionActive).filter_by(id=1).first()
            if not row:
                return None
            return row.profile_id

    def set_active_command_permission_profile(self, profile_id: str | None) -> None:
        with self.SessionLocal() as session:
            row = session.query(CommandPermissionActive).filter_by(id=1).first()
            if row:
                row.profile_id = profile_id
                row.updated_at = datetime.now()
            else:
                session.add(CommandPermissionActive(id=1, profile_id=profile_id))
            session.commit()

    # -- Command Notes ---------------------------------------------------------

    def create_command_note(
        self,
        title: str,
        commands: list[dict],
        session_id: str | None = None,
        message_ts: str | None = None,
        kind: str = "tool_calls",
        content: str | None = None,
    ) -> CommandNote:
        """Save a new command note (tool calls) or answer bookmark."""
        with self.SessionLocal() as session:
            if session_id and not self._visible(session, session_id):
                raise ValueError("session not found")
            note = CommandNote(
                user_id=self._owner_id(),
                session_id=session_id,
                title=title,
                kind=kind,
                commands_json=json.dumps(commands),
                content=content,
                message_ts=message_ts,
            )
            session.add(note)
            session.commit()
            session.refresh(note)
            session.expunge(note)
            return note

    def list_command_notes(self, limit: int = 200, offset: int = 0) -> list[CommandNote]:
        """List all command notes, newest first."""
        with self.SessionLocal() as session:
            query = session.query(CommandNote)
            actor = self._actor()
            if actor:
                query = query.filter(CommandNote.user_id == actor)
            notes = query.order_by(CommandNote.created_at.desc()).offset(offset).limit(limit).all()
            session.expunge_all()
            return notes

    def get_session_command_notes(self, session_id: str) -> list[CommandNote]:
        """Get all command notes for a specific session."""
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return []
            query = session.query(CommandNote).filter_by(session_id=session_id)
            actor = self._actor()
            if actor:
                query = query.filter(CommandNote.user_id == actor)
            notes = query.order_by(CommandNote.created_at.asc()).all()
            session.expunge_all()
            return notes

    def delete_command_note(self, note_id: int) -> bool:
        """Delete a command note by ID. Returns True if deleted."""
        with self.SessionLocal() as session:
            query = session.query(CommandNote).filter_by(id=note_id)
            actor = self._actor()
            if actor:
                query = query.filter(CommandNote.user_id == actor)
            note = query.first()
            if not note:
                return False
            session.delete(note)
            session.commit()
            return True

    # -- Session Instructions --------------------------------------------------

    def add_session_instruction(self, text: str, session_id: str | None = None) -> SessionInstruction:
        """Persist a user-authored instruction.

        ``session_id=None`` creates a global instruction that applies to every
        session.  Pass a session_id for session-scoped instructions.
        """
        with self.SessionLocal() as session:
            if session_id and not self._visible(session, session_id):
                raise ValueError("session not found")
            instr = SessionInstruction(user_id=self._owner_id(), session_id=session_id, text=text.strip())
            session.add(instr)
            session.commit()
            session.refresh(instr)
            session.expunge(instr)
            return instr

    def get_session_instructions(self, session_id: str) -> list[SessionInstruction]:
        """Return all instructions active for *session_id* — session-scoped + global."""
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return []
            query = session.query(SessionInstruction).filter(
                (SessionInstruction.session_id == session_id) | (SessionInstruction.session_id.is_(None))
            )
            actor = self._actor()
            if actor:
                query = query.filter(SessionInstruction.user_id == actor)
            instrs = query.order_by(SessionInstruction.created_at.asc()).all()
            session.expunge_all()
            return instrs

    def get_global_instructions(self) -> list[SessionInstruction]:
        """Return global instructions (session_id IS NULL)."""
        with self.SessionLocal() as session:
            query = session.query(SessionInstruction).filter(SessionInstruction.session_id.is_(None))
            actor = self._actor()
            if actor:
                query = query.filter(SessionInstruction.user_id == actor)
            instrs = query.order_by(SessionInstruction.created_at.asc()).all()
            session.expunge_all()
            return instrs

    def delete_session_instruction(self, instruction_id: int) -> bool:
        """Delete a single instruction by ID. Returns True if deleted."""
        with self.SessionLocal() as session:
            query = session.query(SessionInstruction).filter_by(id=instruction_id)
            actor = self._actor()
            if actor:
                query = query.filter(SessionInstruction.user_id == actor)
            instr = query.first()
            if not instr:
                return False
            session.delete(instr)
            session.commit()
            return True

    def clear_session_instructions(self, session_id: str, *, global_too: bool = False) -> int:
        """Delete all instructions for a session (and optionally global ones).

        Returns the count of deleted rows.
        """
        with self.SessionLocal() as session:
            if not self._visible(session, session_id):
                return 0
            q = session.query(SessionInstruction).filter(SessionInstruction.session_id == session_id)
            if global_too:
                q = session.query(SessionInstruction).filter(
                    (SessionInstruction.session_id == session_id) | (SessionInstruction.session_id.is_(None))
                )
            actor = self._actor()
            if actor:
                q = q.filter(SessionInstruction.user_id == actor)
            count = q.count()
            q.delete(synchronize_session=False)
            session.commit()
            return count

    # -- Scheduled Jobs --------------------------------------------------------

    def create_scheduled_job(
        self,
        job_id: str,
        label: str,
        command: str,
        cron: str,
        cron_human: str | None = None,
        on_failure: str = "notify",
        enabled: bool = True,
    ) -> ScheduledJob:
        """Create a new scheduled job."""
        with self.SessionLocal() as session:
            job = ScheduledJob(
                id=job_id,
                user_id=self._owner_id(),
                label=label,
                command=command,
                cron=cron,
                cron_human=cron_human,
                on_failure=on_failure,
                enabled=enabled,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def get_scheduled_job(self, job_id: str) -> ScheduledJob | None:
        """Get a scheduled job by ID. Hidden when it belongs to someone else."""
        with self.SessionLocal() as session:
            job = self._scoped(session.query(ScheduledJob).filter_by(id=job_id), ScheduledJob.user_id).first()
            if job:
                session.expunge(job)
            return job

    def list_scheduled_jobs(self, enabled_only: bool = False) -> list[ScheduledJob]:
        """List scheduled jobs the current actor can see, newest first.

        No actor (auth off, or the scheduler thread) returns every job.
        """
        with self.SessionLocal() as session:
            q = self._scoped(session.query(ScheduledJob), ScheduledJob.user_id)
            if enabled_only:
                q = q.filter_by(enabled=True)
            jobs = q.order_by(ScheduledJob.created_at.desc()).all()
            session.expunge_all()
            return jobs

    def update_scheduled_job(self, job_id: str, **fields) -> ScheduledJob | None:
        """Update scheduled job fields (label, cron, enabled, etc.)."""
        fields.pop("user_id", None)
        with self.SessionLocal() as session:
            job = self._scoped(session.query(ScheduledJob).filter_by(id=job_id), ScheduledJob.user_id).first()
            if not job:
                return None
            for key, value in fields.items():
                if hasattr(job, key) and key != "user_id":
                    setattr(job, key, value)
            job.updated_at = datetime.now()
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def delete_scheduled_job(self, job_id: str) -> bool:
        """Delete a scheduled job and all its run records (cascade)."""
        with self.SessionLocal() as session:
            job = self._scoped(session.query(ScheduledJob).filter_by(id=job_id), ScheduledJob.user_id).first()
            if not job:
                return False
            session.delete(job)
            session.commit()
            return True

    def add_scheduled_job_run(
        self,
        job_id: str,
        status: str = "running",
    ) -> ScheduledJobRun:
        """Record a new run for a scheduled job."""
        with self.SessionLocal() as session:
            run = ScheduledJobRun(job_id=job_id, status=status)
            session.add(run)
            session.commit()
            session.refresh(run)
            session.expunge(run)
            return run

    def complete_scheduled_job_run(
        self,
        run_id: int,
        status: str,
        exit_code: int | None = None,
        output: str | None = None,
        error: str | None = None,
        duration_s: float | None = None,
    ) -> ScheduledJobRun | None:
        """Mark a job run as completed (success or error) and update the parent job."""
        with self.SessionLocal() as session:
            run = session.query(ScheduledJobRun).filter_by(id=run_id).first()
            if not run:
                return None
            run.status = status
            run.exit_code = exit_code
            run.output = output[:50000] if output else None  # cap at 50k chars
            run.error = error[:10000] if error else None
            run.duration_s = duration_s
            run.finished_at = datetime.now()

            # Update parent job's last_run fields
            job = session.query(ScheduledJob).filter_by(id=run.job_id).first()
            if job:
                job.last_run_at = run.finished_at
                job.last_status = status

            session.commit()
            session.refresh(run)
            session.expunge(run)
            return run

    def get_scheduled_job_runs(
        self,
        job_id: str,
        limit: int = 20,
    ) -> list[ScheduledJobRun]:
        """Get recent runs for a scheduled job, newest first."""
        with self.SessionLocal() as session:
            runs = (
                session.query(ScheduledJobRun)
                .filter_by(job_id=job_id)
                .order_by(ScheduledJobRun.started_at.desc())
                .limit(limit)
                .all()
            )
            session.expunge_all()
            return runs

    # -- User Facts --------------------------------------------------

    def upsert_fact(
        self,
        fact_type: str,
        key: str,
        value: str,
        source_session: str | None = None,
        confidence: float = 0.8,
    ) -> UserFact:
        """Insert or update a user fact (deduplicated by user and key)."""
        owner = self._owner_id()
        with self.SessionLocal() as session:
            query = session.query(UserFact).filter_by(key=key)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            else:
                query = query.filter(UserFact.user_id == owner)
            existing = query.first()
            if existing:
                existing.value = value
                existing.fact_type = fact_type
                existing.confidence = max(existing.confidence, confidence)
                existing.source_session = source_session or existing.source_session
                existing.updated_at = datetime.now()
                session.commit()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                fact = UserFact(
                    user_id=owner,
                    fact_type=fact_type,
                    key=key,
                    value=value,
                    source_session=source_session,
                    confidence=confidence,
                )
                session.add(fact)
                session.commit()
                session.refresh(fact)
                session.expunge(fact)
                return fact

    def get_all_facts(self, min_confidence: float = 0.0) -> list[UserFact]:
        """Get all user facts above a confidence threshold."""
        with self.SessionLocal() as session:
            query = session.query(UserFact).filter(UserFact.confidence >= min_confidence)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            facts = query.order_by(UserFact.updated_at.desc()).all()
            session.expunge_all()
            return facts

    def get_facts_by_type(self, fact_type: str) -> list[UserFact]:
        """Get all facts of a specific type."""
        with self.SessionLocal() as session:
            query = session.query(UserFact).filter_by(fact_type=fact_type)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            facts = query.order_by(UserFact.updated_at.desc()).all()
            session.expunge_all()
            return facts

    def delete_fact(self, key: str) -> bool:
        """Delete a fact by key."""
        with self.SessionLocal() as session:
            query = session.query(UserFact).filter_by(key=key)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            fact = query.first()
            if not fact:
                return False
            session.delete(fact)
            session.commit()
            return True

    def delete_all_facts(self) -> int:
        """Delete every row in ``user_facts``. Returns rows deleted."""
        with self.SessionLocal() as session:
            query = session.query(UserFact)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            count = query.delete()
            session.commit()
            return int(count)

    def delete_old_facts(self, days: int) -> int:
        """Delete facts whose ``updated_at`` is older than *days*.

        Used by the nightly memory-prune task. Returns rows deleted.
        """
        if days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=days)
        with self.SessionLocal() as session:
            query = session.query(UserFact).filter(UserFact.updated_at < cutoff)
            actor = self._actor()
            if actor:
                query = query.filter(UserFact.user_id == actor)
            count = query.delete(synchronize_session=False)
            session.commit()
            return int(count)

    # -- Statistics ------------------------------------------------------------

    def get_statistics(self) -> dict:
        """Return basic stats about the chat database."""
        with self.SessionLocal() as session:
            sessions_query = session.query(ChatSession)
            messages_query = session.query(ChatMessage)
            actor = self._actor()
            if actor:
                sessions_query = sessions_query.filter(ChatSession.user_id == actor)
                messages_query = messages_query.join(ChatSession, ChatMessage.session_id == ChatSession.id).filter(
                    ChatSession.user_id == actor
                )
            total_sessions = sessions_query.count()
            total_messages = messages_query.count()

            latest = sessions_query.order_by(ChatSession.updated_at.desc()).first()
            last_activity = latest.updated_at if latest else None

            return {
                "total_sessions": total_sessions,
                "total_messages": total_messages,
                "last_activity": last_activity,
            }

    # -- Monitor Jobs ----------------------------------------------------------

    def create_monitor_job(
        self,
        job_id: str,
        label: str,
        url: str,
        original_prompt: str | None = None,
        extraction_mode: str = "text",
        css_selector: str | None = None,
        structured_selectors: dict | None = None,
        cron: str = "",
        cron_human: str | None = None,
        notification_method: str = "terminal-notifier",
        webhook_url: str | None = None,
        enabled: bool = True,
    ) -> MonitorJob:
        """Create a new website monitor job."""
        with self.SessionLocal() as session:
            job = MonitorJob(
                id=job_id,
                user_id=self._owner_id(),
                label=label,
                url=url,
                original_prompt=original_prompt,
                extraction_mode=extraction_mode,
                css_selector=css_selector,
                structured_selectors=structured_selectors,
                cron=cron,
                cron_human=cron_human,
                notification_method=notification_method,
                webhook_url=webhook_url,
                enabled=enabled,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def get_monitor_job(self, job_id: str) -> MonitorJob | None:
        """Get a monitor job by ID. Hidden when it belongs to someone else."""
        with self.SessionLocal() as session:
            job = self._scoped(session.query(MonitorJob).filter_by(id=job_id), MonitorJob.user_id).first()
            if job:
                session.expunge(job)
            return job

    def list_monitor_jobs(self, enabled_only: bool = False) -> list[MonitorJob]:
        """List monitor jobs the current actor can see, newest first.

        No actor (auth off, or the scheduler thread) returns every job.
        """
        with self.SessionLocal() as session:
            q = self._scoped(session.query(MonitorJob), MonitorJob.user_id)
            if enabled_only:
                q = q.filter_by(enabled=True)
            jobs = q.order_by(MonitorJob.created_at.desc()).all()
            session.expunge_all()
            return jobs

    def update_monitor_job(self, job_id: str, **fields) -> MonitorJob | None:
        """Update monitor job fields."""
        fields.pop("user_id", None)
        with self.SessionLocal() as session:
            job = self._scoped(session.query(MonitorJob).filter_by(id=job_id), MonitorJob.user_id).first()
            if not job:
                return None
            for key, value in fields.items():
                if hasattr(job, key) and key != "user_id":
                    setattr(job, key, value)
            job.updated_at = datetime.now()
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def get_monitor_screenshot_paths(self, job_id: str) -> list[str]:
        """Return all non-null screenshot paths for a monitor job's checks.

        Call this *before* ``delete_monitor_job()`` so the caller can remove
        the files from disk after the DB rows are gone.
        """
        with self.SessionLocal() as session:
            rows = (
                session.query(MonitorCheck.screenshot_path)
                .filter(
                    MonitorCheck.job_id == job_id,
                    MonitorCheck.screenshot_path.isnot(None),
                )
                .all()
            )
            return [r[0] for r in rows if r[0]]

    def delete_monitor_job(self, job_id: str) -> bool:
        """Delete a monitor job and all its snapshots/checks.

        Explicitly deletes child rows first for robustness — the DB-level
        ``ON DELETE CASCADE`` only works on databases created after the FK
        was added, and SQLite doesn't enforce FKs unless the pragma is set.
        """
        with self.SessionLocal() as session:
            job = self._scoped(session.query(MonitorJob).filter_by(id=job_id), MonitorJob.user_id).first()
            if not job:
                return False
            # Explicit child cleanup (safe even if CASCADE is active)
            session.query(MonitorCheck).filter_by(job_id=job_id).delete()
            session.query(MonitorSnapshot).filter_by(job_id=job_id).delete()
            session.delete(job)
            session.commit()
            return True

    # -- Monitor Snapshots -----------------------------------------------------

    def create_monitor_snapshot(
        self,
        job_id: str,
        content: str,
        content_hash: str,
        extraction_mode: str,
        css_selector_used: str | None = None,
        word_count: int | None = None,
        structured_content: dict | None = None,
    ) -> MonitorSnapshot:
        """Store a new content snapshot for a monitor job."""
        with self.SessionLocal() as session:
            snap = MonitorSnapshot(
                job_id=job_id,
                content=content[:1_000_000],  # cap at 1MB
                content_hash=content_hash,
                extraction_mode=extraction_mode,
                css_selector_used=css_selector_used,
                word_count=word_count,
                structured_content=structured_content,
            )
            session.add(snap)
            session.commit()
            session.refresh(snap)
            session.expunge(snap)
            return snap

    def get_latest_snapshot(self, job_id: str) -> MonitorSnapshot | None:
        """Get the most recent snapshot for a monitor job."""
        with self.SessionLocal() as session:
            snap = (
                session.query(MonitorSnapshot)
                .filter_by(job_id=job_id)
                .order_by(MonitorSnapshot.created_at.desc())
                .first()
            )
            if snap:
                session.expunge(snap)
            return snap

    def get_snapshot_content(self, snapshot_id: int) -> str | None:
        """Get the full content of a specific snapshot."""
        with self.SessionLocal() as session:
            snap = session.query(MonitorSnapshot).filter_by(id=snapshot_id).first()
            if snap:
                return snap.content
            return None

    def delete_old_snapshots(self, job_id: str, keep_count: int = 10) -> int:
        """Prune old snapshots, keeping the N most recent."""
        with self.SessionLocal() as session:
            all_snaps = (
                session.query(MonitorSnapshot)
                .filter_by(job_id=job_id)
                .order_by(MonitorSnapshot.created_at.desc())
                .all()
            )
            to_delete = all_snaps[keep_count:]
            for snap in to_delete:
                session.delete(snap)
            session.commit()
            return len(to_delete)

    # -- Monitor Checks --------------------------------------------------------

    def add_monitor_check(
        self,
        job_id: str,
        status: str = "running",
    ) -> MonitorCheck:
        """Record a new check execution for a monitor job."""
        with self.SessionLocal() as session:
            check = MonitorCheck(job_id=job_id, status=status)
            session.add(check)
            session.commit()
            session.refresh(check)
            session.expunge(check)
            return check

    def complete_monitor_check(
        self,
        check_id: int,
        status: str,
        prev_hash: str | None = None,
        current_hash: str | None = None,
        diff_summary: str | None = None,
        diff_lines_added: int | None = None,
        diff_lines_removed: int | None = None,
        structured_diff: dict | None = None,
        error: str | None = None,
        duration_s: float | None = None,
        screenshot_path: str | None = None,
    ) -> MonitorCheck | None:
        """Mark a monitor check as completed and update the parent job."""
        with self.SessionLocal() as session:
            check = session.query(MonitorCheck).filter_by(id=check_id).first()
            if not check:
                return None
            check.status = status
            check.prev_hash = prev_hash
            check.current_hash = current_hash
            check.diff_summary = diff_summary[:10_000] if diff_summary else None
            check.diff_lines_added = diff_lines_added
            check.diff_lines_removed = diff_lines_removed
            check.structured_diff = structured_diff
            check.error = error[:10_000] if error else None
            check.duration_s = duration_s
            check.screenshot_path = screenshot_path
            check.finished_at = datetime.now()

            # Update parent job's last check fields
            job = session.query(MonitorJob).filter_by(id=check.job_id).first()
            if job:
                job.last_check_at = check.finished_at
                job.last_status = status

            session.commit()
            session.refresh(check)
            session.expunge(check)
            return check

    def get_monitor_checks(
        self,
        job_id: str,
        limit: int = 20,
    ) -> list[MonitorCheck]:
        """Get recent checks for a monitor job, newest first."""
        with self.SessionLocal() as session:
            checks = (
                session.query(MonitorCheck)
                .filter_by(job_id=job_id)
                .order_by(MonitorCheck.started_at.desc())
                .limit(limit)
                .all()
            )
            session.expunge_all()
            return checks
