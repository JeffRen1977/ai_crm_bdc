# WhatsApp bridge — inbound + outbound routing

PriCredit shares a single WhatsApp number with the `wechat` and
`IDVault` OpenClaw projects. This document explains how messages flow
in each direction and how to set it up end-to-end.

## TL;DR

- **Outbound** (alerts you receive): handled by
  `scripts/send_whatsapp_alerts.py`, which shells out to the
  `openclaw message send --channel whatsapp` CLI. Config lives in
  `ingest/notifications.yaml` under the `whatsapp:` block. Disabled
  by default.
- **Inbound** (commands you send): the OpenClaw `main` agent receives
  every inbound WhatsApp message. A small **router skill**
  (`skills/openclaw-pc-router`) inspects the first token and
  dispatches on `@`-prefix:
  - `@pc ...` / `@pricredit ...` → `scripts/pc`
  - `@idvault ...`               → `IDValut/scripts/iv`
  - `@wechat ...`                → default wechat skills (prefix stripped)
  - *no prefix*                  → default wechat skills (unchanged)

The "no prefix = wechat" default preserves your current workflow —
pasting a URL with no prefix still generates a WeChat draft, exactly
like before.

## Architecture

```
WhatsApp (iPhone)
    │
    ▼
OpenClaw gateway  ─── allowFrom in ~/.openclaw/openclaw.json
    │                  enforces sender allowlist
    ▼
main agent  ───► openclaw-pc-router (first skill, always runs)
                     │
        ┌────────────┼──────────────┬───────────────────┐
        ▼            ▼              ▼                   ▼
     @pc ...      @idvault ...   @wechat ... /        (no prefix)
        │            │           (falls through)        │
        ▼            ▼                                  ▼
 PriCredit/       IDValut/                         wechat-from-inbox
 scripts/pc       scripts/iv                       + rest of wechat stack
        │            │                                  │
        └─ stdout ──┴──────► reply sent back to WhatsApp thread
```

## Install (one-time)

1. **Make sure `pc` is executable from the chat bridge.** Already
   done by default:
   ```bash
   ls -l ~/Documents/projects/PriCredit/scripts/pc
   ```
2. **Install the router skill into the `main` agent's skill stack.**
   The main agent's workspace (per `openclaw agents list`) is
   `~/Documents/projects/wechat`, and the gateway scans
   `<workspace>/skills/` for `openclaw-workspace` skills.
   ```bash
   cd ~/Documents/projects/PriCredit
   ./scripts/install-openclaw-skill.sh
   ```
   This **copies** `skills/openclaw-pc-router/` into
   `~/Documents/projects/wechat/skills/` (default). A symlink mode
   exists (`--symlink`) but the loader rejects it with
   `Skipping skill path that resolves outside its configured root`
   because the real path lives outside the wechat workspace. Always
   use the default copy mode.
3. **What the installer actually does** (so you know what to undo):
   - Copies `skills/openclaw-pc-router/` → `wechat/skills/openclaw-pc-router/`.
   - Patches `wechat/AGENTS.md` with a marker-fenced block that
     tells the `main` agent to invoke the router on every message.
     The block looks like:
     ```
     <!-- BEGIN openclaw-pc-router (do not edit; managed by install-openclaw-skill.sh) -->
     **@-prefix routing (WhatsApp / any chat channel).** ... skills/openclaw-pc-router/SKILL.md ...
     <!-- END openclaw-pc-router -->
     ```
   The **AGENTS.md patch is required**; without it the LLM sees the
   SKILL.md on disk but never consults it, because skills are not
   auto-attached — they're activated only when the agent's system
   prompt (AGENTS.md) references them. Use `--no-patch-agents-md`
   to skip, or edit by hand if you prefer different wording.
4. **No agent restart needed.** Skills and AGENTS.md are re-read by
   the gateway for every turn. Verify the router is registered:
   ```bash
   openclaw skills list | grep openclaw_at_router
   ```
5. **Dry-run test** (no WhatsApp delivery, runs the turn locally):
   ```bash
   openclaw agent --agent main --message "@pc status ARCC"
   ```
   You should see the risk snapshot in the terminal.

## Test end-to-end

Send yourself these messages from the WhatsApp number in
`~/.openclaw/openclaw.json → channels.whatsapp.allowFrom`:

| Send                             | Expect                                        |
|----------------------------------|-----------------------------------------------|
| `@pc status ARCC`                | One-paragraph risk snapshot for ARCC          |
| `@pc top 10`                     | Top-10 riskiest BDCs for today                |
| `@pc digest`                     | Universe-wide digest                          |
| `@pc alerts --severity critical` | Today's critical alerts                       |
| `@pc`                            | `pc help` output                              |
| `@idvault status`                | Reports dir + known_faces count               |
| `https://example.com/article`    | WeChat draft (default, unchanged)             |
| `@wechat draft https://…`        | WeChat draft (explicit, same result)          |

Locally, you can exercise the CLIs without WhatsApp:

```bash
cd ~/Documents/projects/PriCredit
./scripts/pc status ARCC
./scripts/pc top 10
./scripts/pc alerts --severity critical

cd ~/Documents/projects/IDValut
./scripts/iv status
```

## Outbound alerts (push notifications)

Enable WhatsApp push alerts from the daily pipeline:

1. Edit `ingest/notifications.yaml`:
   ```yaml
   whatsapp:
     enabled: true
     to:
       - "+18586039367"
     min_severity_tier: high
   ```
2. Run the daily pipeline with `--send-whatsapp`:
   ```bash
   ./scripts/run-daily-pricredit.sh 2026-04-18 --send-whatsapp --digest
   ```
3. Idempotency: each alert is marked sent in a `.whatsapp.json` file
   next to the alert, so re-running the pipeline does not duplicate
   messages.

## Safety model

- **Sender allowlist** is enforced at the OpenClaw gateway level via
  `channels.whatsapp.allowFrom`. The router does not relax it.
- **Argv, not shell.** The router skill passes message tokens as argv
  to `pc` / `iv`. User text is never concatenated into a shell string.
- **CLIs are read-only in v0.** Neither `pc.py` nor `iv.py` writes
  files, triggers the pipeline, or touches EDGAR. A future "kick off
  the pipeline from WhatsApp" feature (e.g. `@pc run`) would require a
  new skill with its own authorization gate — do not extend the router.
- **60-second timeout** on every dispatch. Long-running commands are
  killed and the user gets a `timeout` reply.

## Troubleshooting

**Symptom:** `@pc status ARCC` triggers a WeChat draft instead of a
reply, or the agent says "I don't have access to PriCredit".
**Fix:** Most likely the `AGENTS.md` patch is missing. Check for the
marker block:
```bash
grep -n "BEGIN openclaw-pc-router" ~/Documents/projects/wechat/AGENTS.md
```
If missing, re-run `scripts/install-openclaw-skill.sh` — the
SKILL.md is necessary but not sufficient; the `main` agent's system
prompt (AGENTS.md) must reference it explicitly for the LLM to
activate it.

Dry-run locally to isolate the problem (no WhatsApp round-trip):
```bash
openclaw agent --agent main --message "@pc status ARCC"
```

**Symptom:** `openclaw skills list` prints
`[skills] Skipping skill path that resolves outside its configured root`.
**Fix:** A previous install used `--symlink`. Run
`scripts/install-openclaw-skill.sh --uninstall && scripts/install-openclaw-skill.sh`
to replace it with a copy.

**Symptom:** `@pc` replies with `invalid ticker`.
**Fix:** Send `@pc help` to see the command list. Tickers must match
`^[A-Z][A-Z0-9.\-]{0,9}$`.

**Symptom:** `pc date` picks the wrong `reports/<DATE>/` folder.
**Fix:** Pass the date explicitly, e.g. `@pc status ARCC 2026-04-18`
is not yet supported — use the local CLI with `--date 2026-04-18-final`
or move the stale folder out of `reports/`.

**Symptom:** Outbound alerts say `openclaw: command not found`.
**Fix:** Export `OPENCLAW_CMD=/absolute/path/to/openclaw` before
running the daily pipeline, or add it to PATH.
