#!/usr/bin/env python3
"""Quick test of Avito API credentials — run on Mac."""
import urllib.request
import urllib.parse
import json
import ssl
import sys
import socket
import os

CLIENT_ID = os.environ.get("AVITO_CLIENT_ID", "your_client_id")
CLIENT_SECRET = os.environ.get("AVITO_CLIENT_SECRET", "your_client_secret")

print("=" * 50)
print("🔑 Тест Avito API credentials")
print("=" * 50)

# 0. Network diagnostics
print("\n0️⃣  Диагностика сети...")
try:
    ip = socket.getaddrinfo("api.avito.ru", 443)[0][4][0]
    print(f"   DNS OK: api.avito.ru → {ip}")
except Exception as e:
    print(f"   ❌ DNS ошибка: {e}")
    print("   Проверь интернет и VPN!")
    sys.exit(1)

try:
    sock = socket.create_connection(("api.avito.ru", 443), timeout=10)
    sock.close()
    print("   TCP OK: порт 443 доступен")
except Exception as e:
    print(f"   ❌ TCP ошибка: {e}")
    print("   Файрвол или VPN блокирует api.avito.ru:443")
    sys.exit(1)

# SSL context — попробуем с обычным и без верификации
ctx = ssl.create_default_context()
ctx_noverify = ssl.create_default_context()
ctx_noverify.check_hostname = False
ctx_noverify.verify_mode = ssl.CERT_NONE

def api_request(url, data=None, headers=None, method=None):
    """Make request, trying normal SSL first, then unverified."""
    if method is None:
        method = "POST" if data else "GET"
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    for label, context in [("SSL normal", ctx), ("SSL no-verify", ctx_noverify)]:
        try:
            with urllib.request.urlopen(req, timeout=30, context=context) as resp:
                return json.loads(resp.read())
        except ssl.SSLError as e:
            print(f"   ⚠️  {label} failed: {e}")
            continue
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            print(f"   HTTP {e.code}: {body}")
            return None
        except Exception as e:
            print(f"   ⚠️  {label} failed: {e}")
            continue
    return None

# 1. Get token
print("\n1️⃣  Получаю OAuth token...")
token_body = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}).encode()

token_data = api_request(
    "https://api.avito.ru/token/",
    data=token_body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)

# Fallback: try without trailing slash
if not token_data or "access_token" not in token_data:
    print("   Пробую альтернативный URL...")
    token_data = api_request(
        "https://api.avito.ru/token",
        data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

if not token_data or "access_token" not in token_data:
    print(f"   ❌ Не удалось получить токен: {token_data}")
    sys.exit(1)

token = token_data["access_token"]
expires = token_data.get("expires_in", 0)
print(f"   ✅ Token получен! Expires in {expires}s")
print(f"   Token: {token[:20]}...")

auth = {"Authorization": f"Bearer {token}"}

# 2. Get profile
print("\n2️⃣  Получаю профиль...")
profile = api_request("https://api.avito.ru/core/v1/accounts/self", headers=auth)
if profile:
    user_id = profile.get("id", "?")
    name = profile.get("name", "?")
    email = profile.get("email", "?")
    phone = profile.get("phone", "?")
    print(f"   ✅ Профиль: {name}")
    print(f"   User ID: {user_id}")
    print(f"   Email: {email}")
    print(f"   Phone: {phone}")
else:
    print("   ❌ Профиль недоступен")
    user_id = "unknown"

# 3. Get chats
print("\n3️⃣  Проверяю чаты...")
chats_data = api_request(
    f"https://api.avito.ru/messenger/v2/accounts/{user_id}/chats?limit=5",
    headers=auth,
)
if chats_data:
    chats = chats_data.get("chats", chats_data.get("result", {}).get("chats", []))
    unread_total = sum(c.get("unread_count", 0) for c in chats)
    print(f"   ✅ Чатов: {len(chats)}, непрочитанных сообщений: {unread_total}")
    for i, chat in enumerate(chats[:3]):
        buyer = "?"
        if chat.get("users"):
            buyer = chat["users"][0].get("name", "?")
        elif chat.get("user"):
            buyer = chat["user"].get("name", "?")
        last_msg = str(chat.get("last_message", {}).get("text", ""))[:50]
        unread = chat.get("unread_count", 0)
        print(f"   Chat {i+1}: {buyer} | unread: {unread} | «{last_msg}»")
else:
    print("   ❌ Чаты недоступны (проверь scope messenger:read)")

# 4. Get items
print("\n4️⃣  Проверяю объявления...")
items_data = api_request(
    f"https://api.avito.ru/core/v1/accounts/{user_id}/items?per_page=5&status=active",
    headers=auth,
)
if items_data:
    resources = items_data.get("resources", [])
    print(f"   ✅ Активных объявлений: {len(resources)}")
    for i, item in enumerate(resources[:3]):
        title = item.get("title", "?")
        price = item.get("price", "?")
        print(f"   Item {i+1}: {title} — {price} ₽")
else:
    print("   ❌ Объявления недоступны (проверь scope item:info)")

print("\n" + "=" * 50)
print("🏁 Тест завершён!")
print("=" * 50)
if user_id != "unknown":
    print(f"\n📋 Для настройки avito-mcp добавь в .env:")
    print(f"   AVITO_CLIENT_ID={CLIENT_ID}")
    print(f"   AVITO_CLIENT_SECRET={CLIENT_SECRET}")
    print(f"   AVITO_USER_ID={user_id}")
