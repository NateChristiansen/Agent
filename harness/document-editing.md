# Replacing a feature document, reviewably

The `harness` tool with `action="notes_document"` replaces a whole document from a UTF-8 file,
and shows you the diff before it writes anything. It writes only with `apply=True`. Use it for a feature's `plan.md` body
and for top-level documents like `audit.md`, `design.md` and `implementation-plan-review.md`.
The path you give is always a vault path, even though `features/` links into the private notes
repo.

Write the complete replacement to a scratch file, then preview with `target="features/001-example/plan.md"`
and `from_file=".agent/scratch/plan.md"`.

The preview names the vault path and the physical target it resolved to, prints a unified diff,
and prints the SHA-256 of the original. Read the diff, then call `notes_document` again with
`apply=True` and `expect_sha256=<digest-from-preview>`.

The digest covers the whole original file — frontmatter, tasks, handoffs and all — so a single
task tick makes it stale. When that happens, read the current file and build a fresh
replacement. Do not swap in the new digest to push an old draft through; the digest is the check
that you are editing what you think you are editing. Preview again whenever the draft changes.

Nothing here needs inline Python or shell interpolation: dollar signs, backticks and newlines
come out of the file literally. The tool result gives the diff, both paths, the before and after
digests, and whether anything was written.

A new document needs `create=True` and `expect_absent=True`, so it cannot happen by accident.
Preview first with `target="features/001-example/audit.md"` and
`from_file=".agent/scratch/audit.md"`; then call the tool again with `apply=True`.

Creation refuses an existing target, and it never creates a feature — that is the `feature_new` tool.
Names are ASCII letters, digits, hyphens or underscores plus `.md`. Hidden, nested, absolute and
traversal paths refuse. Logs, handoffs, the generated status and index documents, the owner's
`notes/`, configuration and the product repos are all out of scope. Reserved names match without
case sensitivity, and only the canonical `plan.md` is accepted.

In `plan.md`, the frontmatter and the whole `## Tasks` and `## Handoffs` sections must come
through byte for byte, headings and trailing whitespace included. Change those with the feature,
task and dispatch commands instead. The editor checks both the CLI's raw heading scan and the
headings visible outside fences and comments, so an ambiguous, duplicated or hidden protected
heading refuses, and a fenced example cannot stand in for a real Tasks section. Other editable
documents replace their whole contents.

## Filesystem and concurrency limits

This works on plain features and on the single setup-managed link into the remembered notes
root. It refuses a redirected feature root, a symlinked feature directory or document, a
hardlinked document, and a `plan.md` whose feature identity does not match. Access is
directory-relative and no-follow, anchored to the directory it validated. The notes-root
registration and its parent directories are trusted configuration — this is not a boundary
against something that can move those directories or rewrite that configuration while you work.

POSIX directory locks and no-follow directory access are required. An unsupported platform,
Windows included, refuses rather than falling back to an unlocked write. Sandbox permissions are
unchanged, and a private target still needs filesystem authorization from the host.

Apply takes an exclusive lock on the feature directory. Document commands serialize on it and
re-check the expected digest underneath it, including commands from a separate harness clone
linked to the same feature directory. A replacement writes a temporary sibling, flushes it, and
renames it into place. A creation publishes the finished file with an atomic link that refuses
an occupied target. An ordinary failure cleans its temporary file up.

Task, status, dispatch and outside editor writes do not take that lock. The command re-reads the
target's bytes and file identity immediately before the rename, but that check and the rename are
not one compare-and-swap, so an uncooperative writer can still land in between. Avoid concurrent
writes and re-preview after one finishes. Atomic visibility is not a promise that directory
metadata survives a crash. A killed process can leave a hidden temporary file behind; it never
becomes the document.
