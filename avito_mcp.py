#!/usr/bin/env python3
"""
MCP Server for Avito Messenger & Items API.

Provides tools for managing Avito chats (messenger), items (listings),
and user profile. Designed for service center workflow integration.

Base URL: https://api.avito.ru
Auth: OAuth2 (client_credentials)
Token lifetime: 24 hours
Rate limit: ~5 req/s (conservative)
"""

import json
import logging
import os
import sys
import asyncio
import time
from typing import Optional, List, Dict, Any

import httpx
import ssl as _ssl
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("avito-mcp")

# ── Constants ──────────────────────────────────────────────────────────────
API_BASE_URL = "https://api.avito.ru"

# SSL: Avito requires TLS but some Macs have proxy/antivirus intercepting certs.
# Set AVITO_SSL_VERIFY=false to disable verification if needed.
_ssl_verify = os.environ.get("AVITO_SSL_VERIFY", "true").lower() != "false"
if not _ssl_verify:
    log.warning("⚠️ SSL verification DISABLED (AVITO_SSL_VERIFY=false). Use only for proxy/antivirus bypass!")
    _ssl_ctx = _ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = _ssl.CERT_NONE
else:
    _ssl_ctx = None  # use default
# ── Multi-account support ─────────────────────────────────────────────────
# Account 1 (primary): AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_USER_ID, AVITO_ACCOUNT_NAME
# Account 2 (optional): AVITO_CLIENT_ID_2, AVITO_CLIENT_SECRET_2, AVITO_USER_ID_2, AVITO_ACCOUNT_NAME_2

_accounts: Dict[str, Dict[str, str]] = {}

_a1_id = os.environ.get("AVITO_CLIENT_ID", "")
_a1_secret = os.environ.get("AVITO_CLIENT_SECRET", "")
_a1_user = os.environ.get("AVITO_USER_ID", "")
_a1_name = os.environ.get("AVITO_ACCOUNT_NAME", "main")

if _a1_id and _a1_secret and _a1_user:
    _accounts[_a1_name] = {"client_id": _a1_id, "client_secret": _a1_secret, "user_id": _a1_user}
else:
    log.fatal("AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_USER_ID must be set")
    sys.exit(1)

_a2_id = os.environ.get("AVITO_CLIENT_ID_2", "")
_a2_secret = os.environ.get("AVITO_CLIENT_SECRET_2", "")
_a2_user = os.environ.get("AVITO_USER_ID_2", "")
_a2_name = os.environ.get("AVITO_ACCOUNT_NAME_2", "second")

if _a2_id and _a2_secret and _a2_user:
    _accounts[_a2_name] = {"client_id": _a2_id, "client_secret": _a2_secret, "user_id": _a2_user}
    log.info(f"Multi-account: [{_a1_name}] + [{_a2_name}]")

ACCOUNT_NAMES = list(_accounts.keys())
DEFAULT_ACCOUNT = ACCOUNT_NAMES[0]

# Legacy compat
AVITO_USER_ID = _accounts[DEFAULT_ACCOUNT]["user_id"]

# Auto-detect user_id for accounts that don't have it set
_accounts_needing_uid: List[str] = [
    name for name, cfg in _accounts.items() if cfg["user_id"] == "auto"
]

DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_DELAY = 0.22  # ~5 req/s safety margin

# ── Server ─────────────────────────────────────────────────────────────────
SERVER_INSTRUCTIONS = """
# Avito MCP — Messenger & Items (Multi-Account)

Tools for managing Avito chats and listings. Designed for service centers.
Supports multiple Avito accounts — read tools query ALL accounts by default.

## Quick Navigation

**Чаты/Messenger:**
- Список чатов → avito_chats (фильтры: unread_only, item_ids, account)
- Сообщения в чате → avito_chat_messages (chat_id, account)
- Отправить сообщение → avito_send_message (chat_id, text, account — REQUIRED)
- Прочитать чат → avito_read_chat (chat_id, account — REQUIRED)

**Товары/Items (объявления):**
- Мои объявления → avito_items (status, limit, account)
- Детали объявления → avito_item_info (item_id, account)

**Профиль:**
- Информация → avito_profile (account)
- Список аккаунтов → avito_accounts

**Webhooks:**
- Подписка → avito_subscribe_webhook (url, account)
- Отписка → avito_unsubscribe_webhook (account)

## Multi-Account
- Read tools (chats, items, profile) query ALL accounts and merge results.
- Write tools (send_message, read_chat, blacklist) require `account` parameter.
- Available accounts: """ + ", ".join(ACCOUNT_NAMES) + """

## Tips
- Token обновляется автоматически (24ч lifetime).
- Avito возвращает 403 (не 401) при истёкшем токене.
- chat_id — строка, не число.
- Сообщения возвращаются от новых к старым.
"""

mcp = FastMCP("avito_mcp", instructions=SERVER_INSTRUCTIONS)

# ── Smart Cache ───────────────────────────────────────────────────────────
_cache: Dict[str, tuple] = {}
_CACHE_TTL = {
    "items": 600,       # 10 min for listings (change rarely)
    "profile": 3600,    # 1 hour for profile
    "chats": 60,        # 1 min for chats (update frequently)
    "messages": 30,     # 30s for messages
    "default": 120,     # 2 min for everything else
}


def _cache_key(endpoint: str, params: Optional[Dict] = None, account: str = "") -> str:
    p = json.dumps(params, sort_keys=True, default=str) if params else ""
    return f"{account}|{endpoint}|{p}"


def _cache_ttl(endpoint: str) -> int:
    for key, ttl in _CACHE_TTL.items():
        if key in endpoint:
            return ttl
    return _CACHE_TTL["default"]


def _cache_get(key: str) -> Optional[dict]:
    if key in _cache:
        data, ts, ttl = _cache[key]
        if time.time() - ts < ttl:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: dict, endpoint: str):
    _cache[key] = (data, time.time(), _cache_ttl(endpoint))


def _cache_invalidate(pattern: str = ""):
    if not pattern:
        _cache.clear()
        return
    for k in [k for k in _cache if pattern in k]:
        del _cache[k]


# ── Rate limiter (async-safe via Lock) ────────────────────────────────────
_rate_lock = asyncio.Lock()


async def _rate_limit():
    async with _rate_lock:
        await asyncio.sleep(RATE_LIMIT_DELAY)


# ── OAuth2 Token Manager (multi-account) ──────────────────────────────────
_token_cache: Dict[str, Dict[str, Any]] = {}


def _resolve_account(account: Optional[str] = None) -> str:
    """Resolve account name, default to first account."""
    if not account or account not in _accounts:
        return DEFAULT_ACCOUNT
    return account


async def _get_token(account: Optional[str] = None) -> str:
    """Get valid OAuth2 token for specified account, refreshing if needed."""
    acct = _resolve_account(account)
    now = time.time()
    cached = _token_cache.get(acct, {})
    if cached.get("token") and cached.get("expires_at", 0) > now + 300:
        return cached["token"]

    creds = _accounts[acct]
    c = _get_http_client()
    r = await c.post(
        f"{API_BASE_URL}/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    r.raise_for_status()
    data = r.json()

    _token_cache[acct] = {
        "token": data["access_token"],
        "expires_at": now + data.get("expires_in", 86400),
    }
    return data["access_token"]


def _get_user_id(account: Optional[str] = None) -> str:
    """Get user_id for specified account."""
    return _accounts[_resolve_account(account)]["user_id"]


async def _auto_detect_user_ids():
    """Auto-detect user_id for accounts configured with user_id='auto'."""
    for name in _accounts_needing_uid:
        try:
            token = await _get_token(name)
            c = _get_http_client()
            r = await c.get(
                f"{API_BASE_URL}/core/v1/accounts/self",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
            uid = str(data.get("id", ""))
            if uid:
                _accounts[name]["user_id"] = uid
                log.info(f"Auto-detected user_id for [{name}]: {uid} ({data.get('name', '')})")
            else:
                log.warning(f"Could not auto-detect user_id for [{name}]")
        except Exception as e:
            log.warning(f"Auto-detect failed for [{name}]: {e}")


# ── Shared API client (reusable, with retry) ─────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None
MAX_RETRIES = 3
RETRY_BACKOFF = [0.5, 1.5, 4.0]  # exponential backoff delays


def _get_http_client() -> httpx.AsyncClient:
    """Get or create a reusable httpx client (saves TLS handshake per request)."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            proxy=None, trust_env=False,
            verify=_ssl_ctx if _ssl_ctx else True,
            timeout=DEFAULT_TIMEOUT,
        )
    return _http_client


async def _api(
    endpoint: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    account: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """Make authenticated API request to Avito with retry, cache, and token refresh."""
    acct = _resolve_account(account)

    # Check cache for GET requests
    if method == "GET" and use_cache:
        key = _cache_key(endpoint, params, acct)
        cached = _cache_get(key)
        if cached is not None:
            log.debug(f"Cache HIT: {endpoint}")
            return cached

    token = await _get_token(acct)
    await _rate_limit()

    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    c = _get_http_client()
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            r = await c.request(
                method,
                f"{API_BASE_URL}{endpoint}",
                headers=headers,
                params=params,
                json=body,
            )
            # Avito returns 403 instead of 401 on expired token
            if r.status_code == 403 and attempt == 0:
                _token_cache.pop(acct, None)
                token = await _get_token(acct)
                headers["Authorization"] = f"Bearer {token}"
                continue

            # Retry on 429 (rate limit) and 5xx (server errors)
            if r.status_code == 429 or r.status_code >= 500:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(f"Retry {attempt+1}/{MAX_RETRIES}: HTTP {r.status_code} on {endpoint}, waiting {delay}s")
                await asyncio.sleep(delay)
                continue

            r.raise_for_status()
            if not r.content or not r.content.strip():
                return {"success": True, "status_code": r.status_code}
            try:
                data = r.json()
            except (json.JSONDecodeError, ValueError):
                return {"raw_response": r.text[:500], "status_code": r.status_code}

            # Cache GET responses
            if method == "GET" and use_cache:
                _cache_set(key, data, endpoint)
            # Invalidate on writes
            if method in ("POST", "PUT", "DELETE"):
                _cache_invalidate(acct)  # clear this account's cache
            return data

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(f"Retry {attempt+1}/{MAX_RETRIES}: {type(e).__name__} on {endpoint}, waiting {delay}s")
                await asyncio.sleep(delay)
            continue

    if last_error:
        raise last_error
    r.raise_for_status()
    return {}


def _err(e: Exception) -> str:
    """Format error message."""
    if isinstance(e, httpx.HTTPStatusError):
        s = e.response.status_code
        try:
            b = e.response.json()
        except Exception:
            b = e.response.text[:500]
        msgs = {
            400: f"Bad request: {b}",
            401: "Auth failed. Check AVITO_CLIENT_ID/SECRET.",
            403: "Access denied or token expired.",
            404: "Not found.",
            429: "Rate limit. Retry in a few seconds.",
        }
        return f"Error {s}: {msgs.get(s, f'API error: {b}')}"
    if isinstance(e, httpx.TimeoutException):
        return f"Error: Timeout {DEFAULT_TIMEOUT}s."
    return f"Error: {type(e).__name__}: {e}"


def _json(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _all_accounts(account: Optional[str] = None) -> List[str]:
    """Return list of account names to query. If 'all' or None → all accounts."""
    if not account or account == "all":
        return list(ACCOUNT_NAMES)
    return [_resolve_account(account)]


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNTS TOOL
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="avito_accounts",
    annotations={
        "title": "List Connected Avito Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def avito_accounts() -> str:
    """List all connected Avito accounts and their user IDs.

    Use this to see which accounts are available and their names.
    Account names are used in the `account` parameter of other tools.

    Returns:
        JSON with accounts list. Each account has 'account' (name), 'user_id', 'is_default' fields.
    """
    result = []
    for name in ACCOUNT_NAMES:
        result.append({
            "account": name,
            "user_id": _accounts[name]["user_id"],
            "is_default": name == DEFAULT_ACCOUNT,
        })
    return _json({"accounts": result, "total": len(result)})


# ═══════════════════════════════════════════════════════════════════════════
#  MESSENGER TOOLS
# ═══════════════════════════════════════════════════════════════════════════


class ChatsInput(BaseModel):
    """Input for listing chats."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    unread_only: Optional[bool] = Field(
        default=None,
        description="Filter only unread chats (true/false)"
    )
    item_ids: Optional[str] = Field(
        default=None,
        description="Comma-separated item IDs to filter chats by specific listings"
    )
    limit: Optional[int] = Field(
        default=20,
        description="Max chats to return per account",
        ge=1, le=100
    )
    offset: Optional[int] = Field(
        default=0,
        description="Pagination offset",
        ge=0
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name to query. Omit or 'all' to query ALL accounts."
    )


@mcp.tool(
    name="avito_chats",
    annotations={
        "title": "List Avito Chats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_chats(params: ChatsInput) -> str:
    """List messenger chats with buyers. Queries ALL accounts by default.

    Returns list of chats with buyer names, last message preview, item context,
    and unread status. Each chat is tagged with its account name.

    Args:
        params: ChatsInput with optional filters (unread_only, item_ids, limit, offset, account)

    Returns:
        JSON list of chats with chat_id, user_id, item info, last_message, updated_at.
    """
    try:
        qp: Dict[str, Any] = {"limit": params.limit, "offset": params.offset}
        if params.unread_only is not None:
            qp["unread_only"] = str(params.unread_only).lower()
        if params.item_ids:
            qp["item_ids"] = params.item_ids

        result = []
        errors = []
        for acct in _all_accounts(params.account):
            uid = _get_user_id(acct)
            try:
                data = await _api(
                    f"/messenger/v2/accounts/{uid}/chats",
                    params=qp,
                    account=acct,
                )
                chats = data.get("chats") or data.get("result", {}).get("chats", [])
                for chat in chats:
                    chat_info = {
                        "account": acct,
                        "chat_id": chat.get("id"),
                        "buyer_name": (chat.get("users", [{}])[0].get("name")
                                       if chat.get("users") else
                                       chat.get("user", {}).get("name", "Unknown")),
                        "item_id": (chat.get("context", {}).get("value", {}).get("id")
                                    if chat.get("context") else None),
                        "item_title": (chat.get("context", {}).get("value", {}).get("title")
                                       if chat.get("context") else None),
                        "last_message": chat.get("last_message", {}).get("text", ""),
                        "unread_count": chat.get("unread_count", 0),
                        "updated": chat.get("updated"),
                    }
                    result.append(chat_info)
            except Exception as e:
                errors.append({"account": acct, "error": str(e)})

        if not result and not errors:
            return "No chats found with given filters."

        summary = {
            "total_chats": len(result),
            "unread_chats": sum(1 for c in result if c.get("unread_count", 0) > 0),
            "accounts_queried": [a for a in _all_accounts(params.account)],
            "chats": result,
        }
        if errors:
            summary["errors"] = errors
        return _json(summary)

    except Exception as e:
        return _err(e)


class ChatMessagesInput(BaseModel):
    """Input for reading messages in a chat."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    chat_id: str = Field(
        ..., description="Chat ID (string)", min_length=1
    )
    limit: Optional[int] = Field(
        default=20, description="Max messages to return", ge=1, le=100
    )
    offset: Optional[int] = Field(
        default=0, description="Pagination offset", ge=0
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name. Required if multiple accounts configured."
    )


@mcp.tool(
    name="avito_chat_messages",
    annotations={
        "title": "Get Chat Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_chat_messages(params: ChatMessagesInput) -> str:
    """Get messages from a specific chat. Messages are ordered newest-first.

    Returns message text, author info, timestamps. Use after avito_chats
    to read a specific conversation with a buyer.

    If multiple accounts configured and account not specified, tries each
    account until the chat is found.

    Args:
        params: ChatMessagesInput with chat_id, optional pagination and account

    Returns:
        JSON list of messages with id, author_id, text, created_at, type fields.
    """
    try:
        # If account specified, use it directly; otherwise try all accounts
        accounts_to_try = _all_accounts(params.account) if not params.account else [_resolve_account(params.account)]

        for acct in accounts_to_try:
            uid = _get_user_id(acct)
            try:
                data = await _api(
                    f"/messenger/v3/accounts/{uid}/chats/{params.chat_id}/messages/",
                    params={"limit": params.limit, "offset": params.offset},
                    account=acct,
                )
                messages = data.get("messages") or data.get("result", {}).get("messages", [])

                result = []
                for msg in messages:
                    msg_info = {
                        "id": msg.get("id"),
                        "text": (msg.get("content", {}).get("text")
                                 or msg.get("text")
                                 or msg.get("body", "")),
                        "author_id": str(msg.get("author_id", "")),
                        "is_mine": str(msg.get("author_id", "")) == uid,
                        "created": msg.get("created"),
                        "type": msg.get("type", "text"),
                    }
                    result.append(msg_info)

                return _json({
                    "account": acct,
                    "chat_id": params.chat_id,
                    "count": len(result),
                    "messages": result,
                })
            except Exception:
                continue  # Try next account

        return f"Chat {params.chat_id} not found in any account."

    except Exception as e:
        return _err(e)


class SendMessageInput(BaseModel):
    """Input for sending a message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    chat_id: str = Field(
        ..., description="Chat ID to send message to", min_length=1
    )
    text: str = Field(
        ..., description="Message text to send", min_length=1, max_length=4000
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name. If multiple accounts, specify which one to send from."
    )


@mcp.tool(
    name="avito_send_message",
    annotations={
        "title": "Send Message in Chat",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def avito_send_message(params: SendMessageInput) -> str:
    """Send a text message to a buyer in an Avito chat.

    Use this to reply to buyer inquiries about your listings.
    The message appears in the Avito messenger from your account.

    If multiple accounts are configured, specify `account` to choose which
    account sends the message. If omitted, uses the default account.

    Args:
        params: SendMessageInput with chat_id, text, and optional account

    Returns:
        JSON confirmation with message ID
    """
    try:
        acct = _resolve_account(params.account)
        uid = _get_user_id(acct)
        data = await _api(
            f"/messenger/v1/accounts/{uid}/chats/{params.chat_id}/messages",
            method="POST",
            body={
                "message": {"text": params.text},
                "type": "text",
            },
            account=acct,
        )
        return _json({
            "success": True,
            "account": acct,
            "chat_id": params.chat_id,
            "message_id": data.get("id") or data.get("result", {}).get("id"),
            "text_preview": params.text[:100],
        })
    except Exception as e:
        return _err(e)


class ReadChatInput(BaseModel):
    """Input for marking a chat as read."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    chat_id: str = Field(
        ..., description="Chat ID to mark as read", min_length=1
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name. If multiple accounts, specify which one."
    )


@mcp.tool(
    name="avito_read_chat",
    annotations={
        "title": "Mark Chat as Read",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_read_chat(params: ReadChatInput) -> str:
    """Mark all messages in a chat as read.

    Use after reading messages to clear unread counter.

    Args:
        params: ReadChatInput with chat_id and optional account

    Returns:
        JSON confirmation
    """
    try:
        acct = _resolve_account(params.account)
        uid = _get_user_id(acct)
        data = await _api(
            f"/messenger/v1/accounts/{uid}/chats/{params.chat_id}/read",
            method="POST",
            account=acct,
        )
        return _json({"success": True, "account": acct, "chat_id": params.chat_id})
    except Exception as e:
        return _err(e)


# ═══════════════════════════════════════════════════════════════════════════
#  ITEMS TOOLS
# ═══════════════════════════════════════════════════════════════════════════


class ItemsInput(BaseModel):
    """Input for listing items/listings."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: Optional[str] = Field(
        default=None,
        description="Filter by status: active, removed, old, blocked, rejected"
    )
    limit: Optional[int] = Field(
        default=25, description="Max items to return per account", ge=1, le=100
    )
    offset: Optional[int] = Field(
        default=0, description="Pagination offset", ge=0
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name to query. Omit or 'all' to query ALL accounts."
    )


@mcp.tool(
    name="avito_items",
    annotations={
        "title": "List My Items/Listings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_items(params: ItemsInput) -> str:
    """List your Avito items/listings with optional status filter. Queries ALL accounts by default.

    Returns item IDs, titles, prices, statuses, and URLs.
    Each item is tagged with its account name.

    Args:
        params: ItemsInput with optional status filter, pagination, and account

    Returns:
        JSON list of items with id, title, price, status, url fields.
    """
    try:
        qp: Dict[str, Any] = {
            "per_page": params.limit,
            "page": (params.offset // params.limit) + 1 if params.limit else 1,
        }
        if params.status:
            qp["status"] = params.status

        all_items = []
        errors = []
        for acct in _all_accounts(params.account):
            uid = _get_user_id(acct)
            try:
                # Try v2 first, fallback to v1
                try:
                    data = await _api(f"/core/v2/accounts/{uid}/items", params=qp, account=acct)
                except Exception:
                    data = await _api(f"/core/v1/accounts/{uid}/items", params=qp, account=acct)

                resources = data.get("resources") or data.get("result", {}).get("resources", [])
                for item in resources:
                    all_items.append({
                        "account": acct,
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "price": item.get("price"),
                        "status": item.get("status"),
                        "url": item.get("url"),
                        "category": item.get("category", {}).get("name") if item.get("category") else None,
                        "stats": item.get("stats"),
                    })
            except Exception as e:
                errors.append({"account": acct, "error": str(e)})

        result = {
            "total": len(all_items),
            "accounts_queried": [a for a in _all_accounts(params.account)],
            "items": all_items,
        }
        if errors:
            result["errors"] = errors
        return _json(result)

    except Exception as e:
        return _err(e)


class ItemInfoInput(BaseModel):
    """Input for getting item details."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    item_id: str = Field(
        ..., description="Avito item/listing ID", min_length=1
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name. If omitted, tries all accounts."
    )


@mcp.tool(
    name="avito_item_info",
    annotations={
        "title": "Get Item/Listing Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_item_info(params: ItemInfoInput) -> str:
    """Get detailed information about a specific item/listing.

    Returns title, description, price, status, images, stats.
    If account not specified, tries each account until found.

    Args:
        params: ItemInfoInput with item_id and optional account

    Returns:
        JSON with full item details including id, title, price, description, status, stats.
    """
    try:
        accounts_to_try = _all_accounts(params.account) if not params.account else [_resolve_account(params.account)]
        for acct in accounts_to_try:
            uid = _get_user_id(acct)
            try:
                try:
                    data = await _api(f"/core/v2/accounts/{uid}/items/{params.item_id}", account=acct)
                except Exception:
                    data = await _api(f"/core/v1/accounts/{uid}/items/{params.item_id}/", account=acct)
                data["_account"] = acct
                return _json(data)
            except Exception:
                continue
        return f"Item {params.item_id} not found in any account."
    except Exception as e:
        return _err(e)


# ═══════════════════════════════════════════════════════════════════════════
#  PROFILE TOOL
# ═══════════════════════════════════════════════════════════════════════════


class ProfileInput(BaseModel):
    """Input for profile query."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    account: Optional[str] = Field(
        default=None,
        description="Account name. Omit or 'all' to get profiles for ALL accounts."
    )


@mcp.tool(
    name="avito_profile",
    annotations={
        "title": "Get My Avito Profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_profile(params: ProfileInput) -> str:
    """Get Avito account profile information. Queries ALL accounts by default.

    Returns account name, email, phone, profile URL for each account.
    Useful for verifying which accounts are connected.

    Args:
        params: ProfileInput with optional account

    Returns:
        JSON with user profile: id, name, phone, email, location.
    """
    try:
        profiles = []
        for acct in _all_accounts(params.account):
            try:
                data = await _api(f"/core/v1/accounts/self", account=acct)
                data["_account"] = acct
                profiles.append(data)
            except Exception as e:
                profiles.append({"_account": acct, "error": str(e)})
        if len(profiles) == 1:
            return _json(profiles[0])
        return _json({"profiles": profiles, "total": len(profiles)})
    except Exception as e:
        return _err(e)


# ═══════════════════════════════════════════════════════════════════════════
#  WEBHOOK TOOLS
# ═══════════════════════════════════════════════════════════════════════════


class WebhookInput(BaseModel):
    """Input for webhook subscription."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(
        ...,
        description="Webhook URL to receive Avito notifications (must be HTTPS)",
        min_length=10,
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name. Omit or 'all' to subscribe ALL accounts."
    )


@mcp.tool(
    name="avito_subscribe_webhook",
    annotations={
        "title": "Subscribe to Avito Webhook",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_subscribe_webhook(params: WebhookInput) -> str:
    """Subscribe to Avito messenger webhook notifications.

    When subscribed, Avito will POST to your URL when new messages arrive.
    Subscribes ALL accounts by default.

    Args:
        params: WebhookInput with target HTTPS URL and optional account

    Returns:
        JSON confirmation of subscription
    """
    try:
        results = []
        for acct in _all_accounts(params.account):
            try:
                data = await _api(
                    f"/messenger/v3/webhook",
                    method="POST",
                    body={"url": params.url},
                    account=acct,
                )
                results.append({"account": acct, "success": True, "response": data})
            except Exception as e:
                results.append({"account": acct, "success": False, "error": str(e)})
        return _json({"webhook_url": params.url, "results": results})
    except Exception as e:
        return _err(e)


class UnsubscribeWebhookInput(BaseModel):
    """Input for webhook unsubscription."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    account: Optional[str] = Field(
        default=None,
        description="Account name. Omit or 'all' to unsubscribe ALL accounts."
    )


@mcp.tool(
    name="avito_unsubscribe_webhook",
    annotations={
        "title": "Unsubscribe from Avito Webhook",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_unsubscribe_webhook(params: UnsubscribeWebhookInput) -> str:
    """Unsubscribe from Avito messenger webhook notifications.

    Stops Avito from sending webhook notifications. Unsubscribes ALL accounts by default.

    Args:
        params: UnsubscribeWebhookInput with optional account

    Returns:
        JSON confirmation
    """
    try:
        results = []
        for acct in _all_accounts(params.account):
            try:
                data = await _api(
                    f"/messenger/v3/webhook/unsubscribe",
                    method="POST",
                    account=acct,
                )
                results.append({"account": acct, "success": True, "response": data})
            except Exception as e:
                results.append({"account": acct, "success": False, "error": str(e)})
        return _json({"results": results})
    except Exception as e:
        return _err(e)


# ═══════════════════════════════════════════════════════════════════════════
#  BLACKLIST TOOL
# ═══════════════════════════════════════════════════════════════════════════


class BlacklistInput(BaseModel):
    """Input for blacklisting a user."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: str = Field(
        ..., description="Avito user ID to blacklist", min_length=1
    )
    account: Optional[str] = Field(
        default=None,
        description="Account name to blacklist from. Uses default if omitted."
    )


@mcp.tool(
    name="avito_blacklist_user",
    annotations={
        "title": "Add User to Blacklist",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def avito_blacklist_user(params: BlacklistInput) -> str:
    """Add a user to your blacklist. They won't be able to message you.

    Use for spam or abusive buyers. This action can be reversed in Avito settings.

    Args:
        params: BlacklistInput with user_id and optional account

    Returns:
        JSON confirmation
    """
    try:
        acct = _resolve_account(params.account)
        uid = _get_user_id(acct)
        data = await _api(
            f"/messenger/v1/accounts/{uid}/blacklist",
            method="POST",
            body={"user_id": int(params.user_id)},
            account=acct,
        )
        return _json({"success": True, "account": acct, "blacklisted_user_id": params.user_id})
    except Exception as e:
        return _err(e)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Auto-detect user_ids for accounts with user_id="auto"
    if _accounts_needing_uid:
        asyncio.run(_auto_detect_user_ids())
        # Update ACCOUNT_NAMES in case any were removed
        ACCOUNT_NAMES = [n for n in _accounts if _accounts[n]["user_id"] != "auto"]
    mcp.run()
