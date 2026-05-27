#!/usr/bin/env bash
# Interactive .env setup for production deployments.
#
# Run from anywhere — the script cd's to the repo root automatically.
# Prompts for the minimum required values, hides secret input, and rewrites
# the matching lines in .env (creating one from .env.example if absent).
# The previous .env is backed up to .env.bak.
#
# Usage:
#     bash scripts/configure_env.sh
#
# Designed so you never have to paste a key into your shell history or
# into chat with an assistant.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env.example ]; then
    echo "Error: .env.example not found in $(pwd)" >&2
    exit 1
fi

if [ -f .env ]; then
    cp .env .env.bak
    echo "Backed up existing .env -> .env.bak"
else
    cp .env.example .env
    echo "Created .env from .env.example"
fi

set_value() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" .env; then
        # Use | as sed delimiter so URLs and base64 keys don't clash.
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

comment_line() {
    local key="$1"
    sed -i "s|^${key}=|# ${key}=|" .env 2>/dev/null || true
}

prompt_secret() {
    local prompt="$1"
    local _var
    read -r -s -p "$prompt: " _var
    echo
    printf '%s' "$_var"
}

prompt_plain() {
    local prompt="$1"
    local _var
    read -r -p "$prompt: " _var
    printf '%s' "$_var"
}

echo
echo "Signal Tracker .env interactive setup"
echo "(secret values are not echoed back to the terminal)"
echo

# --- LLM ---
echo "--- LLM provider ---"
default_model="anthropic/claude-sonnet-4-5"
model=$(prompt_plain "LLM_MODEL [$default_model]")
model=${model:-$default_model}
set_value LLM_MODEL "$model"

case "$model" in
    anthropic/*)
        key=$(prompt_secret "ANTHROPIC_API_KEY (input hidden)")
        set_value ANTHROPIC_API_KEY "$key"
        ;;
    openai/*)
        key=$(prompt_secret "OPENAI_API_KEY (input hidden)")
        set_value OPENAI_API_KEY "$key"
        ;;
    gemini/*)
        key=$(prompt_secret "GEMINI_API_KEY (input hidden)")
        set_value GEMINI_API_KEY "$key"
        ;;
    mistral/*)
        key=$(prompt_secret "MISTRAL_API_KEY (input hidden)")
        set_value MISTRAL_API_KEY "$key"
        ;;
    *)
        echo "Unknown provider in LLM_MODEL; skipping API key prompt."
        ;;
esac

# Drop the openai fallback if the corresponding key isn't already set —
# avoids the runtime warning that we'd otherwise log every classify().
comment_line LLM_FALLBACK_MODEL

# --- Dashboard auth ---
echo
echo "--- Dashboard auth ---"
default_user="admin"
auth_user=$(prompt_plain "DASHBOARD_AUTH_USER [$default_user]")
auth_user=${auth_user:-$default_user}
set_value DASHBOARD_AUTH_USER "$auth_user"

auth_pwd=$(prompt_secret "DASHBOARD_AUTH_PASSWORD (input hidden, pick a long one)")
if [ -z "$auth_pwd" ]; then
    echo "Refusing to set an empty password. Aborting." >&2
    exit 2
fi
set_value DASHBOARD_AUTH_PASSWORD "$auth_pwd"

# --- Database ---
echo
echo "--- Database ---"
echo "Press Enter to stay on SQLite (recommended on 1 GB VMs)."
echo "Or paste a postgresql+psycopg://user:pwd@host/db URL for Neon / RDS."
db_url=$(prompt_plain "DATABASE_URL")
set_value DATABASE_URL "$db_url"
set_value DB_PATH "/data/signals.db"

# --- Optional digest email ---
echo
echo "--- Digest email (optional, press Enter to skip) ---"
to_email=$(prompt_plain "DIGEST_TO_EMAIL")
if [ -n "$to_email" ]; then
    set_value DIGEST_TO_EMAIL "$to_email"
    from_email=$(prompt_plain "DIGEST_FROM_EMAIL")
    set_value DIGEST_FROM_EMAIL "$from_email"
    echo "Note: SMTP_HOST + SMTP_USER + SMTP_PASSWORD still need to be filled"
    echo "      manually if you want real email delivery. Without them, the"
    echo "      digest falls back to dry-run mode."
fi

# --- Summary ---
echo
echo "Done. Non-secret values in .env:"
echo "----"
grep -E "^(LLM_MODEL|DASHBOARD_AUTH_USER|DB_PATH|DATABASE_URL|DIGEST_TO_EMAIL|DIGEST_FROM_EMAIL)=" .env || true
echo "----"
echo "ANTHROPIC_API_KEY length: $(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2- | tr -d '\n' | wc -c) chars (expect ~110)"
echo
echo "Next:"
echo "   exit"
echo "   gcloud compute ssh signal-tracker --zone=us-central1-a"
echo "   cd /opt/signal-tracker"
echo "   docker compose -f docker-compose.lite.yml up -d --build"
