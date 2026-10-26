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

## User-facing acceptance testing

Run this workflow for every user-facing release and after a live deployment.
Test-only work must not change plugin code.

### Preconditions and safety

- Use PowerShell 7 at
  `D:\Tool\PowerShell-7.6.6-win-x64\pwsh.exe` for local commands.
- Verify the AstrBot version, plugin version, enabled state, failed-plugin list,
  view HTTP status, and absence of duplicate plugin identities.
- Record the owner UID count, subscription count, trade-rule/order count, trade
  limits, output templates, and settings before changing any test data.
- Copy plugin data to a temporary backup for reference, but restore runtime
  state through the WebUI/API. Never directly write runtime files as cleanup.
- Never raise live-trade limits or arm a live rule during acceptance testing.
  Live mode is limited to verifying the zero-limit rejection path.
- Obtain action-time confirmation before sending QQ messages or deleting test
  data. State the exact recipients, messages, and data that will be removed.

### WebUI matrix

- Check all four tabs, initial loading, polling, success/error feedback, and the
  browser console.
- Test desktop, a 390x844 viewport, and both light and dark themes. The page
  must not overflow horizontally; the tab bar may scroll horizontally.
- Complete the main path using only the keyboard: Tab/Shift+Tab, Enter/Space,
  tab switching, form submission, dialog cancellation, and Escape.
- Verify visible focus, modal focus trapping and focus return, tab/tabpanel
  semantics, live-region announcements, control names, and a 24x24px minimum
  target size.
- Require at least 4.5:1 contrast for normal text and at least 3:1 for large
  text and meaningful UI boundaries.
- Every action must provide immediate pressed feedback. Async actions must
  expose a disabled/busy state and prevent duplicate submission.
- Cover account status and password masking, monitor-group CRUD, output
  preview/validation/save/reset, zero-limit settings, paper-rule CRUD,
  pause/resume, paper drill, live-mode rejection, stop-all, deletion, and
  incremental logs.
- Capture the server-side cause when a generic UI error appears; record it in
  the report and AstrBot logs.

### Trade workbench and paper-book matrix

- Verify `/gjm trade` without a subcommand returns an annotated parameter-insufficient command tree, and that
  `/gjm trade help` plus `/gjm trade help <subcommand>` show usage, examples, and live-risk notes.
- Create a paper rule from the WebUI and confirm the full-screen workbench opens automatically. Verify the
  two-pane status/market layout, focus trap, Escape close, focus return, 390x844 stacking, and both themes.
- Verify the SVG depth chart draws green cumulative buy depth on the left and red cumulative sell depth on the
  right, with quantity and price axes, best-bid/best-ask markers, rule thresholds, cross-book handling, hover
  details, empty state, accessible summary, and no horizontal overflow.
- Exercise the stock-style order console: buy/sell switching, price and quantity validation, decimal limits,
  quantity shortcuts, buy cost preview, fixed-15% sell proceeds preview, inline errors, and keyboard operation.
- Exercise immediate add/edit/delete behavior. Each mutation must call `save_apply` exactly once, show a Toast
  and the latest-action status line, highlight the changed level, and reject duplicate clicks while busy.
- Verify duplicate-price rejection, 100-level limit, delete confirmation/cancel, load-live and rule-generated
  book application, reload persistence, and follow-live reset. A failed mutation must leave chart and levels unchanged.
- Confirm every paper-book action reports loading, success, or a concrete error and never calls real
  buy/sell/cancel APIs from paper mode.
- Verify a live rule cannot open or use the workbench. Switch a test paper rule to live only in MockTransport or
  unit tests; never enable live funds during user acceptance.
- Verify all checkbox-like controls use keyboard-operable `role=switch` semantics, visible focus, at least
  24x24px effective targets, and immediate pressed feedback.
- Verify the global environment switch displays an accurate risk summary. Acceptance testing may cancel the
  confirmation and test zero-limit rejection; it must not confirm global live mode or raise any live limit.
- Check incremental logs for paper-book edits, state transitions, batch mode results, and errors; confirm no
  duplicated nodes and that only the owner's private session receives trade-template notifications.
- Before cleanup, reload the plugin and verify the saved paper book, rule state, and global mode are recovered.
  Then delete test rules and books, restore rules/limits/templates/global mode, and confirm no auto orders remain.

### QQ private and group matrix

- Private chat commands do not use an @ mention; send `/gjm ...` directly.
- Group chat commands must start with the bot's native mention, currently
  `@真珠`; plain typed text is not a valid substitute.
- Send exactly one command at a time. For private chat, activate QQ and enter
  the command directly. For group chat, choose the native mention first. Send
  one command, then immediately activate the Codex window again.
- After switching back, verify the QQ composer is empty and only observe the
  reply without activating another input. Never leave a queued draft.
- Private checks include `/sid`, `/gjm trade list`, `/gjm help`, `/gjm status`,
  `/gjm trade budget`, subscription lifecycle commands, and owner-only trade
  commands after a temporary owner-UID update.
- Group checks include `@bot /gjm help` and `@bot /gjm trade list`; trading
  commands must be rejected outside a private session.
- Temporarily adding an owner UID is allowed only when the action was approved.
  Restore the original owner list before finishing.
- Verify local command replies within 15 seconds and market requests within 60
  seconds. Check for missing, duplicate, garbled, or incorrectly grouped
  messages and images.
- Confirm WebUI trade events reach only the owner's private session and never
  the group chat.

### Evidence and pass criteria

- Write a versioned report under `docs/` containing environment, baseline,
  cases, actual results, defect severity, evidence, cleanup, and residual risk.
- Redact passwords, tokens, email addresses, full UIDs, account identifiers,
  and unrelated chat content from committed evidence.
- The overall result is PASS only when there are no P0/P1 defects, core flows
  are completed, feedback is unambiguous, keyboard operation succeeds, and no
  blocking layout or frontend errors remain.
- Any unmitigated P2 keeps the result at FAIL for release purposes.
- Restore and verify all mutable test data. Append-only logs and chat history
  must be documented as expected residue.
- Record workbench layout, switch semantics, paper-book persistence, global-mode
  cancellation, and private-notification evidence with redacted screenshots or
  API/response excerpts.
