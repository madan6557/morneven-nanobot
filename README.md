# Morneven Nanobot Runtime

`morneven_nanobot` is the Nanobot runtime service used by Morneven Bot Manager. It runs one or more Nanobot gateways, hosts a Basic Auth protected runtime dashboard, materializes active personality workspaces, and applies Morneven runtime safety patches for Telegram routing, topic locks, runtime sync, and process control.

The canonical Morneven documentation lives in the shared workspace `Document/` folder.

## Repository Role

`morneven_nanobot` is responsible for:

- Running Nanobot gateway processes in a container.
- Serving a Basic Auth protected dashboard.
- Persisting runtime files under `/data/.nanobot`.
- Pulling active Bot Manager runtime bundles from Morneven Backend.
- Materializing each active personality workspace.
- Running multi active personalities as separate runtime gateways.
- Starting, stopping, and restarting runtime gateways.
- Stopping old unmanaged gateway processes before new starts.
- Reporting gateway status, logs, workspace changes, config secret summaries, and Telegram topic observations.
- Applying runtime patches from `sitecustomize.py`.

Morneven Backend remains the source of truth for credentials, personalities, workspace files, memory files, channels, topic lock rules, and runtime settings when the Morneven integration is enabled.

## Related Repositories

| Repository | Relationship |
| --- | --- |
| `morneven-website` | Provides the Bot Manager UI used by PL7 Admin and PL7 Author. |
| `morneven-backend` | Stores Bot Manager data and sends runtime bundles to Nanobot. |
| `morneven_nanobot` | Executes active runtime personalities and gateways. |

## Current Capabilities

- Single active personality mode.
- Multi active personality mode with one main identity and multiple active runtime gateways.
- Per personality provider assignment with fallback to global provider.
- OpenRouter profile support from Bot Manager.
- Telegram multi bot routing for separate mentions and multi mention messages.
- Telegram forum topic send support through `message_thread_id`, `thread_id`, or `topic_id`.
- Telegram Topic Lock with observed group and topic registry.
- Inbound Topic Lock drop for blocked group topics.
- Outbound Topic Lock block for explicitly blocked topics.
- Primary outbound topic per personality per Telegram group.
- Default outbound command redirect to primary topic when main topic is not allowed.
- `message_thread_id` `0`, `1`, empty, and `main` are treated as the main or General topic.
- Optional scheduled Dream runtime control per personality.
- Runtime status, logs, workspace pull, secret summary pull, and Telegram topic refresh endpoints.

## Runtime Flow With Morneven Bot Manager

1. Operator configures Bot Manager in Morneven Website.
2. Website saves configuration to Morneven Backend.
3. Backend stores credentials, active personalities, workspace files, memory, profile images, channels, settings, and topic lock rules.
4. Operator clicks Sync or runtime control in Bot Manager.
5. Backend calls Nanobot internal Morneven endpoints.
6. Nanobot pulls the active runtime bundle from Backend using `MORNEVEN_BOT_MANAGER_SYNC_TOKEN`.
7. Nanobot writes one runtime directory per active identity under `/data/.nanobot/runtimes`.
8. Nanobot starts or restarts the relevant gateway process.

Runtime mode is controlled by Bot Manager:

- `single-active-personality` runs only the main personality.
- `multi-active-personality` runs all active identities while preserving one main identity.

## Environment Variables

Required for dashboard access:

```env
ADMIN_USERNAME=<admin-user>
ADMIN_PASSWORD=<strong-password>
```

Required for Morneven integration:

```env
MORNEVEN_BACKEND_INTERNAL_URL=https://<backend-internal-or-public-url>
MORNEVEN_BACKEND_PUBLIC_URL=https://<backend-public-url>
MORNEVEN_BOT_MANAGER_SYNC_TOKEN=<same-as-backend-BOT_MANAGER_SYNC_TOKEN>
NANOBOT_MORNEVEN_RELOAD_TOKEN=<same-as-backend-NANOBOT_MORNEVEN_RELOAD_TOKEN>
```

Workspace:

```env
NANOBOT_AGENTS__DEFAULTS__WORKSPACE=/data/.nanobot/workspace
```

Optional runtime controls:

```env
NANOBOT_GATEWAY_BASE_PORT=18790
NANOBOT_GATEWAY_LOG_MAX_BYTES=2000000
MORNEVEN_TELEGRAM_ROUTING_DEBUG=1
```

Railway should attach a persistent volume mounted at `/data`.

## Deployment

This repo is deployed as a Docker service.

Required platform setup:

- Docker build enabled.
- Public service URL for dashboard access.
- Persistent volume mounted at `/data`.
- Environment variables configured.
- Backend `NANOBOT_INTERNAL_BASE_URL` points to this service, preferably using internal Railway networking.

Railway uses `railway.toml` and starts:

```bash
/app/start.sh
```

## Endpoints

Public dashboard and Nanobot endpoints:

```text
GET  /
GET  /health
GET  /api/config
PUT  /api/config
GET  /api/status
GET  /api/logs
POST /api/gateway/start
POST /api/gateway/stop
POST /api/gateway/restart
GET  /api/runtimes/{identity_id}/config
PUT  /api/runtimes/{identity_id}/config
GET  /api/runtimes/{identity_id}/logs
POST /api/runtimes/{identity_id}/gateway/{action}
```

Morneven protected endpoints:

```text
GET  /api/morneven/status
GET  /api/morneven/workspace/changes
GET  /api/morneven/config-secrets
GET  /api/morneven/telegram/topics
POST /api/morneven/gateway/start
POST /api/morneven/gateway/stop
POST /api/morneven/gateway/restart
POST /api/morneven/runtimes/{identity_id}/gateway/{action}
POST /api/morneven/reload
```

Protected endpoints require `x-morneven-reload-token` matching `NANOBOT_MORNEVEN_RELOAD_TOKEN`.

## Telegram Topic Lock

Topic Lock is stored per personality under `channels.telegram.topicLock`. Each group can allow selected forum topics, block the main or General topic, and define a primary outbound topic.

Example:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "<telegram-bot-token>",
      "allowFrom": ["*"],
      "topicLock": {
        "enabled": true,
        "defaultPolicy": "allow",
        "groups": [
          {
            "chatId": "-1003950002621",
            "title": "Morneven Playground",
            "isForum": true,
            "allowMainTopic": false,
            "allowedTopicIds": ["2", "159"],
            "primaryTopicId": "2"
          }
        ]
      }
    }
  }
}
```

Behavior:

- Inbound messages from blocked topics are dropped before they reach the agent.
- Explicit outbound messages to blocked topics are blocked.
- Outbound system messages without a topic are redirected to `primaryTopicId` when main is blocked.
- Telegram main or General topic is normalized from empty, `0`, `1`, or `main`.
- Observed topic names can be incomplete because Telegram does not always send topic title events.
- Bot Manager provides manual topic add and topic title editing for registry cleanup.

## Native Standalone Mode

This service can run without Morneven Website and Morneven Backend. In that mode it acts as a plain Nanobot runtime with the Morneven runtime patches still available.

Use native mode when you want:

- A standalone Nanobot dashboard.
- Direct config editing through `/api/config`.
- A single native gateway managed by this service.
- No Bot Manager identities, no backend storage, and no website UI.

### Native Setup

1. Deploy this repo as normal.
2. Set only dashboard and workspace variables:

```env
ADMIN_USERNAME=<admin-user>
ADMIN_PASSWORD=<strong-password>
NANOBOT_AGENTS__DEFAULTS__WORKSPACE=/data/.nanobot/workspace
```

3. Do not set these Morneven integration variables:

```env
MORNEVEN_BACKEND_INTERNAL_URL=
MORNEVEN_BACKEND_PUBLIC_URL=
MORNEVEN_BOT_MANAGER_SYNC_TOKEN=
NANOBOT_MORNEVEN_RELOAD_TOKEN=
```

4. Open the dashboard at `/`.
5. Write config through `PUT /api/config` or use the dashboard config editor.
6. Start the gateway with `POST /api/gateway/start`.

### Minimal Native Config

Use this shape as a starting point and adjust it to the provider and channels you use:

```json
{
  "providers": {
    "openai": {
      "apiKey": "<api-key>",
      "model": "gpt-4.1-mini"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openai",
      "model": "gpt-4.1-mini",
      "workspace": "/data/.nanobot/workspace"
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "<telegram-bot-token>",
      "allowFrom": ["*"]
    }
  }
}
```

### Native Topic Lock

Native mode does not use Bot Manager storage, so topic rules live directly in the runtime config under `channels.telegram.topicLock`. You can edit the JSON manually through `/api/config`.

For topic titles, native mode has two options:

- Use clear manual titles in your config comments or external notes.
- Keep `topicLock.groups` readable with group `title`, `allowedTopicIds`, and `primaryTopicId`.

The observed topic registry still writes to runtime storage, but native mode does not provide the full Bot Manager topic editing UI.

### Converting This Repo To A Pure Native Fork

If you want a smaller native-only fork, keep these parts:

- `Dockerfile`
- `start.sh`
- `server.py`
- `sitecustomize.py`
- `templates/`
- `img/`
- `requirements.txt`

Then remove or ignore Morneven-only environment variables and endpoints:

- `MORNEVEN_BACKEND_INTERNAL_URL`
- `MORNEVEN_BACKEND_PUBLIC_URL`
- `MORNEVEN_BOT_MANAGER_SYNC_TOKEN`
- `NANOBOT_MORNEVEN_RELOAD_TOKEN`
- `/api/morneven/*`
- runtime bundle materialization from Backend

Recommended native behavior:

- Keep `/api/config`, `/api/status`, `/api/logs`, and `/api/gateway/*`.
- Keep `sitecustomize.py` if you still want Telegram topic lock, multi mention routing, and message thread fixes.
- Use one persistent workspace path.
- Store credentials only in Nanobot config or platform secrets.

## Operational Notes

- Do not edit Morneven-managed runtime workspace files directly unless intentionally testing sync conflict behavior.
- Bot Manager sync will replace files owned by active personality bundles.
- Gateway start and restart pull current Morneven runtime data before launching when integration is configured.
- If backend sync tokens differ, reload and runtime bundle fetch will fail.
- If `/data` is not persistent, runtime state is lost on redeploy.
- Telegram forum topic sends accept numeric `message_thread_id`, `thread_id`, or `topic_id` metadata.
- Restart affected runtimes after changing runtime config, topic lock rules, credentials, or workspace files.
- Check logs for `topic_lock_drop`, `topic_lock_block`, and `topic_lock_redirect` when debugging Telegram forum routing.

## Documentation

Active shared documentation:

- [Platform Architecture](../Document/Documentation/General/2026-05-25-platform-architecture-v01.md)
- [Bot Manager Guide](../Document/Guide/General/2026-05-27-bot-manager-guide-v01.md)
- [Bot Manager Alpha Deployment Guide](../Document/Guide/General/2026-05-21-bot-manager-alpha-deployment-guide-v01.md)
- [Backend API Contract](../Document/Documentation/Backend/root-docs/2026-05-25-backend-api-contract-v01.md)
- [Document Index](../Document/Documentation/General/2026-05-27-document-index-v02.md)
