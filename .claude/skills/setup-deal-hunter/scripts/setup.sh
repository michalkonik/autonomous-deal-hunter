#!/usr/bin/env bash
# setup.sh - one-shot probes for /setup-deal-hunter. Prints KEY=value lines, one per fact, plus WARN/FAIL lines.
#   setup.sh check      environment: python, venv, systemd user bus, RAM, disk, llama-swap, chrome, bw-secret
#   setup.sh install    create .venv and install the package (idempotent)
#   setup.sh models     llama-swap's running + known models vs the configured roles
#   setup.sh secrets    which secrets are present and parse as JSON (values are NEVER printed)
#   setup.sh verify    is the agent up: unit, status, health, web page (port read from config)
# Exit 0 = usable, 3 = a blocker was printed on a FAIL line.
set -uo pipefail

R=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../../.." && pwd)
VENV="$R/.venv"
PY="$VENV/bin/python"
SWAP=${DH_SWAP_URL:-http://localhost:8080}
rc=0
fail() { echo "FAIL $1"; rc=3; }
warn() { echo "WARN $1"; }

user_bus() {  # systemctl --user needs the D-Bus environment; an agent shell often lacks it
    [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] && return 0
    local rt=/run/user/$(id -u)
    if [ -S "$rt/bus" ]; then
        export XDG_RUNTIME_DIR=$rt DBUS_SESSION_BUS_ADDRESS=unix:path=$rt/bus
        echo "user-bus=exported (add XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS to later commands)"
        return 0
    fi
    return 1
}
user_bus || true   # best effort for every subcommand; `check` reports it as a blocker

case "${1:-check}" in
check)
    echo "repo=$R"
    [ -f "$R/pyproject.toml" ] || { fail "not the Deal Hunter repo (no pyproject.toml at $R)"; exit $rc; }
    if [ -f "$VENV/bin/activate" ]; then echo "venv=present"; else echo "venv=absent"; fi
    for c in python3.12 python3.13 python3; do
        p=$(command -v $c 2>/dev/null) || continue
        v=$($p -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null) || continue
        echo "python-found=$p $v"
    done
    if ! command -v python3.12 >/dev/null && ! command -v python3.13 >/dev/null; then
        warn "no python3.12/3.13 on PATH (requires-python >= 3.12). $p reported $($p --version 2>&1). Install 3.12 or use a matching interpreter for the venv."
    fi
    systemctl --user is-system-running >/dev/null 2>&1 || user_bus
    if systemctl --user is-system-running >/dev/null 2>&1; then
        echo "systemd-user=$(systemctl --user is-system-running 2>&1)"
    else
        fail "systemctl --user is unusable (no user bus). `dh start` needs a systemd user session: loginctl enable-linger $USER, and export XDG_RUNTIME_DIR=/run/user/$(id -u) and DBUS_SESSION_BUS_ADDRESS=unix:path=\$XDG_RUNTIME_DIR/bus"
    fi
    # compare against the CONFIGURED floor (config/models.yaml, overridden by config/local/models.yaml), not 12
    need=$(grep -h '^min_free_ram_gb:' "$R"/config/models.yaml "$R"/config/local/models.yaml 2>/dev/null | tail -1 | awk '{print $2}')
    need=${need:-12}
    awk -v need="$need" '/MemAvailable/ {printf "ram-free-gb=%.1f\n", $2/1048576;
        if ($2/1048576 < need) print "WARN free RAM below models.min_free_ram_gb (" need "): dh start will refuse until the other workload finishes or the floor is lowered in config/local/models.yaml"}' /proc/meminfo
    df -Pk "$R" | awk 'NR==2 {printf "disk-free-gb=%.0f\n", $4/1048576}'
    echo "bw-secret=$(command -v bw-secret || echo absent)"
    echo "chrome=$([ -x /usr/bin/google-chrome ] && echo present || echo 'absent (tier-1 evidence screenshots are skipped; the agent still works)')"
    command -v rocm-smi >/dev/null && echo "rocm-smi=present" || echo "rocm-smi=absent (energy accounting counts base_w only)"
    if curl -s --max-time 5 "$SWAP/running" >/dev/null 2>&1; then
        echo "llama-swap=up"
        curl -s --max-time 5 "$SWAP/running" | head -c 400 | tr -d '\n' | sed 's/^/running-raw=/'
        echo
    else
        warn "llama-swap not reachable at $SWAP: rounds run on the rules matcher only (no product extraction, no triage). Point models.base_url at any OpenAI-compatible endpoint, or run rules-only for now."
    fi
    ;;
install)
    PYV=$(command -v python3.12 || command -v python3.13 || echo python3)
    [ -d "$VENV" ] || "$PYV" -m venv "$VENV" || { fail "python3 -m venv failed (apt install python3.12-venv)"; exit $rc; }
    "$VENV/bin/pip" install -q -U pip || fail "pip upgrade failed"
    (cd "$R" && "$VENV/bin/pip" install -q -e '.[dev]') || { fail "pip install -e '.[dev]' failed"; exit $rc; }
    "$VENV/bin/dh" --help >/dev/null 2>&1 || fail "the dh entry point did not install"
    "$PY" -c "import dh.config, dh.runtime, dh.scheduler, httpx, starlette, uvicorn, PIL, imagehash, feedparser, selectolax, playwright; print('imports=ok')" || fail "a dependency is missing"
    echo "installed=$("$VENV/bin/python" -c 'import dh;print(getattr(dh,"__version__","0.1.0"))' 2>/dev/null || echo 0.1.0)"
    echo "dh=$VENV/bin/dh"
    echo "next: run the tests ('$VENV/bin/pytest -q', in the background), then setup.sh models"
    ;;
models)
    "$PY" - "$R/config/models.yaml" "$R/config/local/models.yaml" <<'PY' || echo "roles=<unreadable: run install first>"
import os, sys
try:
    import yaml
except ImportError:
    print("roles=pyyaml-missing"); raise SystemExit(0)
def load(p):
    return (yaml.safe_load(open(p)) or {}) if os.path.exists(p) else {}
cfg, local = load(sys.argv[1]), load(sys.argv[2])
cfg = {**cfg, **{k: v for k, v in local.items() if not isinstance(v, dict)}}
for k, v in local.items():
    if isinstance(v, dict):
        cfg[k] = {**(cfg.get(k) or {}), **v}
print("base_url=" + str(cfg.get("base_url")))
for role, model in (cfg.get("roles") or {}).items():
    print(f"role-{role}={model}")
print(f"min_free_ram_gb={cfg.get('min_free_ram_gb', 12)}")
PY
    echo "loaded=$(curl -s --max-time 5 "$SWAP/running" | python3 -c 'import json,sys;print(",".join(m["model"] for m in json.load(sys.stdin).get("running",[])))' 2>/dev/null || echo unreachable)"
    echo "known=$(curl -s --max-time 5 "$SWAP/v1/models" | python3 -c 'import json,sys;print(",".join(m["id"] for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo unknown)"
    [ -f "$R/config/local/models.yaml" ] && echo "local-models-overrides=present" || echo "local-models-overrides=absent"
    ;;
secrets)
    for ref in ebay#client_id ebay#client_secret ebay#dev_id \
               ebay2#client_id ebay2#client_secret ebay2#dev_id \
               allegro#client_id allegro#client_secret dh-mail#imap dh-mail#smtp; do
        if ! command -v bw-secret >/dev/null; then echo "$ref=helper-absent"; continue; fi
        if ! v=$(bw-secret "$ref" 2>/dev/null); then echo "$ref=absent"; continue; fi
        case "$ref" in
        *#imap | *#smtp)
            if printf '%s' "$v" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert all(k in d for k in ("host","port","user","password")), "missing keys"' 2>/dev/null; then
                echo "$ref=present,valid-json"
            else
                echo "$ref=present,BAD-JSON (needs {\"host\",\"port\",\"user\",\"password\"[,\"folder\"|\"to\"]})"
            fi ;;
        *) echo "$ref=present,${#v}-chars" ;;
        esac
    done
    ;;
access)
    # Which sources are actually live RIGHT NOW, and why the rest are not. A disabled source is invisible in
    # `dh status` (no adapter, so no health event): only this matrix proves what the agent cannot see.
    "$PY" - "$R" <<'PY' || echo "access=<unreadable: run install first>"
import shutil, subprocess, sys
from pathlib import Path
try:
    import yaml
except ImportError:
    print("access=pyyaml-missing"); raise SystemExit(0)
root = Path(sys.argv[1])

def deep(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

def merged(stem):
    d = {}
    for p in sorted((root / "config").glob("*.yaml")):
        if p.stem == stem:
            d = yaml.safe_load(p.read_text()) or {}
    f = root / "config" / "local" / f"{stem}.yaml"
    return deep(d, yaml.safe_load(f.read_text()) or {}) if f.is_file() else d

ad = (merged("sources").get("adapters") or {})
def secret(ref):
    return bool(shutil.which("bw-secret")) and subprocess.run(["bw-secret", ref], capture_output=True).returncode == 0
have_imap = secret("dh-mail#imap")
have_ebay = secret("ebay#client_id") and secret("ebay#client_secret")
have_ebay2 = secret("ebay2#client_id") and secret("ebay2#client_secret")
have_allegro = secret("allegro#client_id") and secret("allegro#client_secret")
live = dead = 0
for name in sorted(ad):
    e = ad[name] or {}
    on, why, via = bool(e.get("enabled")), str(e.get("reason") or ""), str(e.get("via") or "http")
    if name == "mailbox":
        via = "imap"
    if on and via == "mailbox" and not have_imap:
        on, why = False, "the alert mailbox has no credentials (bw-secret dh-mail#imap): this platform is invisible"
    if name == "mailbox" and not on:
        why = why or "enabled: false in config/sources.yaml - set it true in config/local/sources.yaml once dh-mail#imap exists"
    if on and name == "mailbox" and not have_imap:
        on, why = False, "no credentials (bw-secret dh-mail#imap): the adapter is not even built, and dh status stays clean"
    if on and name == "allegro" and not have_allegro:
        on, why = False, "no Allegro keys (bw-secret allegro#client_id / allegro#client_secret)"
    elif on and via == "api" and name != "allegro" and not have_ebay:
        on, why = False, "no eBay keys (bw-secret ebay#client_id / ebay#client_secret)"
    live += on; dead += (not on)
    print(f"source={name} live={'yes' if on else 'no'} via={via}" + (f" reason={why}" if why else ""))
print(f"live={live} not-live={dead}")
if not have_imap:
    print("access-gap dh-mail#imap: OLX.pl is mailbox-only (it blocks polite polling). Allegro, Kleinanzeigen "
          "and eBay also send saved-search alert mail - ask the owner for the mailbox now")
if not have_allegro:
    print("access-gap allegro#client_id / allegro#client_secret: allegro.pl public search is off - ask for both. "
          "The registered User-Agent is already the default; override adapters.allegro.user_agent before the first "
          "call if theirs differs. /offers/listing may return not_verified until Allegro grants the app")
if not have_ebay:
    print("access-gap ebay#client_id / ebay#client_secret: eBay DE and PL are off (API only) - ask how many "
          "applications they have and store every set (ebay, then ebay2 … ebay8)")
elif not have_ebay2:
    print("access-gap ebay2#client_id / ebay2#client_secret: only one eBay application is stored - ask whether "
          "more exist; a quota on one key switches to the next")
if not Path("/usr/bin/google-chrome").exists():
    print("access-gap /usr/bin/google-chrome: tier-1 evidence screenshots are skipped")
print("note: mailbox-sourced listings are never re-checked live and top out at tier 2. Allegro REST offers "
      "also have no per-offer re-check, so they cannot reach tier 1 (docs/SOURCES.md)")
PY
    ;;
verify)
    cd "$R" || exit 3
    [ -x "$VENV/bin/dh" ] || { fail "no .venv: run setup.sh install"; exit $rc; }
    "$VENV/bin/dh" status 2>&1 | sed 's/^/status: /'
    port=$("$VENV/bin/python" - <<'PY' 2>/dev/null
import yaml, pathlib
for p in ("config/local/web.yaml", "config/web.yaml"):
    f = pathlib.Path(p)
    if f.is_file():
        v = (yaml.safe_load(f.read_text()) or {}).get("port")
        if v:
            print(v); break
else:
    print(8711)
PY
)
    port=${port:-8711}
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$port/" 2>/dev/null || echo 000)
    [ "$code" = 200 ] && echo "web-$port=200" || echo "web-$port=$code (expected 200; check config/local/web.yaml and the unit)"
    "$VENV/bin/dh" status --json 2>/dev/null | python3 -c '
import json,sys
try: s=json.load(sys.stdin)
except Exception: raise SystemExit(0)
h=s.get("health")
if isinstance(h, dict):
    print("health=" + json.dumps({k: v for k, v in h.items() if v}))
elif isinstance(h, list):
    print("health=" + json.dumps([(x.get("kind") if isinstance(x, dict) else x) for x in h][-12:]))
print("paused=" + str(bool(s.get("paused"))))
print("rounds-since-start=" + str(s.get("rounds_since_start", 0)))
' 2>/dev/null
    ;;
mailbox)
    # The alert mailbox, verified rather than described: credentials, the folder, the unseen count, and for each
    # unseen mail whether the platform and its DKIM/SPF proof would be accepted. Read-only: nothing is marked read.
    cd "$R" || exit 3
    [ -x "$PY" ] || { fail "no .venv: run setup.sh install"; exit $rc; }
    "$PY" - <<'PY' || rc=3
import email
from email import policy
from dh.notify import bw_secret
from dh.sources.mailbox import Mailbox, authenticated, platform_of
raw = bw_secret("dh-mail#imap")
if not raw:
    print("FAIL dh-mail#imap is absent: store it with  bash .claude/skills/setup-deal-hunter/scripts/put_secret.sh dh-mail#imap --stdin")
    raise SystemExit(1)
mb = Mailbox(secret_fn=lambda r: raw if r == "dh-mail#imap" else None)
c = mb._resolve()
if c is None:
    print("FAIL dh-mail#imap is not valid JSON with host/user/password"); raise SystemExit(1)
print(f"imap={c['host']}:{c['port']} user={c['user']} folder={c['folder']}")
try:
    mails = mb._fetch_unseen(c)
except Exception as e:
    print(f"FAIL cannot select the folder {c['folder']!r} ({type(e).__name__}: {e})"); raise SystemExit(1)
print(f"unseen-in-folder={len(mails)}")
platforms = set()
for uid, raw_m in mails[:25]:
    m = email.message_from_bytes(raw_m, policy=policy.default)
    pf = platform_of(m["From"] or "")
    ok = bool(pf) and authenticated(m, pf)
    print(f"mail={'accept' if ok else 'skip'} platform={pf or 'unknown'} dkim_or_spf_ok={ok} "
          f"from={(m['From'] or '')[:40]} subject={(m['Subject'] or '')[:40]}")
    if pf:
        platforms.add(pf)
if not mails:
    print("note: the folder is empty - the saved searches have not delivered anything yet (normal on day 1)")
    print("checklist: 1 a dedicated mailbox with IMAP SSL 993; 2 a Gmail filter on {allegro.pl olx.pl "
          "kleinanzeigen.de ebay.de ebay.pl} that SKIPS the inbox and APPLIES a label (never 'mark as read'); "
          "3 that label shown in IMAP and named in the folder field; 4 saved searches with e-mail alerts at the "
          "platform's most frequent setting (OWNER-TODO.md lists them); 5 adapters.mailbox.enabled: true in "
          "config/local/sources.yaml; 6 dh stop && dh start")
if platforms:
    print("platform mail seen: " + ",".join(sorted(platforms)) + " (a real alert that passes the rule means the "
          "parsers have met live mail; watch for alert_parse_empty in dh status)")
print("reminder: only UNSEEN mail in that folder is read; never read the alert mailbox by hand")
PY
    exit $rc
    ;;
*) echo "usage: setup.sh [check|install|models|secrets|access|mailbox|verify]"; exit 2 ;;
esac
exit $rc
