# Changelog

Changes to the plugin contract are marked **Plugins**.

## 0.1.0 (unreleased)

- Renamed from fly-monitor to watchkeep: package, `watchkeep` command, data in `~/.local/share/watchkeep`, keyring service `watchkeep`.
- **Plugins:** the entry-point group is now `watchkeep.sources` and the contract imports from `watchkeep.core.plugin`. Breaking for plugins written against the unreleased fly-monitor name; nothing was published under it.
- Terminal UI with sources grouped and collapsible.
- GitHub source: review requests, comments and mentions, accounts detected through `gh`.
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
