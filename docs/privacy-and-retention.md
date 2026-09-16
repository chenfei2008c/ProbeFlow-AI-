# Privacy, retention, deletion, and recovery

ProbeFlow V1.1 treats a submitted interview as a permanent archive. Original participant audio, saved AI-question audio, machine transcripts, every text revision, memory snapshots, every report version, and report citations have no automatic expiry. Ending an interview, archiving its study, a long period without access, an expired invitation, and an expired login do not delete those records. Formal `audio_assets` remain `retention=permanent` with `expires_at=null`.

Submitted original audio is archived before decoding or ASR. If media validation fails, its duration remains unknown (`null`), the original bytes remain archived, and playback is disabled with the actual failure reason.

Permanent means “until an authenticated researcher deletes the session or the participant withdraws.” It does not mean public access. Every read still requires the relevant administrator or session-scoped participant authorization. It also does not describe a provider’s retention: text or audio sent to a configured ASR, language-model, or TTS provider remains subject to that provider’s policy and deletion interfaces.

## Local storage boundary

`DATA_DIR` contains the SQLite database and archive files. It must be outside the source repository. Session files use session-scoped relative paths such as `audio/<session-id>/...`, `assets/<session-id>/...`, and `chunks/<session-id>/...`; absolute paths, `..` traversal, and paths that resolve through a symlink outside `DATA_DIR` are rejected. File writes use a same-directory temporary file, `fsync`, and atomic replacement. Data directories use mode `0700` and files created by the storage service use mode `0600`.

Before a write, ProbeFlow reserves `MIN_FREE_BYTES` plus the new file size. If that margin is unavailable, it returns `STORAGE_FULL` and leaves existing archives in place. It never evicts a permanent archive to make room. `diagnostics()` reports total primary, formal, temporary, backup, and free bytes, plus the most recent valid backup it can find.

These permissions are access controls, not application-level encryption. Use FileVault, BitLocker, LUKS, or an encrypted server volume and protect operating-system accounts and backups. ProbeFlow does not claim that files are encrypted at rest by the application.

## Deletion and withdrawal

Deletion is serialized with database and storage writes in the supported single-process deployment. Before content is removed, ProbeFlow atomically writes a minimal tombstone to `<DATA_DIR parent>/<DATA_DIR name>-deletions.jsonl`, outside SQLite. A tombstone contains only the session ID, deletion time, and `deleted` or `withdrawn` status. It contains no transcript, report, participant code, event payload, or provider response.

The deletion transaction removes the session, consent, participant authentication, redeemed invitation, turns, all revisions, audio metadata, upload chunks, memory, reports and citations, events, jobs and their payload/results, and idempotency responses. It then removes unshared referenced files and known session-specific temporary/cache directories. Non-content usage and reservation accounting remain for financial reconciliation; those rows retain opaque session and job IDs but no interview text or audio.

The tombstone is deliberately committed before database deletion. If the process stops between those steps, `reconcile_deletions()` completes removal on the next startup before access or worker execution. File writes whose session-scoped path contains a tombstoned session ID fail with `SESSION_DELETED`. Workers must also call `is_deleted(session_id)` immediately before claiming work and before committing any result, because a database result row cannot be inferred from a file path. The API and worker use the same `Database.lock`, so a write that started first completes and is then deleted; a write that starts after the tombstone is refused.

Research deletion also writes a separate `<DATA_DIR name>-study-deletions.jsonl` ledger before removing its versions, invitations, sessions, and study-scoped outline tasks. Both ledgers are merged during recovery; a deleted outline task cannot return its content or cached response after recovery.

Keep both external deletion ledgers with recovery control data and restrict it to administrators. Replacing `probeflow.sqlite3` or restoring an old backup must never replace this ledger with an older copy. If both current storage and the latest external ledger are lost, an older backup alone cannot know about deletions made after that backup and must not be opened as a live archive.

V1.1 supports one backend process. The Python lock does not coordinate multiple worker processes or multiple hosts. A multi-process deployment requires an operating-system or distributed lock spanning task claim, file publication, deletion, and backup before it can make the same race-safety guarantee.

## Temporary cleanup

`cleanup()` first reapplies every tombstone. It then considers only files older than `TEMP_RETENTION_HOURS` in dedicated temporary roots and upload-chunk rows. It never deletes a formal `audio_assets` file, including archives that are many years old.

An upload chunk is removable only when all of these checks succeed:

- the turn has a finalized chunk list;
- a permanent asset is linked to the turn;
- that asset exists and its byte size and SHA-256 match the database;
- no queued, running, retryable, unknown, or otherwise nonterminal job belongs to the session;
- no nonterminal job payload/result references the chunk path; and
- both the database row and file are older than the retention cutoff.

Old unreferenced files under `temp`, `tmp`, `transcodes`, `segments`, and `cache` may be removed when no formal asset, remaining upload chunk, or nonterminal job references the path. Unfinished uploads remain because their `upload_chunks` rows and unfinalized turns remain. Export caches are not automatically cleaned in V1.1 because the current schema has no persistent active-download lease; an endpoint may regenerate or explicitly remove them after it can prove no download is active. Explicit deletion/withdrawal still removes that session’s export subtree.

This policy is intentionally conservative. Orphaned files with ambiguous ownership can consume space until an administrator resolves them. Diagnostics should be monitored and capacity expanded before the free-space reserve is reached.

## Backups

`backup()` creates a new self-contained directory under `BACKUP_DIR` (or the configured sibling default). While holding the storage lock it:

1. reapplies the latest deletion ledger;
2. uses SQLite’s online backup API to produce a consistent database snapshot, including committed WAL state;
3. copies every database-referenced formal asset, remaining upload chunk, and persisted in-flight audio checkpoint into the backup;
4. checks each file’s recorded byte size and SHA-256;
5. writes an asset/reference manifest and a snapshot of the deletion ledger;
6. verifies the SQLite integrity, foreign keys, manifest and all hashes; and
7. atomically publishes the completed backup directory.

Only after the new directory validates does rotation remove older complete backup directories. Rotation keeps `BACKUP_COUNT` daily recovery points (seven by default); manual CLI backups use a separate set with the same count. A manual backup cannot rotate away daily recovery points. Up to fourteen full copies may therefore be retained with the defaults. Daily scheduling resumes from the latest successful daily backup instead of creating one on every restart. Each point contains its own file copies, so deleting an eighth-oldest backup cannot remove the primary archive or a file used by another retained backup. Incomplete directories and unrelated files are not rotation targets.

Backup outcomes are recorded in `DATA_DIR/maintenance/backup-status.json`; failures are displayed instead of silently showing an earlier success. If the disk cannot accept even that status write, the process retains the failure in memory, but it cannot guarantee persistence of the status across a restart.

Backups can still contain content deleted after they were created. Access to backup media must therefore be restricted. Normal restoration filters the old snapshot with the latest external deletion ledger before it validates or exposes the recovered archive. If backup jobs stop, ProbeFlow cannot promise that deleted bytes disappear from old backup media within seven calendar days. Secure destruction of retired media is an operator responsibility.

## Offline restore drill

Restore only to a distinct empty directory outside the repository, the current `DATA_DIR`, and `BACKUP_DIR`. Do not point a running API or worker at the target. The application service should be stopped, or the drill should run on an isolated host/account with no worker process.

At the Python service boundary, the operation is:

```python
result = storage.restore(
    Path("/protected/backups/backup-..."),
    Path("/protected/restore-drill"),
)
```

The restore verifies the backup package before creating the target, copies it to a private staging directory, merges the backup ledger with the current external ledger, removes every tombstoned session, and then validates the result. Validation covers SQLite integrity and foreign keys; audio and upload-chunk sizes and SHA-256 hashes; current and previous transcript-revision links; asset-to-turn/session scope; report-to-session scope; citation turn/revision scope; citation offsets; and exact quoted text. Files that belonged only to filtered sessions are removed. Only a validated staging directory is renamed to the requested target.

Restore also writes `<target parent>/<target name>-deletions.jsonl` and `<target parent>/<target name>-study-deletions.jsonl`. Keep both sibling ledgers if the restored directory becomes a new primary archive. Before opening access, inspect the returned counts, run `diagnostics()` with the target configured as `DATA_DIR`, and confirm that deleted session IDs do not exist. A recovery drill should record the backup path, manifest hash, returned counts, external-ledger location, and the operator/date without copying interview content into tickets or the source repository.

## Limits outside ProbeFlow

Deleting local content cannot guarantee deletion from model/ASR/TTS providers, operating-system snapshots, storage-controller caches, previously copied backup media, or administrator-created exports. Provider deletion requests and media disposal follow their actual contracts and organizational policy. Never put live interview databases, recordings, deletion ledgers, backup manifests, API keys, or restore outputs in the Git repository or provide them to development tools for debugging.
