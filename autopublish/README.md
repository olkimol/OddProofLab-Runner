# UNITEC Auto Publish

Free direct publishing pipeline from one Google Drive / Google Sheets source. Upload-Post is not used.

## Source of truth

Google Sheet: `UNITEC Publish Queue` (`Queue` tab).

Columns used by the publisher:

- `content_id`
- `media_drive_id`
- `media_name`
- `prompt_drive_id`
- `status`
- `telegram_url`
- `instagram_url`
- `threads_url`
- `tiktok_url`
- `youtube_url`
- `created_at`
- `last_error`
- `caption`
- `title`
- `scheduled_at`
- `platforms`
- `ai_generated`

Only rows with `status=READY` or `status=RETRY` are published. `scheduled_at` may be blank for immediate processing or an ISO-8601 timestamp for scheduled publishing. Existing platform URLs are treated as already published, so retries are idempotent at queue level.

## Channels

- Telegram: Bot API, file upload from the runner.
- Instagram: Graph API. Images and Reels use a short-lived public media URL from Supabase staging.
- Threads: Threads API. Images and videos use the same staging URL.
- TikTok: Content Posting API. Videos use `FILE_UPLOAD`. Photos use `PULL_FROM_URL` and therefore require TikTok verification of the media URL domain/prefix. Public Direct Post also requires TikTok app audit; unaudited apps are restricted by TikTok.
- YouTube: YouTube Data API resumable upload. Video only.

## Safety gate

The workflow does nothing unless repository secret `AUTOPUBLISH_ENABLED` is exactly `1`.

Add `AUTOPUBLISH_ENABLED=1` only after every target channel is authorized and a test row has been reviewed. The current queue item is intentionally marked `READY_WAITING_AUTH`, so it cannot be published by the workflow yet.

## GitHub Actions secrets

Core:

- `AUTOPUBLISH_ENABLED` — add last; value `1` enables publication.
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN` — must authorize Google Drive, Google Sheets and Google Docs read access used by this queue.
- `SUPABASE_SECRET_KEY` — server-only Supabase project secret/service-role key. Never put this key in source code or frontend code.

Telegram:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_CHANNEL_USERNAME` — without `@`; used to write the public post URL back to the queue.

Instagram:

- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_USER_ID`

Threads:

- `THREADS_ACCESS_TOKEN`

TikTok:

- `TIKTOK_ACCESS_TOKEN` with Content Posting API authorization.
- `TIKTOK_PHOTO_URL_VERIFIED=1` only after TikTok has verified the media domain/URL prefix. Keep it absent or `0` before verification.

YouTube:

- `YOUTUBE_REFRESH_TOKEN` with `https://www.googleapis.com/auth/youtube.upload` scope. If omitted, the publisher falls back to `GOOGLE_REFRESH_TOKEN`, but that token must then also include the YouTube upload scope.
- `YOUTUBE_CHANNEL_ID` is recommended so the publisher verifies the authorized target channel before upload.

## Existing infrastructure

Queue spreadsheet ID and Supabase project URL are intentionally stored as non-secret workflow configuration. Temporary media is uploaded to bucket `autopublish-stage`, used only while remote platforms fetch it, and then deleted.

## Activation sequence

1. Authorize Google and add the three Google secrets.
2. Add `SUPABASE_SECRET_KEY`.
3. Authorize the required social channels and add their secrets.
4. Test each target channel with a non-public/private test where the platform supports it.
5. Change the intended queue row from `READY_WAITING_AUTH` to `READY`.
6. Add `AUTOPUBLISH_ENABLED=1`.
7. Run `UNITEC Auto Publish` manually once; after verification the schedule checks the queue every 15 minutes.

Do not paste access tokens or secret keys into issues, source files, logs, or chat messages.
