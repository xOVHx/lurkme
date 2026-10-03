#!/usr/bin/env bash
# lurkme installer: runs the bot as a systemd service on a Linux VPS.
#
#   git clone https://github.com/xOVHx/lurkme.git
#   sudo bash lurkme/deploy/install.sh
#
# It installs the code into /opt/lurkme, asks for your Twitch credentials
# (stored in /etc/lurkme/lurkme.env, readable only by root and the bot), and
# starts the service. The bot starts on boot and restarts itself if it crashes.
#
# Re-run any time to update to the latest code:   sudo bash /opt/lurkme/deploy/install.sh
#   --reconfigure   enter the credentials and settings again
#   --test-discord  send a sample gift alert to the Discord webhook
#   --uninstall     stop the service and remove the bot and its saved credentials
#
# Environment overrides: REPO_URL, BRANCH (which git branch to run).
# For unattended installs, CLIENT_ID, CLIENT_SECRET, OAUTH_TOKEN, REFRESH_TOKEN, CHANNELS,
# STREAM_LANGUAGES, CATEGORIES, DISCORD_WEBHOOK_URL and DISCORD_USER_ID are used instead
# of prompting (or create /etc/lurkme/lurkme.env first; see .env.example).

set -euo pipefail

APP=lurkme
SERVICE_USER=lurkme
INSTALL_DIR=/opt/lurkme
VENV_DIR=$INSTALL_DIR/.venv
ENV_DIR=/etc/lurkme
ENV_FILE=$ENV_DIR/lurkme.env
UNIT_FILE=/etc/systemd/system/$APP.service
DEFAULT_REPO_URL=https://github.com/xOVHx/lurkme.git
DEFAULT_BRANCH=main
EXIT_CONFIG=78  # lurker_bot.py exits with this when credentials or settings need fixing

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

has_systemd() { [[ -d /run/systemd/system ]]; }
trim() { local s=$1; s=${s#"${s%%[![:space:]]*}"}; printf '%s' "${s%"${s##*[![:space:]]}"}"; }

# ── Prerequisites ──────────────────────────────────────────────────────────────

# Prints the first Python 3.10+ that can create virtualenvs (the bot is tested on 3.10–3.14).
find_python() {
    local py
    for py in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        command -v "$py" >/dev/null 2>&1 || continue
        "$py" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || continue
        command -v "$py"
        return 0
    done
    return 1
}

install_packages() {
    if command -v git >/dev/null 2>&1 && find_python >/dev/null; then
        return 0
    fi
    say "Installing git and Python"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates python3 python3-venv >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q git python3
        find_python >/dev/null || dnf install -y -q python3.12  # RHEL/Rocky/Alma 9 ship 3.9 as python3
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q git python3
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm --needed git python
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install git python3
    else
        die "Couldn't find a package manager. Install git and Python 3.10+ yourself, then re-run."
    fi
    command -v git >/dev/null 2>&1 || die "git still isn't installed."
    find_python >/dev/null || die "Python 3.10 or newer (with venv support) is required, and none was found."
}

ensure_user() {
    id -u "$SERVICE_USER" >/dev/null 2>&1 && return 0
    say "Creating system user '$SERVICE_USER'"
    local nologin
    nologin=$(command -v nologin || echo /usr/sbin/nologin)
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell "$nologin" "$SERVICE_USER"
}

# ── Code ───────────────────────────────────────────────────────────────────────

# Install from the checkout this script lives in (its origin URL and current branch),
# so `git clone -b some-branch ...` deploys that branch. REPO_URL / BRANCH override it.
detect_source() {
    local src
    src=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    # safe.directory: we run as root, but the checkout usually belongs to your login user
    local git_src=(git -c safe.directory="$src" -C "$src")
    if [[ -z ${REPO_URL:-} ]]; then
        REPO_URL=$("${git_src[@]}" remote get-url origin 2>/dev/null || echo "$DEFAULT_REPO_URL")
    fi
    if [[ -z ${BRANCH:-} ]]; then
        BRANCH=$("${git_src[@]}" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "$DEFAULT_BRANCH")
    fi
}

fetch_code() {
    if [[ -d $INSTALL_DIR/.git ]]; then
        say "Updating $INSTALL_DIR to the latest '$BRANCH'"
        git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
        git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" \
            || die "Couldn't fetch branch '$BRANCH' from $REPO_URL. If it was merged and deleted, re-run with: sudo BRANCH=main bash $INSTALL_DIR/deploy/install.sh"
        if [[ $(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD || true) != "$BRANCH" ]]; then
            git -C "$INSTALL_DIR" checkout --quiet -B "$BRANCH" FETCH_HEAD
        fi
        git -C "$INSTALL_DIR" merge --quiet --ff-only FETCH_HEAD \
            || die "Local changes in $INSTALL_DIR block the update. Commit or discard them (git -C $INSTALL_DIR status), then re-run."
    else
        [[ -e $INSTALL_DIR ]] && die "$INSTALL_DIR exists but isn't a git checkout. Move it aside and re-run."
        say "Downloading lurkme ($BRANCH) into $INSTALL_DIR"
        git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
    # Root owns the code; the service can read it but not modify it
    chown -R root:root "$INSTALL_DIR"
    chmod -R u=rwX,go=rX "$INSTALL_DIR"
}

setup_venv() {
    local py
    py=$(find_python)
    if [[ -e $VENV_DIR ]] && ! "$VENV_DIR/bin/python" -c 'import pip' >/dev/null 2>&1; then
        say "Rebuilding the virtualenv (the system Python changed)"
        rm -rf "$VENV_DIR"
    fi
    if [[ ! -x $VENV_DIR/bin/python ]]; then
        say "Creating virtualenv with $("$py" --version)"
        "$py" -m venv "$VENV_DIR"
    fi
    say "Installing Python packages"
    "$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
    chmod -R u=rwX,go=rX "$VENV_DIR"
}

# ── Credentials ────────────────────────────────────────────────────────────────

# The value of VAR saved in the env file, if any (quoted or not)
current() {
    [[ -f $ENV_FILE ]] || return 0
    trim "$(sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}[[:space:]]*\$/\1/p" "$ENV_FILE" | tail -n 1)"
}

# ask VAR "Prompt" plain|secret|optional|optional-secret PATTERN [DEFAULT]
# Takes $VAR from the environment if set, otherwise prompts (Enter = DEFAULT).
# Without a terminal it uses the default, and fails only if a required value is missing.
ask() {
    local var=$1 prompt=$2 kind=$3 pattern=$4 default=${5:-} value=${!1:-} hint=
    if [[ -n $default ]]; then
        if [[ $kind == *secret ]]; then hint=" [Enter keeps the current one]"; else hint=" [$default]"; fi
    fi
    while :; do
        if [[ -z $value ]]; then
            if [[ ! -t 0 ]]; then
                value=$default
            else
                if [[ $kind == *secret ]]; then
                    read -r -s -p "  $prompt$hint: " value; echo
                else
                    read -r -p "  $prompt$hint: " value
                fi
                value=$(trim "$value")
                [[ -z $value ]] && value=$default
                [[ $kind == optional* && $value == - ]] && value=  # "-" clears an optional setting
            fi
        fi
        if [[ -z $value && $kind != optional* ]]; then
            [[ -t 0 ]] || die "$var isn't set and there's no terminal to ask for it. Run the installer from an interactive SSH session."
            echo "    This one is required."
        elif [[ -z $value || $value =~ ^$pattern$ ]]; then
            printf -v "$var" '%s' "$value"
            return 0
        else
            if [[ ! -t 0 ]]; then
                [[ $kind == *secret ]] && die "$var has an invalid value."  # Never print a secret
                die "$var has an invalid value: $value"
            fi
            echo "    That doesn't look right, try again."
        fi
        value=
    done
}

configure() {
    say "Twitch credentials"
    cat <<'EOF'
  Generate these on your own computer first (see README.md, "Setup"):
  your app's Client ID and Secret, then a user token made with that same app:
      twitch token -u -s "chat:read user:read:follows"
  Secret values stay hidden while you type or paste them.
EOF
    ask CLIENT_ID     "Client ID"         plain  '[A-Za-z0-9]+'          "$(current CLIENT_ID)"
    ask CLIENT_SECRET "Client Secret"     secret '[A-Za-z0-9]+'          "$(current CLIENT_SECRET)"
    ask OAUTH_TOKEN   "User Access Token" secret '(oauth:)?[A-Za-z0-9]+' "$(current OAUTH_TOKEN)"
    ask REFRESH_TOKEN "Refresh Token"     secret '[A-Za-z0-9]+'          "$(current REFRESH_TOKEN)"

    say "Optional settings: Enter keeps the value in [brackets], - clears it"
    local langs
    langs=$(current STREAM_LANGUAGES)
    ask CHANNELS         "Channels to always join, comma-separated"  optional '[A-Za-z0-9_#, ]*' "$(current CHANNELS)"
    ask STREAM_LANGUAGES "Stream languages, comma-separated"         optional '[A-Za-z, ]*'      "${langs:-en}"
    ask CATEGORIES       "Categories, comma-separated (blank = all)" optional '[^"\\$`]*'      "$(current CATEGORIES)"

    say "Discord gift alerts (optional)"
    cat <<'EOF'
  Paste a channel webhook URL (Server Settings > Integrations > Webhooks) to get a
  message whenever someone gifts you a sub. To be pinged, add your numeric user ID:
  Settings > Advanced > Developer Mode on, then right-click your name > Copy User ID.
EOF
    ask DISCORD_WEBHOOK_URL "Webhook URL (blank = no alerts)"        optional-secret \
        'https://([a-z]+\.)?discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+' "$(current DISCORD_WEBHOOK_URL)"
    ask DISCORD_USER_ID     "Your Discord user ID (blank = no ping)" optional '[0-9]{15,21}' "$(current DISCORD_USER_ID)"

    write_env_file
}

write_env_file() {
    local tmp
    install -d -m 750 -o root -g "$SERVICE_USER" "$ENV_DIR"
    tmp=$(mktemp "$ENV_DIR/.lurkme.env.XXXXXX")
    {
        echo "# lurkme settings, written by deploy/install.sh. Edit, then: sudo systemctl restart $APP"
        for var in CLIENT_ID CLIENT_SECRET OAUTH_TOKEN REFRESH_TOKEN CHANNELS STREAM_LANGUAGES CATEGORIES \
                   DISCORD_WEBHOOK_URL DISCORD_USER_ID; do
            printf '%s="%s"\n' "$var" "${!var:-}"
        done
    } >"$tmp"
    chown root:"$SERVICE_USER" "$tmp"
    chmod 640 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
    say "Saved settings to $ENV_FILE"
}

# ── Service ────────────────────────────────────────────────────────────────────

write_unit() {
    cat >"$UNIT_FILE" <<EOF
[Unit]
Description=lurkme Twitch lurker bot
Documentation=https://github.com/xOVHx/lurkme
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=LURKME_ENV_FILE=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/lurker_bot.py

# Restart on any crash, but not when the bot says its credentials need fixing
Restart=always
RestartSec=10
RestartPreventExitStatus=$EXIT_CONFIG

# Lock the bot down: it only needs to read its code and settings and talk to Twitch
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
LockPersonality=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
UMask=0077
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$UNIT_FILE"
}

start_service() {
    systemctl daemon-reload
    systemctl enable --quiet "$APP"
    say "Starting the bot"
    local since
    since=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart "$APP"

    # Wait for it to log in (or fail) so problems show up right here
    for _ in $(seq 1 30); do
        sleep 1
        if journalctl -u "$APP" --since "$since" -q -o cat 2>/dev/null | grep -q '^\[ready\]'; then
            say "lurkme is running"
            break
        fi
        if ! systemctl is-active --quiet "$APP"; then
            warn "lurkme stopped right after starting. Recent log:"
            journalctl -u "$APP" -n 20 --no-pager -o cat >&2 || true
            [[ $(systemctl show -p ExecMainStatus --value "$APP") == "$EXIT_CONFIG" ]] \
                && echo "Fix the credentials with: sudo bash $INSTALL_DIR/deploy/install.sh --reconfigure" >&2
            exit 1
        fi
    done
    journalctl -u "$APP" -n 15 --no-pager -o cat || true
}

print_help() {
    cat <<EOF

Useful commands:
  Live log:          journalctl -u $APP -f
  Status:            systemctl status $APP
  Restart:           sudo systemctl restart $APP
  Update:            sudo bash $INSTALL_DIR/deploy/install.sh
  New tokens:        sudo bash $INSTALL_DIR/deploy/install.sh --reconfigure
  Test Discord:      sudo bash $INSTALL_DIR/deploy/install.sh --test-discord
  Uninstall:         sudo bash $INSTALL_DIR/deploy/install.sh --uninstall
EOF
}

uninstall() {
    local reply
    read -r -p "Remove lurkme, its service and its saved credentials? [y/N] " reply
    [[ $reply == [yY]* ]] || die "Cancelled."
    if has_systemd; then
        systemctl disable --now --quiet "$APP" 2>/dev/null || true
    fi
    rm -f "$UNIT_FILE"
    has_systemd && systemctl daemon-reload
    rm -rf "$INSTALL_DIR" "$ENV_DIR"
    id -u "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER"
    say "lurkme has been removed"
}

# ── Main ───────────────────────────────────────────────────────────────────────

main() {
    local reconfigure=false
    case ${1:-} in
        "")            ;;
        --reconfigure) reconfigure=true ;;
        --test-discord)
            [[ $EUID -eq 0 ]] || die "Run this with sudo."
            [[ -x $VENV_DIR/bin/python && -f $ENV_FILE ]] || die "Install lurkme first."
            # Run as the service user, exactly like the service does
            runuser -u "$SERVICE_USER" -- env LURKME_ENV_FILE="$ENV_FILE" \
                "$VENV_DIR/bin/python" "$INSTALL_DIR/lurker_bot.py" --test-discord
            exit ;;
        --uninstall)   [[ $EUID -eq 0 ]] || die "Run this with sudo."; uninstall; exit 0 ;;
        -h|--help)     awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)             die "Unknown option: $1 (try --help)" ;;
    esac
    [[ $EUID -eq 0 ]] || die "Run this with sudo: sudo bash $0"

    detect_source
    install_packages
    ensure_user
    fetch_code
    setup_venv

    if $reconfigure || [[ ! -f $ENV_FILE ]]; then
        configure
    else
        say "Keeping existing settings in $ENV_FILE (use --reconfigure to change them)"
    fi
    # The bot runs as $SERVICE_USER and must be able to read its settings, even if
    # someone created the file by hand with different permissions
    chown root:"$SERVICE_USER" "$ENV_DIR" "$ENV_FILE"
    chmod 750 "$ENV_DIR"
    chmod 640 "$ENV_FILE"

    write_unit
    if ! has_systemd; then
        warn "systemd isn't running here, so the service wasn't started. Run the bot by hand with:"
        echo "  sudo -u $SERVICE_USER LURKME_ENV_FILE=$ENV_FILE $VENV_DIR/bin/python $INSTALL_DIR/lurker_bot.py" >&2
        exit 2
    fi
    start_service
    print_help
}

# Everything runs from main() so bash has read the whole file before a `git pull` can change it
main "$@"
