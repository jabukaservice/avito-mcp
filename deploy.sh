#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Avito MCP — Deploy Script for Mac
# Копирует файлы, ставит зависимости, обновляет конфиг Claude Desktop
# ═══════════════════════════════════════════════════════════════════════════

set -e

DEST="${AVITO_MCP_DIR:-$HOME/avito-mcp}"
CONFIG_FILE="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "═══════════════════════════════════════════════════"
echo "🚀 Avito MCP — Установка"
echo "═══════════════════════════════════════════════════"

# 1. Создать директорию
echo ""
echo "1️⃣  Создаю директорию $DEST ..."
mkdir -p "$DEST"

# 2. Копировать файлы
echo "2️⃣  Копирую файлы..."
cp "$SCRIPT_DIR/avito_mcp.py" "$DEST/"
cp "$SCRIPT_DIR/requirements.txt" "$DEST/"
cp "$SCRIPT_DIR/.env.example" "$DEST/"
cp "$SCRIPT_DIR/.gitignore" "$DEST/"
echo "   ✅ Файлы скопированы"

# 3. Установить зависимости
echo "3️⃣  Устанавливаю Python зависимости..."
pip3 install --user --break-system-packages -q httpx pydantic mcp 2>/dev/null || \
pip3 install --user -q httpx pydantic mcp 2>/dev/null || \
pip3 install -q httpx pydantic mcp
echo "   ✅ Зависимости установлены"

# 4. Обновить конфиг Claude Desktop
echo "4️⃣  Обновляю конфиг Claude Desktop..."
if [ ! -f "$CONFIG_FILE" ]; then
    echo "   ⚠️  Конфиг не найден: $CONFIG_FILE"
    echo "   Создаю новый..."
    mkdir -p "$(dirname "$CONFIG_FILE")"
    echo '{"mcpServers":{}}' > "$CONFIG_FILE"
fi

# Используем python3 для безопасного JSON merge
DEST="$DEST" python3 << 'PYEOF'
import json
import os
import sys

dest = os.environ.get("DEST", os.path.expanduser("~/avito-mcp"))
config_file = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
print("   Введите Avito credentials:")

try:
    with open(config_file, "r") as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

if "mcpServers" not in config:
    config["mcpServers"] = {}

avito_id = input("   AVITO_CLIENT_ID: ").strip()
avito_secret = input("   AVITO_CLIENT_SECRET: ").strip()
avito_user = input("   AVITO_USER_ID: ").strip()
avito_name = input("   AVITO_ACCOUNT_NAME [main]: ").strip() or "main"
avito_ssl = input("   AVITO_SSL_VERIFY [false]: ").strip() or "false"

env_block = {
    "AVITO_CLIENT_ID": avito_id,
    "AVITO_CLIENT_SECRET": avito_secret,
    "AVITO_USER_ID": avito_user,
    "AVITO_ACCOUNT_NAME": avito_name,
    "AVITO_SSL_VERIFY": avito_ssl,
}

avito_id_2 = input("   AVITO_CLIENT_ID_2 (Enter to skip): ").strip()
if avito_id_2:
    env_block["AVITO_CLIENT_ID_2"] = avito_id_2
    env_block["AVITO_CLIENT_SECRET_2"] = input("   AVITO_CLIENT_SECRET_2: ").strip()
    env_block["AVITO_USER_ID_2"] = input("   AVITO_USER_ID_2 [auto]: ").strip() or "auto"
    env_block["AVITO_ACCOUNT_NAME_2"] = input("   AVITO_ACCOUNT_NAME_2 [second]: ").strip() or "second"

config["mcpServers"]["avito-mcp"] = {
    "command": "python3",
    "args": [dest + "/avito_mcp.py"],
    "env": env_block,
}

with open(config_file, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("   ✅ Конфиг обновлён")
PYEOF

# 5. Summary
echo ""
echo "═══════════════════════════════════════════════════"
echo "✅ Установка завершена!"
echo "═══════════════════════════════════════════════════"
echo ""
echo "📁 Файлы: $DEST"
echo "⚙️  Конфиг: $CONFIG_FILE"
echo ""
echo "🔄 Перезапусти Claude Desktop чтобы подключить MCP:"
echo "   Cmd+Q → открыть заново"
echo ""
echo "📋 Аккаунты Avito — настроены через интерактивный setup"
echo ""
echo "🧪 Проверка после перезапуска:"
echo "   Скажи Claude: 'avito_accounts' или 'покажи чаты авито'"
