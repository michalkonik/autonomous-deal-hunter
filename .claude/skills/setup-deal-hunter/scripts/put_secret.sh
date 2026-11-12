#!/usr/bin/env bash
# put_secret.sh - store ONE secret for Deal Hunter without the value entering the agent's context.
#   bash put_secret.sh ebay#client_id                  # prompts the owner on their terminal (read -rs)
#   printf '%s' "$V" | bash put_secret.sh dh-mail#imap --stdin
# Two backends, chosen by what the machine has:
#   * a Bitwarden-backed bw-secret (openclaw-bw-resolver style)  -> the item is written with `bw edit`/`bw create`
#     into the folder in BW_AGENT_FOLDERS, using the agent session key (~/.config/bw/session). The vault must be
#     unlocked for agents (bw-agent-unlock). A locked vault is a LOUD failure, never a silent wait.
#   * otherwise a tiny file-backed bw-secret (~/.config/deal-hunter/secrets, mode 600) is installed,
#     so a clean machine still has secrets.
# Prints only item names and lengths - never a value.
set -uo pipefail
ref=${1:-}; [ -n "$ref" ] && [[ "$ref" == *"#"* ]] || { echo "usage: put_secret.sh <item>#<field> [--stdin]" >&2; exit 2; }
item=${ref%%#*}; field=${ref##*#}
STORE=${DEAL_HUNTER_SECRETS:-$HOME/.config/deal-hunter/secrets}
BIN=${BH_BIN:-$HOME/.local/bin/bw-secret}
stdin=0; [ "${2:-}" = "--stdin" ] && stdin=1

read_value() {  # from stdin, or hidden on the owner's terminal; empty aborts
    local v
    if [ "$stdin" = 1 ]; then v=$(cat); else
        stty -echo 2>/dev/null || true
        printf 'Value for %s (hidden; empty = abort): ' "$ref" >&2
        read -r v
        stty echo 2>/dev/null || true
        printf '\n' >&2
    fi
    [ -n "$v" ] || { echo "abort: no value given for $ref" >&2; exit 1; }
    printf '%s' "$v"
}

have_bitwarden() {  # is the existing bw-secret backed by a real vault (not our own shim)?
    [ -x "$BIN" ] && ! grep -q "# deal-hunter/crypto-agent file-backed bw-secret" "$BIN" 2>/dev/null
}

bw_session() {  # the agent session key, if one exists (never printed)
    if [ -n "${BW_SESSION:-}" ]; then printf '%s' "$BW_SESSION"
    elif [ -f "$HOME/.config/bw/session" ]; then cat "$HOME/.config/bw/session"; fi
}

if have_bitwarden; then
    BW=$(command -v bw || true)
    [ -n "$BW" ] || { echo "FAIL bw-secret is Bitwarden-backed but the bw CLI is not on PATH" >&2; exit 1; }
    command -v jq >/dev/null || { echo "FAIL jq is needed to write a Bitwarden custom field" >&2; exit 1; }
    s=$(bw_session); [ -n "$s" ] || {
        echo "FAIL the Bitwarden vault is locked for agents: run 'bw-agent-unlock' (master password) in your own" >&2
        echo "     terminal, then re-run this command. Nothing was stored - the value was NOT written anywhere." >&2
        exit 1; }
    export BW_SESSION=$s
    folder=$(printf '%s' "${BW_AGENT_FOLDERS:-agents,agent-test}" | cut -d, -f1)
    fid=$("$BW" list folders 2>/dev/null | jq -r --arg n "$folder" '.[]|select(.name==$n)|.id' | head -1)
    [ -n "$fid" ] && [ "$fid" != "null" ] || { echo "FAIL no vault folder named '$folder' (BW_AGENT_FOLDERS)" >&2; exit 1; }

    if json=$("$BW" get item "$item" 2>/dev/null); then
        id=$(printf '%s' "$json" | jq -r '.id')
        [ "$id" != "null" ] && [ -n "$id" ] || { echo "FAIL could not read the id of vault item '$item'" >&2; exit 1; }
        val=$(read_value)
        new=$(printf '%s' "$json" | jq --arg f "$field" --arg v "$val" \
             'if ((.fields // []) | map(.name) | index($f)) != null
              then .fields = (.fields | map(if .name == $f then .value = $v else . end))
              else .fields = ((.fields // []) + [{"name": $f, "type": 1, "linkedId": null, "value": $v}]) end')
        # the bw CLI wants the item id and the base64-encoded object on stdin (with a trailing newline)
        { printf '%s' "$new" | base64 -w0; echo; } | "$BW" edit item "$id" >/dev/null 2>&1 \
            || { echo "FAIL bw edit failed for $item" >&2; exit 1; }
    else
        val=$(read_value)
        new=$(jq -n --arg n "$item" --arg f "$field" --arg v "$val" --arg fid "$fid" \
              '{organizationId: null, collectionId: null, type: 1, name: $n, folderId: $fid,
                login: {username: null, password: null, totp: null, uris: []},
                fields: [{name: $f, type: 1, linkedId: null, value: $v}],
                notes: "Deal Hunter secret (setup-deal-hunter). Read at run time through bw-secret.",
                attachments: [], reprompt: 0}')
        { printf '%s' "$new" | base64 -w0; echo; } | "$BW" create item >/dev/null 2>&1 \
            || { echo "FAIL bw create failed for $item" >&2; exit 1; }
    fi
    if bw-secret "$ref" >/dev/null 2>&1; then
        echo "$ref=stored in Bitwarden item '$item' (folder $folder) and verified ($(bw-secret "$ref" | wc -c) chars)"
    else
        echo "FAIL stored but not readable through bw-secret - is the item in folder '$folder'?" >&2; exit 1
    fi
    exit 0
fi

mkdir -p "$(dirname "$STORE")" "$(dirname "$BIN")"
touch "$STORE" && chmod 600 "$STORE"
if [ ! -x "$BIN" ]; then
    cat > "$BIN" <<'SH'
#!/usr/bin/env bash
# deal-hunter/crypto-agent file-backed bw-secret - installed by the setup skill.
# One secret per line: item#field<TAB>value. Store: ~/.config/deal-hunter/secrets (mode 600).
# Prints the value; exits 1 when missing. Never chmod this file to anything wider than 600.
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: bw-secret <item>#<field>" >&2; exit 2; }
awk -F'\t' -v k="$1" '$1==k {print $2; found=1} END {exit found?0:1}' "${DEAL_HUNTER_SECRETS:-$HOME/.config/deal-hunter/secrets}"
SH
    chmod 700 "$BIN"
    echo "backend=installed file-backed bw-secret at $BIN (store $STORE, mode 600)"
else
    echo "backend=file-backed bw-secret at $BIN"
fi

value=$(read_value)
tmp=$(mktemp); chmod 600 "$tmp"
awk -F'\t' -v k="$ref" '$1!=k' "$STORE" > "$tmp"
printf '%s\t%s\n' "$ref" "$value" >> "$tmp"
mv "$tmp" "$STORE" && chmod 600 "$STORE"
unset value
if bw-secret "$ref" >/dev/null 2>&1; then
    echo "$ref=stored and verified ($(bw-secret "$ref" | wc -c) chars)"
else
    echo "$ref=stored but could not be read back - check $STORE permissions"; exit 1
fi
