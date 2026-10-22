# Project Agent Instructions

## Secrets

- Local AstrBot credentials are stored in `.env`.
- Read `ASTRBOT_API_URL` and `ASTRBOT_API_KEY` from `.env` or the process
  environment.
- Never commit `.env`, print the API key, include it in command output, or copy
  it into tracked files.
- Keep `.env.example` free of real credentials.

## AstrBot management

- Manage AstrBot through its official WebUI or HTTP API. Do not directly modify
  the AstrBot runtime, plugin installation directory, database, or process.
- Prefer the HTTP API once `ASTRBOT_API_KEY` is available; browser automation is
  only a fallback.
- The local OpenAPI schema and interactive documentation are available at:
  - `GET $ASTRBOT_API_URL/api/v1/openapi.json`
  - `$ASTRBOT_API_URL/api/v1/docs`
- Plugin management endpoints require the `plugin` API key scope:
  - `GET /api/v1/plugins`
  - `POST /api/v1/plugins/install/upload`
  - `POST /api/v1/plugins/install/github`
  - `POST /api/v1/plugins/{plugin_id}/update`
  - `POST /api/v1/plugins/{plugin_id}/reload`
- MCP management endpoints require the `mcp` API key scope.
- Send the key with `X-API-Key: $ASTRBOT_API_KEY`.

## Deployment workflow

1. Run tests, Ruff, schema validation, and an AstrBot-environment import check.
2. Package only committed files:
   `git archive --format=zip --prefix=astrbot_plugin_gaijin_market_watcher/ --output=<temp.zip> HEAD`
3. Deploy the ZIP with:
   `POST $ASTRBOT_API_URL/api/v1/plugins/install/upload`
   using multipart field `file` and
   `ignore_version_check=true` when replacing the same or cached version.
4. Verify the installed version and enabled state through `GET /api/v1/plugins`
   and confirm the plugin starts in the AstrBot logs.

## Git workflow

- Do not commit feature or fix work directly to `main`.
- Create and push a `fix/*` branch for each update.
- Do not create pull requests unless the user explicitly asks for one.
- Wait for explicit user confirmation before merging a `fix/*` branch into
  `main`.
- Do not create a release, tag, or deploy unless the user explicitly requests
  it.
