# Deploying Signal Tracker on Hetzner Cloud (~€4 / month)

Hetzner is the cheapest reliable cloud host in 2026 (CX22: 2 vCPU x86, 4 GB
RAM, 40 GB SSD = €3.59/month). EU data centers (Falkenstein, Helsinki,
Nuremberg), instant provisioning, no "out of capacity" games. The deploy
script and compose stack are identical to the Oracle guide — only the
provisioning step differs.

## Architecture

Same as the Oracle guide ([overview](../DEPLOY.md)):

```
Cloudflare DNS + Tunnel  →  Hetzner CX22 VM (Ubuntu 22.04)
                            └─ docker compose: dashboard + scheduler + postgres
```

## Step 1 — Create the VM (5 min)

1. Sign up at <https://accounts.hetzner.com/signUp> (no card required for sign-up,
   only at first VM creation). EU billing.
2. Console → **Cloud → New Project → Create Server**.
3. Pick:
   - **Location**: Falkenstein, Nuremberg or Helsinki (EU). Pick the closest.
   - **Image**: Ubuntu 22.04
   - **Type**: **CX22** (€3.59/month) — 2 vCPU, 4 GB RAM, 40 GB SSD.
     CX32 (€5.99) is also fine if you want more headroom; the project comfortably
     fits in 1 GB so CX22 has plenty of margin.
   - **Volume / Network**: defaults are fine, skip.
   - **Firewall**: create a new firewall named `signal-tracker`, allow inbound
     **TCP 22 (SSH)**, **TCP 80**, **TCP 443**.
   - **SSH key**: paste your `~/.ssh/id_ed25519.pub`.
   - **Name**: `signal-tracker`.
4. **Create & Buy now**. The VM is up in ~10 seconds. Note the public IPv4.

## Step 2 — Bootstrap (10 min)

```bash
ssh root@<PUBLIC_IP>          # Hetzner uses root by default

# Create a non-root sudoer
adduser deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
exit

ssh deploy@<PUBLIC_IP>

curl -fsSL https://raw.githubusercontent.com/M13-Prime/Score_Simulator/main/scripts/deploy.sh | bash
```

The script will:
1. Install Docker, git, iptables-persistent.
2. Add `deploy` to the `docker` group.
3. Clone the repo to `/opt/signal-tracker`.
4. Copy `.env.example` to `.env` and exit, asking you to fill it in.

Edit `.env`:

```bash
cd /opt/signal-tracker
nano .env
```

Required keys:

```bash
LLM_MODEL=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...

DASHBOARD_AUTH_USER=admin
DASHBOARD_AUTH_PASSWORD=<strong password>

POSTGRES_USER=signaltracker
POSTGRES_PASSWORD=<another strong password>
POSTGRES_DB=signaltracker
```

Log out / back in (for `docker` group to apply), then re-run:

```bash
exit
ssh deploy@<PUBLIC_IP>
cd /opt/signal-tracker
bash scripts/deploy.sh
```

This builds the image and starts the 3 services. Verify:

```bash
docker compose ps
docker compose logs -f dashboard
curl -i -u admin:<password> http://127.0.0.1:8000/healthz   # -> 200
```

## Step 3 — Cloudflare Tunnel (15 min)

Same as the Oracle guide — see [`oracle.md` § Step 4](oracle.md#step-4--cloudflare-tunnel-https-without-certificates-15-min).

The only difference: Hetzner CX22 is x86_64, so download the AMD64 build of
`cloudflared`:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
    -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
```

Everything after that (`cloudflared tunnel login` / `create` / `route dns` /
service install) is identical.

## Costs

| Line | Monthly |
|------|---------|
| Hetzner CX22 (Ubuntu) | €3.59 |
| Cloudflare DNS + Tunnel | €0 |
| Domain (Cloudflare Registrar `.com`) | €0.85 |
| Anthropic API (~500 articles/day Sonnet) | ~€30–60 |
| **Fixed infra** | **€4.44** |
| **+ LLM at usage** | variable |

## Backups

Hetzner provides cheap snapshots (€0.0119 / GB / month) and automated weekly
backups (€0.72 / month for 40 GB). Enable in the console under **Backups**.

For the Postgres data specifically:

```bash
# Manual one-shot dump
docker compose exec db pg_dump -U signaltracker signaltracker \
    > /opt/signal-tracker/backups/db-$(date +%F).sql

# Optional: daily cron pushed to Cloudflare R2 (free up to 10 GB)
# See https://developers.cloudflare.com/r2/ for the S3-compatible setup.
```

## Operating commands

Identical to the Oracle guide — see the
[Operating section](oracle.md#operating-the-deployment).

## Why not Hetzner ARM?

Hetzner does offer ARM Ampere shapes (CAX11 / CAX21 — €3.79–€5.99/month) which
are also great. The CX22 (x86) is the same price as CAX11 (ARM) with similar
specs. Pick ARM only if you specifically want it; x86 is the well-trodden path
for this project.
