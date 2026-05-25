# Deploying Signal Tracker on Google Cloud (Always Free tier)

GCP's Always Free tier gives you **one e2-micro VM** in `us-west1`,
`us-central1`, or `us-east1`, **30 GB of standard persistent disk**, and
**5 GB of Cloud Storage** — forever, on a billing account that's never
charged as long as you stay inside the quota.

The challenge: e2-micro only has **1 GB of RAM**. Running our default stack
(dashboard + scheduler + Postgres + Docker overhead) inside that is tight.
This guide uses a leaner config: **SQLite on a persistent disk** instead of
Postgres. SQLite is plenty for single-user signal tracking
(~100 writes/day at most).

If you absolutely want managed Postgres on free tier, jump to
[Variant B: with Neon](#variant-b-keep-postgres-via-neon-still-free).

## Architecture (default = SQLite)

```
Cloudflare DNS + Tunnel  →  GCP e2-micro (Ubuntu 22.04)
                            └─ docker compose:
                               • dashboard  (FastAPI)
                               • scheduler  (APScheduler)
                               • SQLite file on /data (persistent disk)
```

## Step 1 — Set up the GCP project (10 min)

1. Go to <https://console.cloud.google.com/> and sign in.
2. **Billing**: create a billing account if you don't have one. A card is
   required but **nothing is charged** as long as you stay in the Always
   Free quota. Set a budget alert at €1 in **Billing → Budgets & alerts**
   for peace of mind.
3. **New Project**: top bar → **Select a project → NEW PROJECT** → name it
   `signal-tracker` → **Create**.
4. **Enable APIs**: search bar → "Compute Engine API" → **Enable**.
5. Wait ~30 seconds for activation.

## Step 2 — Create the e2-micro VM (5 min)

⚠️ The "Always Free" tier has **strict requirements**. Stick to them:
- **Machine type must be `e2-micro`** (NOT `e2-small` — that's paid).
- **Region must be** `us-west1` (Oregon), `us-central1` (Iowa), or
  `us-east1` (South Carolina). Other regions are paid.
- **Disk type must be Standard persistent disk** (HDD). SSD costs extra.
- **Disk size max 30 GB** to stay free.

In the console:

1. **Compute Engine → VM instances → Create instance**.
2. Fill in:
   - **Name**: `signal-tracker`
   - **Region**: `us-central1` (Iowa) — cheapest egress, included in free
   - **Zone**: any (e.g. `us-central1-a`)
   - **Machine configuration**:
     - **Series**: `E2`
     - **Machine type**: `e2-micro` (the bottom of the dropdown — 0.25 vCPU,
       1 GB memory). Note that the displayed price says "$0/month with
       Free Tier" if everything is correct.
   - **OS and storage** → **Change**:
     - **Operating system**: Ubuntu
     - **Version**: Ubuntu 22.04 LTS (Minimal)
     - **Boot disk type**: **Standard persistent disk** (HDD)
     - **Size**: 30 GB
   - **Networking** → **Firewall**:
     - ☑ **Allow HTTP traffic**
     - ☑ **Allow HTTPS traffic**
   - **Advanced → SSH Keys**: paste your `~/.ssh/id_ed25519.pub`. The username
     before the `@` in the key becomes the SSH login (e.g. `ubuntu`,
     `malek`...).
3. **Create**. The VM is ready in ~30 seconds. Note the **External IP**
   from the instance list.

Quick sanity check (does the cost preview show $0?):

> If the right-hand panel says "Monthly cost estimate: $0.00" you're on
> Always Free. If it shows anything else (e.g. $6/month), one of the four
> bullets above isn't set right. Most common: you picked `e2-small` instead
> of `e2-micro`, or a non-free region.

## Step 3 — Add swap (mandatory on e2-micro)

Building the Docker image with `uv sync` can OOM on 1 GB of RAM. Add 2 GB
of swap before the first build:

```bash
ssh -i ~/.ssh/id_ed25519 <your-username>@<EXTERNAL_IP>

sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h    # confirms "Swap: 2.0Gi"
```

## Step 4 — Run the bootstrap (10 min)

```bash
curl -fsSL https://raw.githubusercontent.com/M13-Prime/Score_Simulator/main/scripts/deploy.sh | bash
```

This installs Docker, clones the repo to `/opt/signal-tracker`, and
creates `.env` from the template — then exits and asks you to fill it in.

```bash
cd /opt/signal-tracker
nano .env
```

For GCP / SQLite mode, set these only:

```bash
LLM_MODEL=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...

DASHBOARD_AUTH_USER=admin
DASHBOARD_AUTH_PASSWORD=<strong password>

# Stay on SQLite — leave DATABASE_URL empty and use DB_PATH.
DATABASE_URL=
DB_PATH=/data/signals.db
```

Save, log out / SSH back in (for `docker` group), then start in **lite mode**:

```bash
exit
ssh -i ~/.ssh/id_ed25519 <your-username>@<EXTERNAL_IP>
cd /opt/signal-tracker
docker compose -f docker-compose.lite.yml up -d --build
```

The `-f docker-compose.lite.yml` flag tells Compose to use the GCP-friendly
file: **no Postgres service**, dashboard + scheduler share a `/data` named
volume that holds the SQLite file.

Verify:

```bash
docker compose -f docker-compose.lite.yml ps             # 2 services Up
docker compose -f docker-compose.lite.yml logs -f dashboard
curl -i -u admin:<password> http://127.0.0.1:8000/healthz   # -> 200
```

Run the doctor inside the container:

```bash
docker compose -f docker-compose.lite.yml exec dashboard python scripts/doctor.py
```

## Step 5 — Cloudflare Tunnel (15 min)

Identical to the Oracle guide — see
[`oracle.md` § Step 4](oracle.md#step-4--cloudflare-tunnel-https-without-certificates-15-min).

GCP e2-micro is x86_64, so download the AMD64 build of `cloudflared`:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
    -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
```

The rest (`cloudflared tunnel login` / `create` / `route dns` / service
install) is the same.

## Step 6 — Daily backups to Cloud Storage (5 min)

You get 5 GB of Cloud Storage free in the same region. Snapshot the SQLite
file daily as insurance:

```bash
# Once: create a bucket in the project
gcloud storage buckets create gs://signal-tracker-backups \
    --location=us-central1 --uniform-bucket-level-access

# Recurring: cron entry on the VM
crontab -e
```

Add at the bottom:

```
# Daily SQLite backup to Cloud Storage at 03:00 UTC
0 3 * * * docker compose -f /opt/signal-tracker/docker-compose.lite.yml exec -T dashboard sqlite3 /data/signals.db ".backup '/tmp/db-$(date +\%F).sqlite'" \
  && docker cp signal-tracker-dashboard-1:/tmp/db-$(date +\%F).sqlite /tmp/ \
  && gcloud storage cp /tmp/db-$(date +\%F).sqlite gs://signal-tracker-backups/
```

Or the lazier version — just copy the file directly from the host's bind-mount:

```
0 3 * * * gcloud storage cp /var/lib/docker/volumes/signal-tracker_signal-tracker-data/_data/signals.db gs://signal-tracker-backups/db-$(date +\%F).sqlite
```

## Costs

| Line | Monthly |
|------|---------|
| GCP e2-micro (us-central1) | **€0** (Always Free) |
| 30 GB standard PD | **€0** (Always Free) |
| 5 GB Cloud Storage backups | **€0** (Always Free) |
| Cloudflare DNS + Tunnel | €0 |
| Domain | €0.85 (~€10/year) |
| Anthropic API (~500 articles/day) | ~€30–60 |
| **Fixed infra** | **€0.85** |
| **+ LLM at usage** | variable |

## Operating commands

Same as the Oracle guide but prefixed with `-f docker-compose.lite.yml`:

```bash
# Status / logs
docker compose -f docker-compose.lite.yml ps
docker compose -f docker-compose.lite.yml logs -f

# Update the code
cd /opt/signal-tracker && git pull && docker compose -f docker-compose.lite.yml build && docker compose -f docker-compose.lite.yml up -d

# Manual run
docker compose -f docker-compose.lite.yml exec dashboard python scripts/daily.py --dry-run
```

You can also create an alias to avoid typing the `-f` flag each time:

```bash
echo 'alias dc="docker compose -f /opt/signal-tracker/docker-compose.lite.yml"' >> ~/.bashrc
source ~/.bashrc
# Now: dc ps, dc logs -f, etc.
```

## Variant B — Keep Postgres via Neon (still free)

If you want managed Postgres without spending the e2-micro RAM on it:

1. Sign up at <https://neon.tech> (free tier: 0.5 GB storage, no cold start).
2. Create a project, copy the connection string from the dashboard.
3. In your `.env` on the VM:
   ```bash
   DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>.neon.tech/<db>?sslmode=require
   # DB_PATH not used when DATABASE_URL is set
   ```
4. Use the default `docker-compose.yml` (with the local Postgres service)
   OR use `docker-compose.lite.yml` — either works because the dashboard
   honours `DATABASE_URL` over the local `db` service.

Trade-off: one less dependency to back up on your end (Neon takes care of
it), but you depend on a 3rd party for uptime. For a personal tool, SQLite
on the GCP disk is the simpler path.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| `docker compose build` killed with "out of memory" | Swap not enabled. Re-run Step 3. |
| `e2-micro` not visible in the picker | The selected region isn't `us-west1` / `us-central1` / `us-east1`. Change region. |
| Monthly estimate shows `> $0` | You picked `e2-small`, an SSD disk, or a paid region. Fix one of the four constraints in Step 2. |
| Dashboard says `database is locked` (SQLite) | Scheduler and dashboard wrote concurrently. Rare and self-recovering. If frequent, switch to Variant B (Neon Postgres). |
| `gcloud` command not found in cron | Cron uses minimal PATH. Use the full path: `/usr/bin/gcloud` or `/snap/bin/gcloud`. |

## Why not Cloud Run + Cloud SQL?

You could deploy the dashboard as a Cloud Run service (free up to 2M
requests / month) and use Cloud Scheduler to ping a `/cron/daily`
endpoint. It works but it's more setup, the scheduler-as-Cloud-Run-Job
loses some flexibility, and Cloud SQL has no Always Free tier — you'd
still need Neon. For a personal tool, the e2-micro path is simpler.
