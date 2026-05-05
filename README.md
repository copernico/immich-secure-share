# immich-share

Export an Immich album to Backblaze B2 as a temporary, expiring share link.
Immich stays private (no internet exposure). Recipients get a single URL that
stops working after the chosen number of days. Files are cleaned up automatically.

---

## How it works

1. You open the web UI (via Tailscale) and pick an album
2. The app downloads preview images from Immich and uploads them to a private B2 bucket
3. A presigned share URL is generated — valid for 1–30 days
4. You send that URL to whoever you want. No account needed on their end
5. After 30 days, a background job cleans up orphaned files from B2

---

## Prerequisites

- Docker + Docker Compose on the host machine
- The host must be able to reach your Immich instance (same LAN, or both on Tailscale)
- A Backblaze B2 account with a **private** bucket and an application key

---

## B2 setup

1. Create a **private** bucket at backblaze.com (e.g. `my-immich-shares`)
2. Create an application key scoped to that bucket with read/write permissions
3. Note your bucket's endpoint — visible under **Buckets → Endpoint**, looks like
   `https://s3.us-west-004.backblazeb2.com`

---

## Installation

```bash
tar xzf immich-share.tar.gz
cd immich-share
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
IMMICH_URL=http://192.168.1.x:2283      # or Tailscale IP if running remotely
IMMICH_API_KEY=your_immich_api_key      # Immich → Account Settings → API Keys

B2_ENDPOINT=https://s3.us-west-004.backblazeb2.com
B2_KEY_ID=your_b2_key_id
B2_APP_KEY=your_b2_app_key
B2_BUCKET=your_bucket_name
```

Then start the container:

```bash
docker compose up -d --build
```

---

## Accessing the UI

Open `http://<host-tailscale-ip>:8099` from any device on your Tailscale network.

On the host itself: `http://localhost:8099`

---

## Usage

1. The album grid loads automatically with thumbnails
2. Tap an album to select it
3. Adjust the expiry (1 / 3 / 7 / 14 / 30 days)
4. Tap **Publish** and wait for the upload to complete
5. Copy the generated link and send it

The link works for anyone with the URL — no login required.
After the expiry date the link returns 403. Files are deleted from B2 within 30 days.

---

## Immich on the same Docker host

If Immich runs in Docker on the same machine, use the container name as the host
and attach immich-share to Immich's network. In `docker-compose.yml`, uncomment:

```yaml
networks:
  - immich_default
networks:
  immich_default:
    external: true
```

And set `IMMICH_URL=http://immich-server:3001` in `.env`.

---

## Updating

```bash
docker compose down
docker compose up -d --build
```

---

## Cleanup

The container runs a background job every hour that deletes B2 objects older than
30 days. This is the worst-case retention — a 1-day share may linger up to 30 days,
but the link is already dead after day 1. No manual cleanup needed.
