# Creek

Branch-and-merge decision tracking for long Claude Code sessions.

A long session drifts. Fixes pile up in the context, the same question gets asked in every reply, and after a compaction the reason behind an earlier decision is gone, so it gets undone. Creek keeps a small structured record of the session and hands the relevant piece back to Claude only when it matters.

It tracks:

- **nodes**: the points of your brief
- **branches**: detours to fix one point (inline, or in a forked session)
- **records**: what was decided, the rule it creates, what it touches, what it replaces
- **questions**: things only you can answer

Creek never calls a model. Everything it does is local file work and string matching. Claude's only extra work is an occasional one-line `creek:` marker at the end of a reply it was writing anyway.

```
creek · 4 open · 1 you (q1 ×6) · 1 conflict · 1 unrecorded
```

## Install

Requires Python 3.11+ (standard library only) and Claude Code.

```sh
pipx install git+https://github.com/arnavpopley/creek     # puts `creek` on PATH
claude plugin marketplace add arnavpopley/creek             # registers the hooks
claude plugin install creek@creek
creek install                                               # adds the status line
creek doctor                                                # checks PATH, ledger, hook latency
```

`creek install` backs up `~/.claude/settings.json` before changing it. If you already have a status line, Creek wraps it (`creek statusline --then '<yours>'`) so yours still prints first. `creek install --remove` puts it back.

To try Creek for one session without installing the plugin, run `claude --plugin-dir <path to this repo>/plugin`.

## Using it

Work as usual. Creek adds a short protocol to the start of each session. Claude ends a reply with a marker only when one applies:

```
creek: on 11
creek: node Which key 'more' uses
creek: resolve 11 | decision: ignore key repeats and keys pressed while a dialog is open | rule: the early return runs before the key is marked handled | affects: src/components/ContactList.tsx | tags: keydown, repeat
creek: ask Which key should 'more' use?
```

Paste a numbered brief (3+ top-level items, or start with `brief:`) and each item becomes a node.

Creek works in four places:

- **Before an edit:** if the file or the edit's text touches a settled record, the record is shown to Claude first: `r9 (n11, msg 6): Ignore key repeats…. Rule: …`. At most 3 records, each ≤ 220 chars, and the same record isn't shown again within 15 turns.
- **Questions:** a question is asked once and queued for you. Repeats are counted (`q1 ×6`), not repeated in replies.
- **After compaction:** open nodes, open questions and active rules come back automatically, plus the settled decisions.
- **Conflicts:** two records on different nodes that touch the same file are flagged for you to check.

### Commands in chat

Prompts starting with `::` are handled by Creek and never reach Claude (zero tokens).

| command | does |
|---|---|
| `::status` | open nodes, questions, conflicts, open branches |
| `::map` | the board, with a git-style graph of your prompts, branches and merges: inline in the chat in Orca, otherwise `.creek/map.html` |
| `::answer q1 o` | answers a question, records it, tells Claude on your next message |
| `::branch 13` | opens an inline branch on n13 |
| `::merge` | closes the current branch with its latest record |
| `::table 9 <reason>` / `::drop 9 <reason>` | tables or abandons a node |
| `::node <title>` | adds a node |
| `::ok c1` / `::supersede c1` | keeps both records, or retires the older one |
| `::pull r7` | shows Claude the full record and its source reply next turn |

### Terminal

```
creek status                  same as ::status
creek fork <node>             work on a node in a forked session (see below)
creek map [--open]            writes .creek/map.html (and, inside Orca, prints the line that shows it in chat)
creek sync                    PR state and review scores from gh
creek cost                    what Creek injected, per hook, against the transcript's token usage
creek replay <fixture.json>   loads a session reconstruction into a fresh ledger (testing)
creek install | doctor
```

**Forks.** Run `creek fork 9` in a second terminal. It opens a branch on n9 and starts `claude --resume <your session> --fork-session`, tagged to that branch. Fix the point there and type `::merge` once Claude has resolved it. The main session gets one line on its next message: `b18 merged into n9: <decision>. Rule: <rule>`. Outside a terminal, `creek fork` prints the command instead of running it.

## Sub-chats in Orca

`orca-plugin/` is an Orca sidebar panel: each point of your brief gets its own sub-chat with its own box. Typing there runs that point in a fork of your main session; when it's settled the sub-chat dissolves and the main chat gets one line. The same thing from a terminal: `creek sub list`, `creek sub send 3 "teal"`.

## How it works

```
Claude Code hooks ──► creek hook <event> ──► .creek/events.jsonl  (append-only, the source of truth)
                                         └─► .creek/state.json    (folded from the log, cached)
```

| hook | what Creek does |
|---|---|
| SessionStart | protocol (≤ 180 tokens) + state block (≤ 1,200 tokens); after compaction, settled decisions too |
| UserPromptSubmit | `::` commands, brief splitting, change notes (≤ 400 tokens) |
| PreToolUse (edits) | the guard: matching records as context; never blocks |
| PostToolUse | logs edits and PR activity |
| Stop | reads `creek:` markers from the final reply; never blocks |
| PreCompact | logs the compaction |

Everything lives in `<git root>/.creek/`, which carries its own `.gitignore`, so the ledger is never committed. Hooks finish in about 60 ms at p95 against a 500-event ledger. On any internal error they exit 0 silently and log to `.creek/errors.log`, so Creek can't break a session.

## Measured

Replaying a real 16-message website session (`tests/fixtures/website-session.json`, scrubbed) recovers **6 of the 8 details that were lost** in it. Three came from the guard showing an earlier rule at a later edit. Two came from the question queue. One was only caught at the fix edit, not the first touching edit. The 2 misses had no edit to hook: one was a diagnosis given in a reply, the other a reminder to recheck production. With decision text only (no rules or tags), 5 of 8 are recovered, and the top-layer case is missed. On the real sessions used to build this, Creek's injected text was 2.6–3.5% of the new input tokens.

## Limits

- Term matching misses dependencies about behaviour when the edit doesn't mention a tagged term. `::pull` and the map are the backup.
- Records depend on Claude writing markers. The unrecorded check catches a merged PR with no record, but not a decision that left no trace.
- Inline branches don't shrink the context until compaction. Forks do, at the cost of switching sessions.
- Review scores are captured only from Bash output, or by `creek sync` when the reviewer's comment shows a score.
- No claude.ai chat app support (it has no hooks), no team use, and no cloud sync.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests
```

The map tests render in headless Chrome or Chromium when one is installed (`CREEK_CHROME` overrides the path) and are skipped otherwise.
