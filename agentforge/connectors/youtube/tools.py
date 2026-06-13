"""YouTube tool factory — read-only YouTube Data API v3 calls bound to a connection."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from agentforge.secret_redactor import get_redactor

logger = get_logger(__name__)

_YT_BASE = "https://www.googleapis.com/youtube/v3"
_REQUEST_TIMEOUT = 20


def create_youtube_tools(
    connection_id: str,
    token_accessor: Callable[[], str],
) -> list[Callable]:
    """Create YouTube tool callables bound to a specific connection."""

    def _err_body(exc: Exception) -> str:
        """Render an HTTP error for a tool result, redacting secrets from the body."""
        if isinstance(exc, HTTPError) and hasattr(exc, "read"):
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = str(exc)
            text = f"{exc.code}: {raw[:500]}" if raw else str(exc)
        else:
            text = str(exc)
        return get_redactor().redact(text).text

    def _yt_get(path: str, params: dict | None = None) -> dict:
        qs = f"?{urlencode(params)}" if params else ""
        url = f"{_YT_BASE}{path}{qs}"

        def _do(t: str) -> dict:
            req = Request(url, headers={"Authorization": f"Bearer {t}"})
            with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())

        try:
            return _do(token_accessor())
        except HTTPError as exc:
            if exc.code != 401:
                raise
            # Token expired mid-call. Accessor refreshes > retry once.
            return _do(token_accessor())

    from agentforge.tools.registry import tool

    @tool
    def youtube_search(query: str, kind: str = "video", limit: int = 10) -> str:
        """Search YouTube for videos, channels, or playlists."""
        if not query:
            return json.dumps({"status": "error", "error": "query is required"})
        kind = kind if kind in ("video", "channel", "playlist") else "video"
        try:
            data = _yt_get(
                "/search",
                {
                    "part": "snippet",
                    "q": query,
                    "type": kind,
                    "maxResults": max(1, min(int(limit), 50)),
                },
            )
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            logger.error("youtube_search error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        results = []
        for it in data.get("items") or []:
            sn = it.get("snippet", {})
            id_obj = it.get("id", {})
            results.append(
                {
                    "kind": id_obj.get("kind", "").replace("youtube#", ""),
                    "video_id": id_obj.get("videoId", ""),
                    "channel_id": id_obj.get("channelId", ""),
                    "playlist_id": id_obj.get("playlistId", ""),
                    "title": sn.get("title", ""),
                    "channel_title": sn.get("channelTitle", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "description": sn.get("description", ""),
                }
            )
        if not results:
            return json.dumps({"status": "no_results", "query": query})
        return json.dumps({"status": "ok", "query": query, "count": len(results), "results": results}, indent=2)

    @tool
    def youtube_video_details(video_id: str) -> str:
        """Get details for a video: title, channel, stats (views/likes/comments), duration."""
        if not video_id:
            return json.dumps({"status": "error", "error": "video_id is required"})
        try:
            data = _yt_get("/videos", {"part": "snippet,statistics,contentDetails", "id": video_id})
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        items = data.get("items") or []
        if not items:
            return json.dumps({"status": "not_found", "video_id": video_id})
        v = items[0]
        sn = v.get("snippet", {})
        st = v.get("statistics", {})
        cd = v.get("contentDetails", {})
        return json.dumps(
            {
                "status": "ok",
                "video": {
                    "video_id": v.get("id", ""),
                    "title": sn.get("title", ""),
                    "channel_title": sn.get("channelTitle", ""),
                    "channel_id": sn.get("channelId", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "duration": cd.get("duration", ""),
                    "view_count": st.get("viewCount", ""),
                    "like_count": st.get("likeCount", ""),
                    "comment_count": st.get("commentCount", ""),
                    "tags": sn.get("tags", []),
                    "description": sn.get("description", ""),
                },
            },
            indent=2,
        )

    @tool
    def youtube_channel_details(channel_id: str = "", handle: str = "") -> str:
        """Get channel details by ``channel_id`` or @handle: title, stats, uploads playlist."""
        if not channel_id and not handle:
            return json.dumps({"status": "error", "error": "channel_id or handle is required"})
        params = {"part": "snippet,statistics,contentDetails"}
        if channel_id:
            params["id"] = channel_id
        else:
            params["forHandle"] = handle.lstrip("@")
        try:
            data = _yt_get("/channels", params)
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        items = data.get("items") or []
        if not items:
            return json.dumps({"status": "not_found", "channel_id": channel_id, "handle": handle})
        c = items[0]
        sn = c.get("snippet", {})
        st = c.get("statistics", {})
        uploads = c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        return json.dumps(
            {
                "status": "ok",
                "channel": {
                    "channel_id": c.get("id", ""),
                    "title": sn.get("title", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "subscriber_count": st.get("subscriberCount", ""),
                    "video_count": st.get("videoCount", ""),
                    "view_count": st.get("viewCount", ""),
                    "uploads_playlist": uploads,
                    "description": sn.get("description", ""),
                },
            },
            indent=2,
        )

    @tool
    def youtube_playlist_items(playlist_id: str, limit: int = 25) -> str:
        """List videos in a playlist."""
        if not playlist_id:
            return json.dumps({"status": "error", "error": "playlist_id is required"})
        try:
            data = _yt_get(
                "/playlistItems",
                {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": max(1, min(int(limit), 50)),
                },
            )
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        videos = []
        for it in data.get("items") or []:
            sn = it.get("snippet", {})
            cd = it.get("contentDetails", {})
            videos.append(
                {
                    "video_id": cd.get("videoId", ""),
                    "title": sn.get("title", ""),
                    "channel_title": sn.get("videoOwnerChannelTitle", "") or sn.get("channelTitle", ""),
                    "published_at": cd.get("videoPublishedAt", "") or sn.get("publishedAt", ""),
                }
            )
        if not videos:
            return json.dumps({"status": "no_results", "playlist_id": playlist_id})
        return json.dumps(
            {"status": "ok", "playlist_id": playlist_id, "count": len(videos), "videos": videos},
            indent=2,
        )

    @tool
    def youtube_my_subscriptions(limit: int = 25) -> str:
        """List the connected account's channel subscriptions."""
        try:
            data = _yt_get(
                "/subscriptions",
                {
                    "part": "snippet",
                    "mine": "true",
                    "maxResults": max(1, min(int(limit), 50)),
                    "order": "alphabetical",
                },
            )
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        subs = []
        for it in data.get("items") or []:
            sn = it.get("snippet", {})
            subs.append(
                {
                    "channel_title": sn.get("title", ""),
                    "channel_id": sn.get("resourceId", {}).get("channelId", ""),
                    "description": sn.get("description", ""),
                }
            )
        if not subs:
            return json.dumps({"status": "no_results", "message": "No subscriptions found."})
        return json.dumps({"status": "ok", "count": len(subs), "subscriptions": subs}, indent=2)

    return [
        youtube_search,
        youtube_video_details,
        youtube_channel_details,
        youtube_playlist_items,
        youtube_my_subscriptions,
    ]
