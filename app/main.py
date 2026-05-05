#!/usr/bin/env python3
"""
immich-share — FastAPI backend
Serves the UI and wraps the album-to-B2 share logic.
"""

import asyncio
import io
import os
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── CONFIG (from env) ─────────────────────────────────────────────────────────

IMMICH_URL     = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
B2_ENDPOINT    = os.environ["B2_ENDPOINT"]
B2_KEY_ID      = os.environ["B2_KEY_ID"]
B2_APP_KEY     = os.environ["B2_APP_KEY"]
B2_BUCKET      = os.environ["B2_BUCKET"]

DB_PATH = os.environ.get("DB_PATH", "/data/shares.db")

# ── DATABASE ──────────────────────────────────────────────────────────────────

def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                id          TEXT PRIMARY KEY,
                album_name  TEXT NOT NULL,
                photo_count INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                url         TEXT NOT NULL,
                revoked_at  TEXT
            )
        """)
        # migrate existing installs that lack the column
        try:
            conn.execute("ALTER TABLE shares ADD COLUMN revoked_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

def record_share(share_id: str, album_name: str, photo_count: int, expires_at: datetime, url: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO shares (id, album_name, photo_count, created_at, expires_at, url) VALUES (?,?,?,?,?,?)",
            (
                share_id,
                album_name,
                photo_count,
                datetime.now(timezone.utc).isoformat(),
                expires_at.isoformat(),
                url,
            ),
        )

# ── HELPERS ───────────────────────────────────────────────────────────────────

def immich_headers():
    return {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}

def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APP_KEY,
        config=boto3.session.Config(signature_version="s3v4"),
    )

def upload_bytes(s3, key: str, data: bytes, content_type: str):
    s3.put_object(Bucket=B2_BUCKET, Key=key, Body=data, ContentType=content_type)

def presign(s3, key: str, expires_in: int) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": B2_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )

# ── B2 NATIVE API (permanent deletion) ───────────────────────────────────────

def b2_delete_prefix(prefixes: list[str]):
    """Permanently delete all file versions under each prefix using the B2 Native API."""
    # Authorize
    with httpx.Client(timeout=30) as client:
        r = client.get(
            "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
            auth=(B2_KEY_ID, B2_APP_KEY),
        )
        r.raise_for_status()
        auth = r.json()

    api_url   = auth["apiInfo"]["storageApi"]["apiUrl"]
    auth_token = auth["authorizationToken"]
    bucket_id  = auth["apiInfo"]["storageApi"]["bucketId"] if "bucketId" in auth.get("apiInfo", {}).get("storageApi", {}) else None

    # Resolve bucket ID if not in auth response
    if not bucket_id:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{api_url}/b2api/v3/b2_list_buckets",
                headers={"Authorization": auth_token},
                json={"accountId": auth["accountId"], "bucketName": B2_BUCKET},
            )
            r.raise_for_status()
            buckets = r.json().get("buckets", [])
        if not buckets:
            raise ValueError(f"Bucket {B2_BUCKET!r} not found")
        bucket_id = buckets[0]["bucketId"]

    headers = {"Authorization": auth_token}

    for prefix in prefixes:
        start_name, start_id = prefix, None
        while True:
            params = {"bucketId": bucket_id, "prefix": prefix, "maxFileCount": 1000}
            if start_name:
                params["startFileName"] = start_name
            if start_id:
                params["startFileId"] = start_id

            with httpx.Client(timeout=30) as client:
                r = client.post(
                    f"{api_url}/b2api/v3/b2_list_file_versions",
                    headers=headers,
                    json=params,
                )
                r.raise_for_status()
                data = r.json()

            files = data.get("files", [])
            for f in files:
                with httpx.Client(timeout=30) as client:
                    client.post(
                        f"{api_url}/b2api/v3/b2_delete_file_version",
                        headers=headers,
                        json={"fileName": f["fileName"], "fileId": f["fileId"]},
                    )

            next_name = data.get("nextFileName")
            next_id   = data.get("nextFileId")
            if not next_name:
                break
            start_name, start_id = next_name, next_id

# ── GALLERY HTML BUILDER ──────────────────────────────────────────────────────

LIGHTBOX_JS = """
const imgs=[];
let cur=0;
document.querySelectorAll('.thumb').forEach((el,i)=>{
  imgs.push(el.src);
  el.addEventListener('click',()=>{cur=i;openLb();});
});
function openLb(){
  const img=document.getElementById('lb-img');
  img.src='';
  img.src=imgs[cur];
  document.getElementById('lb').classList.remove('hidden');
  document.body.style.overflow='hidden';
  document.getElementById('lb-counter').textContent=(cur+1)+' / '+imgs.length;
}
function closeLb(){
  document.getElementById('lb').classList.add('hidden');
  document.body.style.overflow='';
}
function prevImg(){cur=(cur-1+imgs.length)%imgs.length;openLb();}
function nextImg(){cur=(cur+1)%imgs.length;openLb();}
document.addEventListener('keydown',e=>{
  if(document.getElementById('lb').classList.contains('hidden'))return;
  if(e.key==='Escape')closeLb();
  if(e.key==='ArrowLeft')prevImg();
  if(e.key==='ArrowRight')nextImg();
});
document.getElementById('lb').addEventListener('click',e=>{if(e.target===document.getElementById('lb'))closeLb();});
"""

def build_gallery_html(album_name: str, photo_urls: list[str], expires_iso: str, zip_url: str) -> str:
    thumbs = "\n".join(
        f'    <img class="thumb" src="{u}" loading="lazy" alt="photo {i+1}">'
        for i, u in enumerate(photo_urls)
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{album_name}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111;color:#eee;font-family:system-ui,sans-serif;min-height:100vh}}
header{{padding:1.5rem 1rem 1rem;text-align:center}}
header h1{{font-size:1.4rem;font-weight:600}}
header p{{font-size:.8rem;color:#888;margin-top:.3rem}}
.dl-btn{{display:inline-flex;align-items:center;gap:.45rem;margin-top:.9rem;padding:.55rem 1.1rem;background:#e8ff6e;color:#111;border:none;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;text-decoration:none;transition:background .15s}}
.dl-btn:hover{{background:#d4ea55}}
.dl-btn svg{{width:16px;height:16px;flex-shrink:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;padding:1rem}}
.thumb{{width:100%;aspect-ratio:1;object-fit:cover;cursor:zoom-in;border-radius:4px;transition:opacity .15s}}
.thumb:hover{{opacity:.8}}
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.94);display:flex;align-items:center;justify-content:center;z-index:999}}
#lb.hidden{{display:none}}
#lb-img{{max-width:92vw;max-height:88vh;border-radius:4px;object-fit:contain}}
.lb-btn{{position:fixed;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.1);border:none;color:#fff;font-size:2rem;padding:.4rem .9rem;cursor:pointer;border-radius:4px;user-select:none;transition:background .15s}}
.lb-btn:hover{{background:rgba(255,255,255,.25)}}
#lb-prev{{left:.75rem}}
#lb-next{{right:.75rem}}
#lb-close{{position:fixed;top:.75rem;right:.75rem;background:rgba(255,255,255,.1);border:none;color:#fff;font-size:1.4rem;padding:.2rem .6rem;cursor:pointer;border-radius:4px}}
#lb-close:hover{{background:rgba(255,255,255,.25)}}
#lb-counter{{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);font-size:.8rem;color:rgba(255,255,255,.5);pointer-events:none}}
</style>
</head>
<body>
<header>
  <h1>{album_name}</h1>
  <p>Expires {expires_iso} &nbsp;·&nbsp; {len(photo_urls)} photos</p>
  <a class="dl-btn" href="{zip_url}" download>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3v13M7 11l5 5 5-5"/><path d="M5 21h14"/>
    </svg>
    Download all photos
  </a>
</header>
<div class="grid">{thumbs}</div>
<div id="lb" class="hidden">
  <img id="lb-img" src="" alt="">
  <button class="lb-btn" id="lb-prev" onclick="prevImg()">&#8249;</button>
  <button class="lb-btn" id="lb-next" onclick="nextImg()">&#8250;</button>
  <button id="lb-close" onclick="closeLb()">&#x2715;</button>
  <div id="lb-counter"></div>
</div>
<script>
{LIGHTBOX_JS}
</script>
</body>
</html>"""

# ── SHARE LOGIC (sync, run in thread) ────────────────────────────────────────

def do_share(album_id: str, days: int) -> str:
    """Downloads previews, uploads to B2, returns presigned index URL."""
    expires_in = days * 24 * 3600
    share_uuid = str(uuid.uuid4())
    prefix = f"albums/{share_uuid}"

    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(
                f"{IMMICH_URL}/api/albums/{album_id}",
                headers=immich_headers(),
            )
            if r.status_code == 404:
                raise ValueError("Album not found")
            r.raise_for_status()
            album = r.json()
    except httpx.ConnectError:
        raise ValueError(f"Cannot reach Immich at {IMMICH_URL}")

    album_name = album.get("albumName", "Album")
    assets = [a for a in album.get("assets", []) if a.get("type", "").upper() == "IMAGE"]
    if not assets:
        raise ValueError("Album has no images")

    s3 = make_s3()
    photo_items = []  # list of (presigned_url, filename, raw_bytes)
    errors = []

    def process(asset):
        aid = asset["id"]
        stem = os.path.splitext(asset.get("originalFileName", aid))[0]
        filename = f"{stem}.jpg"
        key = f"{prefix}/{filename}"
        try:
            with httpx.Client(timeout=60) as c:
                resp = c.get(
                    f"{IMMICH_URL}/api/assets/{aid}/thumbnail",
                    headers={**immich_headers(), "Accept": "image/jpeg"},
                    params={"size": "preview"},
                )
                resp.raise_for_status()
                data = resp.content
            upload_bytes(s3, key, data, "image/jpeg")
            return presign(s3, key, expires_in), filename, data
        except Exception as e:
            return None, None, str(e)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(process, a): a for a in assets}
        for future in as_completed(futures):
            url, filename, data_or_err = future.result()
            if url is None:
                errors.append(data_or_err)
            else:
                photo_items.append((url, filename, data_or_err))

    if not photo_items:
        raise ValueError(f"All {len(errors)} downloads failed: {errors[0]}")

    # Build and upload zip archive
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, filename, data in photo_items:
            zf.writestr(filename, data)
    zip_key = f"{prefix}/photos.zip"
    upload_bytes(s3, zip_key, zip_buf.getvalue(), "application/zip")
    zip_url = presign(s3, zip_key, expires_in)

    photo_urls = [url for url, _, _ in photo_items]

    expire_dt = datetime.now(timezone.utc) + timedelta(days=days)
    expires_iso = expire_dt.strftime("%Y-%m-%d %H:%M UTC")

    html = build_gallery_html(album_name, photo_urls, expires_iso, zip_url)
    html_key = f"{prefix}/index.html"
    upload_bytes(s3, html_key, html.encode(), "text/html; charset=utf-8")
    index_url = presign(s3, html_key, expires_in)
    record_share(share_uuid, album_name, len(photo_urls), expire_dt, index_url)
    return index_url

# ── CLEANUP LOOP ─────────────────────────────────────────────────────────────

def cleanup_loop():
    """Runs every hour, permanently deletes B2 files for expired/revoked shares."""
    while True:
        time.sleep(3600)
        try:
            now = datetime.now(timezone.utc)
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM shares WHERE revoked_at IS NOT NULL OR expires_at < ?",
                    (now.isoformat(),),
                ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                print("[cleanup] nothing to delete")
                continue
            b2_delete_prefix([f"albums/{id_}/" for id_ in ids])
            with db_connect() as conn:
                conn.executemany("DELETE FROM shares WHERE id = ?", [(id_,) for id_ in ids])
            print(f"[cleanup] deleted {len(ids)} expired/revoked shares")
        except Exception as e:
            print(f"[cleanup] error: {e}")

@asynccontextmanager
async def lifespan(app):
    db_init()
    t = threading.Thread(target=cleanup_loop, daemon=True)
    t.start()
    yield

# ── API ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="immich-share", lifespan=lifespan)

@app.get("/api/albums")
async def api_list_albums():
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{IMMICH_URL}/api/albums", headers=immich_headers())
            r.raise_for_status()
    except httpx.ConnectError:
        raise HTTPException(503, f"Cannot reach Immich at {IMMICH_URL}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Immich returned {e.response.status_code}")
    albums = r.json()
    return [
        {
            "id": a["id"],
            "name": a.get("albumName", ""),
            "assetCount": a.get("assetCount", 0),
            "thumbUrl": (
                f"{IMMICH_URL}/api/assets/{a['albumThumbnailAssetId']}/thumbnail"
                f"?size=thumbnail"
                if a.get("albumThumbnailAssetId") else None
            ),
        }
        for a in sorted(albums, key=lambda x: x.get("albumName", ""))
    ]

class ShareRequest(BaseModel):
    albumId: str
    days: int = 7

@app.post("/api/share")
async def api_share(req: ShareRequest):
    if not 1 <= req.days <= 30:
        raise HTTPException(400, "days must be between 1 and 90")
    loop = asyncio.get_event_loop()
    try:
        url = await loop.run_in_executor(None, do_share, req.albumId, req.days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Share failed: {e}")
    return {"url": url}

@app.get("/api/proxy-thumb")
async def proxy_thumb(url: str):
    """Proxy Immich thumbnail so the API key never hits the browser."""
    from fastapi.responses import Response
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={**immich_headers(), "Accept": "image/jpeg"})
            r.raise_for_status()
    except httpx.ConnectError:
        raise HTTPException(503, f"Cannot reach Immich at {IMMICH_URL}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Immich returned {e.response.status_code}")
    return Response(content=r.content, media_type="image/jpeg")

@app.get("/api/health")
async def health():
    return {"ok": True}

@app.get("/api/shares")
async def api_shares():
    now = datetime.now(timezone.utc)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, album_name, photo_count, created_at, expires_at, url, revoked_at FROM shares ORDER BY created_at DESC"
        ).fetchall()
    return [
        {
            "id":          r["id"],
            "album_name":  r["album_name"],
            "photo_count": r["photo_count"],
            "created_at":  r["created_at"],
            "expires_at":  r["expires_at"],
            "expired":     r["revoked_at"] is None and datetime.fromisoformat(r["expires_at"]) < now,
            "revoked":     r["revoked_at"] is not None,
            "revoked_at":  r["revoked_at"],
            "url":         r["url"],
        }
        for r in rows
    ]

@app.delete("/api/shares/{share_id}", status_code=204)
async def api_delete_share(share_id: str):
    with db_connect() as conn:
        row = conn.execute("SELECT id, revoked_at FROM shares WHERE id = ?", (share_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Share not found")
        if row["revoked_at"] is not None:
            raise HTTPException(409, "Share already revoked")

    def delete_from_b2():
        b2_delete_prefix([f"albums/{share_id}/"])

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_from_b2)
    except Exception as e:
        raise HTTPException(500, f"B2 deletion failed: {e}")

    with db_connect() as conn:
        conn.execute(
            "UPDATE shares SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), share_id),
        )

class RenewRequest(BaseModel):
    days: int = 7

@app.post("/api/shares/{share_id}/renew")
async def api_renew_share(share_id: str, req: RenewRequest):
    if not 1 <= req.days <= 30:
        raise HTTPException(400, "days must be between 1 and 30")

    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, album_name, photo_count, revoked_at FROM shares WHERE id = ?", (share_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Share not found")
        if row["revoked_at"] is not None:
            raise HTTPException(400, "Cannot renew a revoked share — create a new share instead")

    expires_in = req.days * 24 * 3600
    old_prefix = f"albums/{share_id}"
    new_uuid = str(uuid.uuid4())
    new_prefix = f"albums/{new_uuid}"

    def renew_in_b2():
        s3 = make_s3()
        paginator = s3.get_paginator("list_objects_v2")
        old_keys = []
        for page in paginator.paginate(Bucket=B2_BUCKET, Prefix=old_prefix + "/"):
            old_keys.extend(obj["Key"] for obj in page.get("Contents", []))
        if not old_keys:
            raise ValueError("No files found in B2 for this share — it may have been cleaned up")

        photo_urls = []
        zip_url = None
        for old_key in old_keys:
            if old_key.endswith("/index.html"):
                continue
            filename = old_key.split("/")[-1]
            new_key = f"{new_prefix}/{filename}"
            # copy by downloading and re-uploading
            obj = s3.get_object(Bucket=B2_BUCKET, Key=old_key)
            data = obj["Body"].read()
            content_type = obj["ContentType"]
            upload_bytes(s3, new_key, data, content_type)
            url = presign(s3, new_key, expires_in)
            if filename == "photos.zip":
                zip_url = url
            else:
                photo_urls.append(url)

        expire_dt = datetime.now(timezone.utc) + timedelta(days=req.days)
        expires_iso = expire_dt.strftime("%Y-%m-%d %H:%M UTC")
        html = build_gallery_html(row["album_name"], photo_urls, expires_iso, zip_url or "")
        new_html_key = f"{new_prefix}/index.html"
        upload_bytes(s3, new_html_key, html.encode(), "text/html; charset=utf-8")
        index_url = presign(s3, new_html_key, expires_in)

        # delete old prefix
        to_delete = [{"Key": k} for k in old_keys]
        for i in range(0, len(to_delete), 1000):
            s3.delete_objects(
                Bucket=B2_BUCKET,
                Delete={"Objects": to_delete[i:i+1000], "Quiet": True},
            )

        return index_url, expire_dt

    loop = asyncio.get_event_loop()
    try:
        index_url, expire_dt = await loop.run_in_executor(None, renew_in_b2)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Renew failed: {e}")

    with db_connect() as conn:
        conn.execute(
            "UPDATE shares SET id = ?, expires_at = ?, url = ?, revoked_at = NULL WHERE id = ?",
            (new_uuid, expire_dt.isoformat(), index_url, share_id),
        )
    return {"id": new_uuid, "url": index_url}

@app.delete("/api/shares", status_code=204)
async def api_cleanup_shares():
    now = datetime.now(timezone.utc)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id FROM shares WHERE revoked_at IS NOT NULL OR expires_at < ?",
            (now.isoformat(),),
        ).fetchall()

    ids = [r["id"] for r in rows]
    if not ids:
        return

    def delete_all_from_b2():
        b2_delete_prefix([f"albums/{id_}/" for id_ in ids])

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, delete_all_from_b2)
    except Exception as e:
        raise HTTPException(500, f"B2 cleanup failed: {e}")

    with db_connect() as conn:
        conn.executemany("DELETE FROM shares WHERE id = ?", [(id_,) for id_ in ids])

# ── SERVE FRONTEND ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = os.path.join(os.path.dirname(__file__), "ui.html")
    with open(html_path) as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    html_path = os.path.join(os.path.dirname(__file__), "admin.html")
    with open(html_path) as f:
        return f.read()
