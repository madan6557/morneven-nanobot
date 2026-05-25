# Morneven Nanobot Runtime

`morneven_nanobot` is the Morneven-managed Nanobot runtime service. It hosts the gateway process, exposes a small admin dashboard, and receives active personality bundles from Morneven Backend through Bot Manager.

The canonical Morneven documentation lives in the shared workspace `Document/` folder.

## Repository Role

`morneven_nanobot` is responsible for:

- Running the Nanobot gateway in a container.
- Serving a Basic Auth protected dashboard.
- Persisting runtime files under `/data/.nanobot`.
- Pulling active Bot Manager runtime bundles from Morneven Backend.
- Materializing active personality workspace files.
- Starting, stopping, and restarting the gateway.
- Reporting gateway status, logs, and Morneven runtime state.
- Pushing safe config secret summaries back to Morneven Backend where supported.

Morneven Backend remains the source of truth for credentials, personalities, workspace files, memory files, channels, and runtime settings.

## Related Repositories

| Repository | Relationship |
| --- | --- |
| `morneven-website` | Provides the Bot Manager UI used by PL7 Admin and PL7 Author |
| `morneven-backend` | Stores Bot Manager data and sends runtime bundles to Nanobot |
| `morneven_nanobot` | Executes the active runtime personality and gateway |

## Runtime Flow

1. Operator configures Bot Manager in Morneven Website.
2. Website saves configuration to Morneven Backend.
3. Backend stores credentials, active personality, workspace files, memory, profile image, channels, and settings.
4. Operator clicks sync or runtime control in Bot Manager.
5. Backend calls Nanobot internal Morneven endpoints.
6. Nanobot pulls the active runtime bundle from backend using `MORNEVEN_BOT_MANAGER_SYNC_TOKEN`.
7. Nanobot writes files into `/data/.nanobot/workspace`.
8. Nanobot starts or restarts the gateway.

Only one active runtime personality is supported at a time.

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
```

Morneven protected endpoints:

```text
GET  /api/morneven/status
GET  /api/morneven/workspace/changes
GET  /api/morneven/config-secrets
POST /api/morneven/gateway/start
POST /api/morneven/gateway/stop
POST /api/morneven/gateway/restart
POST /api/morneven/reload
```

Protected endpoints require `x-morneven-reload-token` matching `NANOBOT_MORNEVEN_RELOAD_TOKEN`.

## Operational Notes

- Do not edit runtime workspace files directly unless intentionally testing sync conflict behavior.
- Bot Manager sync will replace files owned by the active personality bundle.
- Gateway start and restart pull current Morneven runtime data before launching when integration is configured.
- If backend sync tokens differ, reload and runtime bundle fetch will fail.
- If `/data` is not persistent, runtime state is lost on redeploy.

## Documentation

Active shared documentation:

- [Platform Architecture](../Document/Documentation/General/2026-05-25-platform-architecture-v01.md)
- [Bot Manager Alpha Integration Plan](../Document/Documentation/General/2026-05-21-bot-manager-alpha-integration-plan-v01.md)
- [Bot Manager Alpha User Guide](../Document/Guide/General/2026-05-21-bot-manager-alpha-user-guide-v01.md)
- [Bot Manager Alpha Deployment Guide](../Document/Guide/General/2026-05-21-bot-manager-alpha-deployment-guide-v01.md)
- [Backend API Contract](../Document/Documentation/Backend/root-docs/2026-05-25-backend-api-contract-v01.md)
- [Document Index](../Document/Documentation/General/2026-05-25-document-index-v01.md)
