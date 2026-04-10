# Avito MCP Server

MCP server for Avito Messenger & Items API. Multi-account support — query all accounts simultaneously.

## Features

- 11 tools: chats, messages, send, items, profile, webhooks, blacklist
- Multi-account: query both accounts at once, results merged with account tags
- Auto-detect user_id on startup (set `AVITO_USER_ID=auto`)
- OAuth2 token auto-refresh (24h lifetime)
- SSL bypass for corporate proxies (`AVITO_SSL_VERIFY=false`)
- Rate limiting (async-safe, ~5 req/s)

## Quick Start

```bash
pip3 install httpx pydantic mcp
```

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "avito-mcp": {
      "command": "python3",
      "args": ["/path/to/avito_mcp.py"],
      "env": {
        "AVITO_CLIENT_ID": "your_client_id",
        "AVITO_CLIENT_SECRET": "your_client_secret",
        "AVITO_USER_ID": "your_user_id",
        "AVITO_SSL_VERIFY": "false"
      }
    }
  }
}
```

Or run `bash deploy.sh` for interactive setup.

## Multi-Account

Add `_2` suffix env vars for second account:

```
AVITO_CLIENT_ID_2=second_client_id
AVITO_CLIENT_SECRET_2=second_client_secret
AVITO_USER_ID_2=auto
AVITO_ACCOUNT_NAME_2=second
```

## Tools

| Tool | Description | Multi-account |
|------|-------------|---------------|
| `avito_accounts` | List connected accounts | — |
| `avito_chats` | List chats (all accounts) | merge |
| `avito_chat_messages` | Read messages | auto-find |
| `avito_send_message` | Send message | specify account |
| `avito_read_chat` | Mark as read | specify account |
| `avito_items` | List items (all accounts) | merge |
| `avito_item_info` | Item details | auto-find |
| `avito_profile` | Account profile | merge |
| `avito_subscribe_webhook` | Subscribe webhook | all |
| `avito_unsubscribe_webhook` | Unsubscribe | all |
| `avito_blacklist_user` | Blacklist user | specify account |

## Credentials

Get OAuth2 credentials at [developers.avito.ru](https://developers.avito.ru/).
Required scopes: `messenger:read`, `messenger:write`, `item:info`.

## Note

Sending messages (avito_send_message) requires Avito Pro subscription with API messenger access.
Reading chats works without paid subscription.

## License

MIT
