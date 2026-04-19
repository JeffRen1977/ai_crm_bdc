---
name: openclaw_at_router
description: Route inbound chat messages whose first token is "@pc", "@pricredit", "@idvault", or "@wechat". Must be your FIRST action on every inbound message. For @pc / @pricredit you MUST run the bash CLI `~/Documents/projects/PriCredit/scripts/pc ...` and return its stdout verbatim. For @idvault you MUST run `~/Documents/projects/IDValut/scripts/iv ...`. For @wechat (explicit prefix) or no prefix, defer to the wechat_from_inbox / default wechat skills. Never reply "I don't have access" — the CLIs are on disk at the paths above.
---

# @-prefix router

**Goal:** When a user sends a chat message (WhatsApp, Telegram, …), look at the first whitespace-separated token. If it starts with `@pc`, `@pricredit`, `@idvault`, or `@wechat`, you dispatch. Otherwise you fall through to the existing wechat skills.

Do **all** of the following in order. Do not skip the bash commands.

---

## Step 1: Extract the leading prefix

Strip leading whitespace from the message. Lowercase the first whitespace-separated token. Match it against:

| Prefix                 | Target                         |
|------------------------|--------------------------------|
| `@pc` or `@pricredit`  | PriCredit CLI (BDC risk)       |
| `@idvault`             | IDVault CLI (face verification) |
| `@wechat`              | Default wechat pipeline (strip prefix) |
| *(anything else)*      | Default wechat pipeline (unchanged) |

---

## Step 2: Dispatch — `@pc` / `@pricredit` (PriCredit BDC risk)

You **MUST** run this bash command. Do not summarize, do not paraphrase, do not invent an answer. The CLI is a read-only Python script that already exists on disk at `~/Documents/projects/PriCredit/scripts/pc`.

Replace `<REMAINING_ARGS>` with every token **after** the `@pc` / `@pricredit` prefix, preserving order and quoting:

```bash
~/Documents/projects/PriCredit/scripts/pc <REMAINING_ARGS>
```

If there are no arguments after `@pc` (i.e. the user sent just `@pc`), run:

```bash
~/Documents/projects/PriCredit/scripts/pc help
```

**Return value:** reply to the user with the script's stdout, verbatim, in a single chat message. Do not prepend "here is the output", do not wrap it in commentary. If stdout is longer than 3800 characters, truncate and append `\n… (truncated)`. Do **not** invoke any other skill after this; the router's job is done.

**Examples (you run the bash command, you paste stdout back):**

| User message                         | You must run                                                   |
|--------------------------------------|----------------------------------------------------------------|
| `@pc status ARCC`                    | `~/Documents/projects/PriCredit/scripts/pc status ARCC`        |
| `@pc top 10`                         | `~/Documents/projects/PriCredit/scripts/pc top 10`             |
| `@pc digest`                         | `~/Documents/projects/PriCredit/scripts/pc digest`             |
| `@pc alerts --severity critical`     | `~/Documents/projects/PriCredit/scripts/pc alerts --severity critical` |
| `@pc`                                | `~/Documents/projects/PriCredit/scripts/pc help`               |
| `@pricredit brief ARCC`              | `~/Documents/projects/PriCredit/scripts/pc brief ARCC`         |

---

## Step 3: Dispatch — `@idvault` (IDVault face verification)

You **MUST** run this bash command:

```bash
~/Documents/projects/IDValut/scripts/iv <REMAINING_ARGS>
```

Return stdout verbatim, same rules as Step 2. If the script returns `command not found` or exits with "not yet wired", pass its output through — the user needs to see the real error, not a generic apology.

**Examples:**

| User message               | You must run                                           |
|----------------------------|--------------------------------------------------------|
| `@idvault status`          | `~/Documents/projects/IDValut/scripts/iv status`       |
| `@idvault list`            | `~/Documents/projects/IDValut/scripts/iv list`         |
| `@idvault`                 | `~/Documents/projects/IDValut/scripts/iv help`         |

---

## Step 4: Dispatch — `@wechat` (explicit) or *no prefix*

Strip the `@wechat` token if present. Then hand the remaining text to `wechat_from_inbox` and the other wechat skills exactly as if the user had sent that text with no prefix. This preserves the current behavior: pasting a URL with no prefix still produces a WeChat draft.

If the message is none of the above (no prefix at all), also defer to `wechat_from_inbox` and the default wechat stack unchanged. Do not add any `@pc`/`@idvault` reasoning to those cases.

---

## Step 5: Safety rules (non-negotiable)

- **Absolute CLI paths only.** Always invoke `~/Documents/projects/PriCredit/scripts/pc` and `~/Documents/projects/IDValut/scripts/iv` with a leading `~/` — never a relative path. Both CLIs resolve their own project roots.
- **Pass arguments as separate tokens** exactly as the user wrote them. Never concatenate user input into a single shell-escaped string; let the bash tool handle argv.
- **60-second timeout.** If the CLI doesn't return within ~60 seconds, stop waiting and reply `@pc timeout (>60s). Try a narrower query.`
- **Never reply "I don't have access to PriCredit" or similar.** The CLI IS on disk at the paths above. If you cannot find it, the correct answer is to run `ls ~/Documents/projects/PriCredit/scripts/pc` and report the actual filesystem error — not to apologize.
- **Read-only v0.** Neither `pc` nor `iv` writes files in v0. A future `@pc run` (which would trigger the daily pipeline) is **out of scope**; if the user asks, reply `@pc run is not enabled in v0. Run scripts/run-daily-pricredit.sh from the terminal.` Do not invent it.

---

## When to use this (trigger)

- **Every single inbound message.** This skill MUST run first so it can decide whether to dispatch or to yield to the wechat stack.
- Explicit triggers: the message begins with `@pc`, `@pricredit`, `@idvault`, or `@wechat` (case-insensitive).
- Implicit pass-through: no prefix → defer to `wechat_from_inbox`.
