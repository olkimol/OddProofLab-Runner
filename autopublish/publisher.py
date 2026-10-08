#!/usr/bin/env python3
import mimetypes
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

TOKEN_URI = "https://oauth2.googleapis.com/token"
QUEUE_SHEET = os.getenv("QUEUE_SHEET", "Queue")
QUEUE_RANGE = f"{QUEUE_SHEET}!A1:Q1000"
PUBLISHABLE = {"READY", "RETRY"}
URL_COLUMNS = {
    "telegram": "telegram_url",
    "instagram": "instagram_url",
    "threads": "threads_url",
    "tiktok": "tiktok_url",
    "youtube": "youtube_url",
}
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents.readonly",
]
YOUTUBE_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


class PublishError(RuntimeError):
    pass


def env(name: str, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if required and not value:
        raise PublishError(f"Missing environment variable: {name}")
    return value


def request_json(method, url, *, timeout=60, **kwargs):
    response = requests.request(method, url, timeout=timeout, **kwargs)
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text[:1000]}
    if not response.ok:
        raise PublishError(f"{method} {url} -> {response.status_code}: {payload}")
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code") not in (None, "", "ok"):
            raise PublishError(f"{method} {url} -> API error: {error}")
    return payload


def google_credentials(scopes, refresh_env="GOOGLE_REFRESH_TOKEN"):
    return Credentials(
        token=None,
        refresh_token=env(refresh_env),
        token_uri=TOKEN_URI,
        client_id=env("GOOGLE_CLIENT_ID"),
        client_secret=env("GOOGLE_CLIENT_SECRET"),
        scopes=scopes,
    )


def google_services():
    credentials = google_credentials(GOOGLE_SCOPES)
    return {
        "drive": build("drive", "v3", credentials=credentials, cache_discovery=False),
        "sheets": build("sheets", "v4", credentials=credentials, cache_discovery=False),
        "docs": build("docs", "v1", credentials=credentials, cache_discovery=False),
    }


def youtube_service():
    refresh_name = "YOUTUBE_REFRESH_TOKEN" if os.getenv("YOUTUBE_REFRESH_TOKEN") else "GOOGLE_REFRESH_TOKEN"
    credentials = google_credentials(YOUTUBE_SCOPE, refresh_env=refresh_name)
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def read_queue(sheets):
    spreadsheet_id = env("QUEUE_SPREADSHEET_ID")
    values = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=QUEUE_RANGE)
        .execute()
        .get("values", [])
    )
    if not values:
        return [], []
    headers = [str(x).strip() for x in values[0]]
    rows = []
    for row_number, raw in enumerate(values[1:], start=2):
        padded = list(raw) + [""] * max(0, len(headers) - len(raw))
        rows.append((row_number, dict(zip(headers, padded[: len(headers)]))))
    return headers, rows


def write_cells(sheets, row_number: int, headers: list[str], changes: dict):
    spreadsheet_id = env("QUEUE_SPREADSHEET_ID")
    updates = []
    for key, value in changes.items():
        if key not in headers:
            continue
        column_index = headers.index(key)
        column = ""
        n = column_index + 1
        while n:
            n, rem = divmod(n - 1, 26)
            column = chr(65 + rem) + column
        updates.append({"range": f"{QUEUE_SHEET}!{column}{row_number}", "values": [[value]]})
    if updates:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()


def due(row):
    scheduled = str(row.get("scheduled_at", "")).strip()
    if not scheduled:
        return True
    try:
        target = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target <= datetime.now(timezone.utc)
    except ValueError:
        raise PublishError(f"Invalid scheduled_at: {scheduled}")


def parse_platforms(row):
    raw = str(row.get("platforms", "")).strip()
    if not raw:
        return list(URL_COLUMNS)
    selected = []
    for item in raw.split(","):
        name = item.strip().lower()
        if name in URL_COLUMNS and name not in selected:
            selected.append(name)
    return selected


def drive_download(drive, file_id: str, destination: Path):
    meta = drive.files().get(fileId=file_id, fields="id,name,mimeType,size").execute()
    request = drive.files().get_media(fileId=file_id)
    with destination.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return meta


def document_text(docs, document_id: str) -> str:
    doc = docs.documents().get(documentId=document_id).execute()
    out = []
    for block in doc.get("body", {}).get("content", []):
        paragraph = block.get("paragraph")
        if not paragraph:
            continue
        for element in paragraph.get("elements", []):
            run = element.get("textRun")
            if run:
                out.append(run.get("content", ""))
    return "".join(out).strip()


def extract_text_defaults(text: str):
    lines = [line.strip() for line in text.splitlines()]
    title = next((line for line in lines if line), "")
    caption = ""
    markers = ("Подпись для Telegram:", "Подпись:", "Caption:")
    for i, line in enumerate(lines):
        if line in markers:
            collected = []
            for candidate in lines[i + 1 :]:
                if candidate.upper() == candidate and len(candidate) > 4 and any(ch.isalpha() for ch in candidate):
                    break
                if candidate:
                    collected.append(candidate)
                if len(collected) >= 3:
                    break
            caption = "\n".join(collected).strip()
            break
    return title[:100], caption


def stage_upload(path: Path, mime: str, content_id: str):
    supabase_url = env("SUPABASE_URL").rstrip("/")
    secret = env("SUPABASE_SECRET_KEY")
    bucket = env("STAGE_BUCKET", required=False, default="autopublish-stage")
    object_path = f"{content_id}/{int(time.time())}-{path.name}"
    encoded = quote(object_path, safe="/")
    url = f"{supabase_url}/storage/v1/object/{bucket}/{encoded}"
    headers = {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": mime,
        "x-upsert": "true",
    }
    with path.open("rb") as handle:
        response = requests.post(url, headers=headers, data=handle, timeout=180)
    if not response.ok:
        raise PublishError(f"Supabase upload failed: {response.status_code} {response.text[:1000]}")
    public_url = f"{supabase_url}/storage/v1/object/public/{bucket}/{encoded}"
    return object_path, public_url


def stage_delete(object_path: str):
    if not object_path:
        return
    supabase_url = env("SUPABASE_URL").rstrip("/")
    secret = env("SUPABASE_SECRET_KEY")
    bucket = env("STAGE_BUCKET", required=False, default="autopublish-stage")
    url = f"{supabase_url}/storage/v1/object/{bucket}"
    headers = {"apikey": secret, "Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    try:
        requests.delete(url, headers=headers, json={"prefixes": [object_path]}, timeout=60)
    except Exception:
        pass


def publish_telegram(path: Path, mime: str, caption: str):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    username = env("TELEGRAM_CHANNEL_USERNAME", required=False).lstrip("@")
    if mime.startswith("image/"):
        method, field = "sendPhoto", "photo"
    elif mime.startswith("video/"):
        method, field = "sendVideo", "video"
    else:
        method, field = "sendDocument", "document"
    with path.open("rb") as handle:
        result = request_json(
            "POST",
            f"https://api.telegram.org/bot{token}/{method}",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={field: (path.name, handle, mime)},
            timeout=180,
        )
    message_id = result["result"]["message_id"]
    return f"https://t.me/{username}/{message_id}" if username else f"telegram:message:{message_id}"


def wait_meta_container(base: str, container_id: str, token: str, *, threads=False):
    for _ in range(30):
        if threads:
            payload = request_json(
                "GET", f"{base}/{container_id}",
                params={"fields": "status,error_message", "access_token": token},
            )
            status = str(payload.get("status", "")).upper()
            if status in {"FINISHED", "PUBLISHED"}:
                return
            if status == "ERROR":
                raise PublishError(f"Threads media processing failed: {payload}")
        else:
            payload = request_json(
                "GET", f"{base}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
            status = str(payload.get("status_code", "")).upper()
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise PublishError(f"Instagram media processing failed: {payload}")
        time.sleep(5)
    raise PublishError("Media container processing timed out")


def publish_instagram(public_url: str, mime: str, caption: str):
    token = env("INSTAGRAM_ACCESS_TOKEN")
    user_id = env("INSTAGRAM_USER_ID")
    version = env("META_GRAPH_VERSION", required=False, default="v24.0")
    api_base = env("INSTAGRAM_API_BASE", required=False, default="https://graph.facebook.com").rstrip("/")
    base = f"{api_base}/{version}"
    params = {"caption": caption[:2200], "access_token": token}
    if mime.startswith("video/"):
        params.update({"media_type": "REELS", "video_url": public_url, "share_to_feed": "true"})
    elif mime.startswith("image/"):
        params.update({"image_url": public_url})
    else:
        raise PublishError(f"Instagram unsupported MIME type: {mime}")
    container = request_json("POST", f"{base}/{user_id}/media", params=params)["id"]
    wait_meta_container(base, container, token)
    media_id = request_json(
        "POST", f"{base}/{user_id}/media_publish",
        params={"creation_id": container, "access_token": token},
    )["id"]
    details = request_json(
        "GET", f"{base}/{media_id}",
        params={"fields": "permalink", "access_token": token},
    )
    return details.get("permalink") or f"instagram:media:{media_id}"


def publish_threads(public_url: str, mime: str, caption: str):
    token = env("THREADS_ACCESS_TOKEN")
    user_id = env("THREADS_USER_ID", required=False, default="me")
    base = env("THREADS_API_BASE", required=False, default="https://graph.threads.net/v1.0").rstrip("/")
    params = {"text": caption[:500], "access_token": token}
    if mime.startswith("video/"):
        params.update({"media_type": "VIDEO", "video_url": public_url})
    elif mime.startswith("image/"):
        params.update({"media_type": "IMAGE", "image_url": public_url})
    else:
        raise PublishError(f"Threads unsupported MIME type: {mime}")
    container = request_json("POST", f"{base}/{user_id}/threads", params=params)["id"]
    if mime.startswith("video/"):
        wait_meta_container(base, container, token, threads=True)
    post_id = request_json(
        "POST", f"{base}/{user_id}/threads_publish",
        params={"creation_id": container, "access_token": token},
    )["id"]
    details = request_json(
        "GET", f"{base}/{post_id}",
        params={"fields": "permalink", "access_token": token},
    )
    return details.get("permalink") or f"threads:post:{post_id}"


def tiktok_creator_info(token: str):
    return request_json(
        "POST",
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        json={},
    ).get("data", {})


def wait_tiktok(token: str, publish_id: str):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    for _ in range(36):
        payload = request_json(
            "POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers=headers, json={"publish_id": publish_id},
        )
        data = payload.get("data", {})
        status = str(data.get("status", "")).upper()
        if status == "PUBLISH_COMPLETE":
            ids = data.get("publicaly_available_post_id") or []
            return str(ids[0]) if ids else publish_id
        if status == "FAILED":
            raise PublishError(f"TikTok publish failed: {data}")
        time.sleep(5)
    return publish_id


def publish_tiktok(path: Path, public_url: str, mime: str, title: str, caption: str, ai_generated: bool):
    token = env("TIKTOK_ACCESS_TOKEN")
    privacy = env("TIKTOK_PRIVACY_LEVEL", required=False, default="SELF_ONLY")
    creator = tiktok_creator_info(token)
    options = creator.get("privacy_level_options") or []
    if privacy not in options:
        raise PublishError(f"TikTok privacy {privacy!r} not allowed; available: {options}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}

    if mime.startswith("image/"):
        if env("TIKTOK_PHOTO_URL_VERIFIED", required=False, default="0") != "1":
            raise PublishError("TikTok photo publishing requires a verified media URL domain/prefix")
        body = {
            "post_info": {
                "title": title[:90],
                "description": caption[:4000],
                "privacy_level": privacy,
                "disable_comment": bool(creator.get("comment_disabled", False)),
                "auto_add_music": True,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_cover_index": 0,
                "photo_images": [public_url],
            },
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
            "is_aigc": bool(ai_generated),
        }
        payload = request_json(
            "POST", "https://open.tiktokapis.com/v2/post/publish/content/init/",
            headers=headers, json=body,
        )
        publish_id = payload["data"]["publish_id"]
    elif mime.startswith("video/"):
        size = path.stat().st_size
        body = {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": privacy,
                "disable_duet": bool(creator.get("duet_disabled", False)),
                "disable_comment": bool(creator.get("comment_disabled", False)),
                "disable_stitch": bool(creator.get("stitch_disabled", False)),
                "is_aigc": bool(ai_generated),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        }
        payload = request_json(
            "POST", "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers=headers, json=body,
        )
        publish_id = payload["data"]["publish_id"]
        upload_url = payload["data"]["upload_url"]
        with path.open("rb") as handle:
            response = requests.put(
                upload_url,
                headers={
                    "Content-Type": mime,
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                data=handle,
                timeout=300,
            )
        if not response.ok:
            raise PublishError(f"TikTok video upload failed: {response.status_code} {response.text[:1000]}")
    else:
        raise PublishError(f"TikTok unsupported MIME type: {mime}")

    post_id = wait_tiktok(token, publish_id)
    username = str(creator.get("creator_username", "")).strip().lstrip("@")
    if post_id == publish_id or not username:
        return f"tiktok:publish:{post_id}"
    kind = "photo" if mime.startswith("image/") else "video"
    return f"https://www.tiktok.com/@{username}/{kind}/{post_id}"


def publish_youtube(path: Path, mime: str, title: str, caption: str, ai_generated: bool):
    if not mime.startswith("video/"):
        raise PublishError("YouTube publishing in this pipeline supports video only")
    youtube = youtube_service()
    expected = env("YOUTUBE_CHANNEL_ID", required=False)
    if expected:
        channels = youtube.channels().list(part="id", mine=True).execute().get("items", [])
        visible = {item.get("id") for item in channels}
        if expected not in visible:
            raise PublishError(f"Authorized YouTube channel does not match YOUTUBE_CHANNEL_ID {expected}")
    body = {
        "snippet": {
            "title": title[:100] or path.stem[:100],
            "description": caption[:5000],
            "categoryId": env("YOUTUBE_CATEGORY_ID", required=False, default="22"),
        },
        "status": {
            "privacyStatus": env("YOUTUBE_PRIVACY", required=False, default="public"),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(ai_generated),
        },
    }
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return f"https://www.youtube.com/watch?v={response['id']}"


def publish_one(services, headers, row_number, row):
    status = str(row.get("status", "")).strip().upper()
    if status not in PUBLISHABLE or not due(row):
        return False

    content_id = str(row.get("content_id", "")).strip()
    media_id = str(row.get("media_drive_id", "")).strip()
    if not content_id or not media_id:
        raise PublishError("content_id and media_drive_id are required")

    prompt_id = str(row.get("prompt_drive_id", "")).strip()
    text = document_text(services["docs"], prompt_id) if prompt_id else ""
    fallback_title, fallback_caption = extract_text_defaults(text)
    title = str(row.get("title", "")).strip() or fallback_title or content_id
    caption = str(row.get("caption", "")).strip() or fallback_caption or title
    ai_generated = str(row.get("ai_generated", "")).strip().lower() in {"true", "1", "yes", "да"}
    platforms = parse_platforms(row)

    write_cells(services["sheets"], row_number, headers, {"status": "PUBLISHING", "last_error": ""})
    staged_object = ""
    public_url = ""

    with tempfile.TemporaryDirectory(prefix="unitec-autopublish-") as tmp:
        filename = str(row.get("media_name", "")).strip() or f"{content_id}.bin"
        path = Path(tmp) / Path(filename).name
        meta = drive_download(services["drive"], media_id, path)
        mime = meta.get("mimeType") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        needs_url = any(name in platforms for name in ("instagram", "threads")) or (
            "tiktok" in platforms and mime.startswith("image/")
        )
        if needs_url:
            staged_object, public_url = stage_upload(path, mime, content_id)

        changes = {}
        failures = []
        try:
            for platform in platforms:
                column = URL_COLUMNS[platform]
                existing = str(row.get(column, "")).strip()
                if existing:
                    continue
                try:
                    if platform == "telegram":
                        url = publish_telegram(path, mime, caption)
                    elif platform == "instagram":
                        url = publish_instagram(public_url, mime, caption)
                    elif platform == "threads":
                        url = publish_threads(public_url, mime, caption)
                    elif platform == "tiktok":
                        url = publish_tiktok(path, public_url, mime, title, caption, ai_generated)
                    elif platform == "youtube":
                        if not mime.startswith("video/"):
                            continue
                        url = publish_youtube(path, mime, title, caption, ai_generated)
                    else:
                        continue
                    changes[column] = url
                    write_cells(services["sheets"], row_number, headers, {column: url})
                except Exception as exc:
                    failures.append(f"{platform}: {exc}")
        finally:
            stage_delete(staged_object)

    if failures:
        final_status = "RETRY"
        last_error = " | ".join(failures)[:5000]
    else:
        final_status = "PUBLISHED"
        last_error = ""
    changes.update({"status": final_status, "last_error": last_error})
    write_cells(services["sheets"], row_number, headers, changes)
    return True


def main():
    if env("AUTOPUBLISH_ENABLED", required=False, default="0") != "1":
        print("AUTOPUBLISH_DISABLED=1")
        return 0
    services = google_services()
    headers, rows = read_queue(services["sheets"])
    processed = 0
    for row_number, row in rows:
        try:
            if publish_one(services, headers, row_number, row):
                processed += 1
        except Exception as exc:
            print(f"ROW_{row_number}_ERROR={exc}", file=sys.stderr)
            try:
                write_cells(
                    services["sheets"], row_number, headers,
                    {"status": "RETRY", "last_error": str(exc)[:5000]},
                )
            except Exception:
                pass
    print(f"PROCESSED={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
