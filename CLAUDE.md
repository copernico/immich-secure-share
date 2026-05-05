# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**immich-share** is a FastAPI application that exports Immich albums to Backblaze B2 as temporary, expiring share links. Immich stays private (no internet exposure), while recipients get a presigned URL that expires after 1-30 days.

**Key Flow:**
1. User selects an Immich album via web UI
2. App downloads preview images from Immich API
3. Uploads to private B2 bucket with unique UUID prefix (`albums/{uuid}/`)
4. Generates presigned S3 URLs for gallery and all images
5. Returns a single HTML gallery URL valid for chosen duration
6. Background cleanup job deletes objects older than 30 days

## Architecture

**Single-file backend (`app/main.py`):**
- FastAPI app with 4 endpoints: `/api/albums`, `/api/share`, `/api/proxy-thumb`, `/api/health`
- Background cleanup thread runs hourly via `lifespan` context manager
- Synchronous share logic (`do_share()`) runs in thread executor to avoid blocking async event loop
- Uses `httpx` for Immich API calls, `boto3` for B2/S3 operations

**Frontend (`app/ui.html`):**
- Vanilla HTML/CSS/JS single page
- Album grid with thumbnail proxy to hide Immich API key from browser
- Publish flow uploads assets in parallel (4 workers) and generates gallery

**Gallery output:**
- Self-contained HTML page with inline CSS/JS
- Lightbox with keyboard navigation (←/→/Esc)
- All asset URLs are presigned and expire with the share

**Storage structure in B2:**
```
albums/
  {uuid}/
    index.html          # gallery page
    {filename}.jpg      # preview images
```

## Development Commands

### Build and run

```bash
# Build and start container
docker compose up -d --build

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose down && docker compose up -d --build

# Stop
docker compose down
```

### Environment setup

Copy `.env.example` to `.env` and configure:
- `IMMICH_URL`: Immich instance endpoint (LAN IP or Tailscale)
- `IMMICH_API_KEY`: Generate in Immich → Account Settings → API Keys
- `B2_ENDPOINT`: Backblaze B2 S3-compatible endpoint (e.g., `https://s3.us-west-004.backblazeb2.com`)
- `B2_KEY_ID`, `B2_APP_KEY`: B2 application key with read/write access
- `B2_BUCKET`: Private B2 bucket name

### Testing the app

UI available at:
- Localhost: `http://localhost:8099`
- Tailscale network: `http://<host-tailscale-ip>:8099`

Health check: `curl http://localhost:8099/api/health`

### Running with Immich on same Docker host

If Immich runs in Docker on the same machine, uncomment the `networks` section in `docker-compose.yml` and set `IMMICH_URL=http://immich-server:3001` in `.env`.

## Key Technical Decisions

**Why preview images instead of originals?**  
Keeps B2 storage costs low and download times fast. Gallery shows compressed previews (~200-400KB each), not full-resolution originals.

**Why 30-day hard cleanup?**  
Presigned URLs expire client-side (1-30 days), but files remain in B2. The cleanup job ensures worst-case 30-day retention for cost control. A 1-day share may linger up to 30 days in storage but the link is already dead.

**Why synchronous `do_share()` in thread executor?**  
Mixing `httpx.Client` (sync) and `boto3` (sync) with `fastapi` (async). Running in `loop.run_in_executor()` keeps the async event loop responsive during multi-asset downloads.

**Why proxy `/api/proxy-thumb`?**  
Immich API key must never reach the browser. Backend proxies thumbnails with key in header, frontend gets plain image bytes.

## Dependencies

- `fastapi` 0.115.0 – web framework
- `uvicorn` 0.30.6 – ASGI server
- `httpx` 0.27.0 – async/sync HTTP client for Immich API
- `boto3` 1.35.0 – AWS SDK for B2 S3-compatible storage

## Debugging

**Album not loading:**  
Check `IMMICH_URL` and `IMMICH_API_KEY` in `.env`. Test with:
```bash
curl -H "x-api-key: YOUR_KEY" http://YOUR_IMMICH_URL/api/albums
```

**Share fails with 403 from B2:**  
Verify B2 bucket is **private** (not public) and application key has read/write permissions. Presigned URLs only work with private buckets.

**Cleanup not running:**  
Check container logs for `[cleanup]` entries. Cleanup runs hourly. Manually trigger by restarting: `docker compose restart`.

**Immich on Docker network:**  
If using container name (e.g., `immich-server`), ensure `immich-share` is attached to Immich's Docker network. See `docker-compose.yml` commented section.
