# Chat channels

Talk to your agents from a chat app, and get their answers back. Telegram is
the first channel; Discord comes next.

## Telegram

1. In Telegram, talk to **@BotFather**, `/newbot`, and copy the token.
2. In ConductLoop, **Credentials → Channels → Telegram bot token**: paste it.
   It is kept in the keychain (or the encrypted vault when there is no
   keychain). It is never written to the database, `config.json` or a log,
   and no API answer contains it.
3. Turn the channel on: `POST /api/channels/telegram/enable`. The token is
   checked with Telegram (`getMe`) first; a rejected token leaves it off.
4. Create a pairing code: `POST /api/channels/telegram/pairing`. Send
   `/pair <code>` to your bot within 10 minutes. The code works once, closes
   after 5 wrong guesses, and only its hash is stored.
5. In the chat: `/agents`, `/use <id>`, then write. The answer of the agent
   comes back in the chat (long answers are split; very long ones are cut).
   `POST /api/channels/telegram/default-agent {"agent_id": n}` sets who
   answers when a chat has not picked anyone.

| Route | What it does |
|---|---|
| `GET /api/channels` | status: on/off, where the token is, bot name, paired chats, last error |
| `POST /api/channels/telegram/enable` / `disable` | on / off (off also closes an open code) |
| `POST /api/channels/telegram/pairing` | a new one-time code |
| `POST /api/channels/telegram/default-agent` | the default agent (`null` for none) |
| `DELETE /api/channels/telegram/peers/<id>` | unpair a chat |

### How it behaves

- **Off by default.** While off, no call is made to Telegram.
- **Local only.** Messages are fetched by long polling (`getUpdates`) from
  this machine. No webhook, no open port, nothing to expose.
- **Private chats you paired, nobody else.** An unpaired chat gets one fixed
  sentence (at most every 10 minutes) and never reaches an agent, so it
  costs nothing. Groups and channels are ignored.
- **One answer per message.** The update offset is saved before the message
  is handled, so a crash or a restart never answers (and pays for) the same
  message twice.
- **The agent wake is the usual one** (`scheduler._wake_agent`): it has a
  deadline (`OPS_AGENT_WAKE_TIMEOUT`) and is logged in the agent's log.
- **Errors are explained.** A rejected token or a webhook still set on the
  bot (Telegram refuses polling then) shows in `last_error` and the poller
  waits a minute before trying again.
- `OPS_DISABLE_CHANNELS=1` keeps every channel stopped.

The older Boardroom bridge (`telegram_bridge.py`, `TELEGRAM_BOT_TOKEN`,
webhook) is separate and unchanged. Do not point both at the same bot.
