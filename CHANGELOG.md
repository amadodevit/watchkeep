# Changelog

Changes to the plugin contract are marked **Plugins**.

## 0.1.0 (unreleased)

- `watchkeep run`: the scheduler without the UI, for a login service (launchd, systemd). Only one process syncs a database at a time: a lock next to `state.db` decides, and the others (e.g. the UI opened alongside) only re-read what it writes, taking over when it stops.
- `watchkeep status [--json]`: what needs you from the last syncs (open items and failing rows per source, appointments left out), for scripts and other tools. Never syncs; settings and secrets are not included.
- Calendar source (`pip install 'watchkeep[calendar]'`): the next 31 days of appointments from a secret iCal address (Google Calendar gives one per calendar). Recurring meetings are expanded, cancelled ones dropped. An Agenda tab lists them by day, all-day first; what is over is struck through and what is happening now is marked. The tab shows only while a calendar source exists. On the Agenda, `d`, `x` and `w` show today, 3 days or a week from today (remembered); every day gets a header and free days read "nothing on". The source no longer has a "Days to show" setting; a stored one is ignored.
- **Plugins:** a third kind, `kind = "events"`, returning `Appointment` objects. Additive: items and samples plugins need no change.

- Renamed from fly-monitor to watchkeep: package, `watchkeep` command, data in `~/.local/share/watchkeep`, keyring service `watchkeep`.
- **Plugins:** the entry-point group is now `watchkeep.sources` and the contract imports from `watchkeep.core.plugin`. Breaking for plugins written against the unreleased fly-monitor name; nothing was published under it.
- Terminal UI with sources grouped and collapsible.
- GitHub source: review requests, comments and mentions, accounts detected through `gh`.
- GitHub source: "My PRs": every open PR of yours, one row each from a single GraphQL query, under its most pressing state: checks failing, changes requested, approved, partly approved (the repo wants more approvals), commented, or waiting for review. Rows name who put them there; bots don't count. Unread comments and reviews on your own open PRs become one "activity on my PR" row per PR, whatever reason GitHub gives (`author`, or `comment`/`mention` once you joined in), instead of plain comment rows. On for new sources; sources added before need it ticked under Watch.
- **Plugins:** `HttpConnection.post_json` for JSON POSTs such as GraphQL queries. Additive.
- GitHub source: optional "PRs with no reviewer" in picked repos (names or patterns): open PRs nobody was asked to review, assigned or reviewed yet, drafts and your own skipped. PRs already waiting when a repo is first watched show without a popup.
- Website source: status code and response time, failing after 5 of the last 10 checks.
- Desktop and in-app notifications, acknowledge and close, ignore.
- Secrets live in the OS keyring. Older databases are migrated on startup.
- ActiveMQ Classic source: one row per queue (waiting, consumers, and messages in/out since the last check), queues picked from the broker or by pattern.
- Checks: rules on a source's numbers, edited in the add-source form.
- Attention first: source headers count failing/acknowledged/ok rows and turn red or orange; failing rows sort first.
- Recovered notification when a failing row is healthy again.
- Overview grouped by client › kind (`g` cycles: kind, environment › kind, flat); `-`/`+` fold or unfold one level at a time. Sources get client and environment fields.
- Each kind gets its own columns (queues: waiting, consumers, in, out); healthy sources take one line.
- Panels: tabs that each show a saved filter (client, environment, tag, type, plus hand-picked sources). `p` manages them, `P` turns a group header into one, `1`–`9` and `Tab` switch, and each tab shows its failing count. "All" is always there. Plugins need no change: any source can be filtered by type, client or tag.
- Notifications tab: this session's events, newest first; Enter opens one.
- **Plugins:** optional `title`; declared metrics become the columns of your rows.
- Edit a source in place (`e`): everything but its identity (URL, account). History is kept.
- **Plugins:** `Metric`, `Check`/`Condition` and `default_checks` for samples plugins; `Setting.fetch` with the `options(ctx, key)` hook.
- **Plugins:** `SyncContext.quiet`: item ids to show without a notification when new (additive).
- **Plugins:** `SyncContext.memory` (per-source state the core keeps between successful syncs), `SyncContext.warnings` (partial failures shown on the source row) and `ctx.http.get` (raw response, for XML and conditional GETs). All additive: existing plugins need no change.
- Read and archive for item sources (GitHub, feeds, any items plugin): mark an item read to hide it for good; the archive keeps every item ever reported, with when it appeared and when it was read, and can mark it unread. Kept in the core, so plugins need no change.
- **Plugins:** first version of the contract, see [PLUGINS.md](PLUGINS.md).
