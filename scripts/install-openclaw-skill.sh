#!/usr/bin/env bash
# Install the PriCredit @-prefix router skill into the main OpenClaw
# agent's skills directory (by default: the `wechat` project repo,
# since that's where the `main` agent's skill stack lives).
#
# Usage:
#   scripts/install-openclaw-skill.sh                # copy into wechat/skills/
#   scripts/install-openclaw-skill.sh --symlink      # symlink instead of copy
#   scripts/install-openclaw-skill.sh --target DIR   # override target skills dir
#   scripts/install-openclaw-skill.sh --uninstall    # remove the copy/symlink
#
# Default is --copy, not --symlink, because the OpenClaw skill loader
# rejects skills whose real path resolves outside the workspace root
# ("Skipping skill path that resolves outside its configured root").
# --symlink is kept only for experimentation.
#
# Also patches <target-parent>/AGENTS.md with an idempotent routing
# block. This step is REQUIRED for the skill to activate — placing a
# SKILL.md in skills/ is necessary but not sufficient; the agent's
# system prompt (AGENTS.md) must explicitly reference it. Disable
# with --no-patch-agents-md.
#
# Safe to re-run: will replace an existing install of the same skill.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$ROOT/skills/openclaw-pc-router"
SKILL_NAME="openclaw-pc-router"

# Reasonable default: the wechat repo, since its `main` agent is the
# one bound to the shared WhatsApp channel. If the user installed the
# wechat repo elsewhere, they pass --target.
DEFAULT_TARGET="$HOME/Documents/projects/wechat/skills"

mode="copy"
uninstall=0
target="$DEFAULT_TARGET"
patch_agents_md=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --copy)               mode="copy"; shift ;;
        --symlink)            mode="symlink"; shift ;;
        --target)             target="$2"; shift 2 ;;
        --uninstall)          uninstall=1; shift ;;
        --no-patch-agents-md) patch_agents_md=0; shift ;;
        -h|--help)
            sed -n '1,/^set -euo/ s/^# \{0,1\}//p' "$0"
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -d "$SKILL_SRC" ]]; then
    echo "error: skill source not found at $SKILL_SRC" >&2
    exit 1
fi

if [[ ! -d "$target" ]]; then
    echo "error: target skills dir does not exist: $target" >&2
    echo "       pass --target <DIR> to override (e.g. ~/Documents/projects/wechat/skills)" >&2
    exit 1
fi

dest="$target/$SKILL_NAME"
# AGENTS.md lives at the workspace root (target/../AGENTS.md).
agents_md="$(dirname "$target")/AGENTS.md"
marker_begin="<!-- BEGIN openclaw-pc-router (do not edit; managed by install-openclaw-skill.sh) -->"
marker_end="<!-- END openclaw-pc-router -->"

patch_block() {
    # Remove any existing managed block, then append a fresh one.
    # Uses Python to avoid sed multiline quoting pain.
    python3 - "$agents_md" "$marker_begin" "$marker_end" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
begin, end = sys.argv[2], sys.argv[3]
body = path.read_text(encoding="utf-8") if path.exists() else ""
body = re.sub(
    re.escape(begin) + r".*?" + re.escape(end) + r"\n?",
    "",
    body,
    flags=re.DOTALL,
)
block = (
    f"{begin}\n"
    "**@-prefix routing (WhatsApp / any chat channel).** This agent handles every inbound chat message. "
    "If the first whitespace-separated token of the message is `@pc` or `@pricredit`, you **must** follow "
    "`skills/openclaw-pc-router/SKILL.md` and run the bash CLI "
    "`~/Documents/projects/PriCredit/scripts/pc <rest-of-message>`, then reply with its stdout verbatim. "
    "If the first token is `@idvault`, run `~/Documents/projects/IDValut/scripts/iv <rest-of-message>` "
    "and reply with stdout. If the first token is `@wechat`, strip it and fall through to the wechat_from_inbox "
    "/ default wechat skills. If there is no `@` prefix, behavior is unchanged — defer to wechat_from_inbox "
    "exactly as before. Never reply \"I don't have access to PriCredit\"; the CLI is on disk at the path above.\n"
    f"{end}\n"
)
if body and not body.endswith("\n"):
    body += "\n"
path.write_text(body + "\n" + block, encoding="utf-8")
PY
}

unpatch_block() {
    [[ -f "$agents_md" ]] || return 0
    python3 - "$agents_md" "$marker_begin" "$marker_end" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
begin, end = sys.argv[2], sys.argv[3]
body = path.read_text(encoding="utf-8")
new = re.sub(
    r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n?",
    "\n",
    body,
    flags=re.DOTALL,
)
if new != body:
    path.write_text(new, encoding="utf-8")
    print(f"removed router block from {path}")
else:
    print(f"no router block found in {path}")
PY
}

if [[ "$uninstall" -eq 1 ]]; then
    if [[ -L "$dest" || -e "$dest" ]]; then
        rm -rf "$dest"
        echo "removed $dest"
    else
        echo "nothing to remove at $dest"
    fi
    if [[ "$patch_agents_md" -eq 1 ]]; then
        unpatch_block
    fi
    exit 0
fi

# Replace any existing install so this script stays idempotent.
if [[ -L "$dest" || -e "$dest" ]]; then
    rm -rf "$dest"
fi

case "$mode" in
    symlink)
        ln -s "$SKILL_SRC" "$dest"
        echo "symlinked $SKILL_SRC -> $dest"
        ;;
    copy)
        cp -R "$SKILL_SRC" "$dest"
        echo "copied $SKILL_SRC -> $dest"
        ;;
esac

if [[ "$patch_agents_md" -eq 1 ]]; then
    if [[ ! -f "$agents_md" ]]; then
        echo "warning: $agents_md does not exist; creating a new one with just the routing block" >&2
        : > "$agents_md"
    fi
    patch_block
    echo "patched routing block into $agents_md"
fi

cat <<EOF

Next steps:
  1. Skills are live-discovered by the gateway; no restart needed.
     Verify the router is registered:
       openclaw skills list | grep openclaw_at_router
     You should see a "ready" row.
  2. Send yourself a test message in WhatsApp:
       @pc status ARCC
     You should get the risk snapshot back in the same thread.
  3. If the skill is not listed, check that the copy landed under
     the \`main\` agent's workspace (openclaw agents list → Workspace):
       ls $dest
EOF
