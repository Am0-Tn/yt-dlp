import os
import re
import logging
import asyncio
import secrets
import time
from typing import Optional, List, Dict, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from cachetools import TTLCache
import httpx
import yt_dlp

# ==================== Configuration ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("media-link-api")

API_SECRET = os.getenv("API_SECRET", "")
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
MAX_CACHE_SIZE = 500
ENABLE_PROXY = os.getenv("ENABLE_PROXY", "false").lower() == "true"
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", 3))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 50))

cache = TTLCache(maxsize=MAX_CACHE_SIZE, ttl=CACHE_TTL)
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_start_time = time.time()

app = FastAPI(
    title="Media Link API",
    description="API مستقرة لاستخراج الروابط المباشرة – أفلام / مسلسلات / موسيقى",
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Security ====================
async def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-Api-Key")):
    if not API_SECRET:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing API key (header: X-Api-Key)")


# ==================== Models ====================
class LinkRequest(BaseModel):
    type: str = Field("movie", description="movie | tv | music")
    title: str = Field(..., min_length=1, description="اسم الفيلم / المسلسل / الأغنية")
    year: Optional[str] = Field(None, description="سنة الإصدار")
    tmdb_id: Optional[int] = Field(None, description="معرف TMDB (اختياري)")
    season: Optional[int] = Field(None, ge=1, description="رقم الموسم")
    episode: Optional[int] = Field(None, ge=1, description="رقم الحلقة")
    episode_title: Optional[str] = Field(None, description="اسم الحلقة")
    artist: Optional[str] = Field(None, description="اسم الفنان (للموسيقى)")
    quality: str = Field("best", description="best | 1080 | 720 | 480 | audio")
    language: Optional[str] = Field(None, description="لغة الترجمة المفضلة: ar | en")

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title cannot be empty")
        return v

    @field_validator("type")
    @classmethod
    def type_ok(cls, v: str) -> str:
        v = (v or "").lower().strip()
        if v not in ("movie", "tv", "music"):
            raise ValueError("type must be movie, tv, or music")
        return v

    @field_validator("quality")
    @classmethod
    def quality_ok(cls, v: str) -> str:
        v = (v or "best").lower().strip()
        if v not in ("best", "1080", "720", "480", "audio"):
            return "best"
        return v


class FormatInfo(BaseModel):
    quality: str
    ext: str
    height: Optional[int] = None
    fps: Optional[float] = None
    filesize: Optional[int] = None
    has_audio: bool = True
    has_video: bool = True
    url: str


class SubtitleInfo(BaseModel):
    language: str
    name: Optional[str] = None
    ext: Optional[str] = None
    url: str


class ExtractedInfo(BaseModel):
    source_title: str
    webpage_url: Optional[str] = None
    direct_url: Optional[str] = None
    duration: Optional[float] = None
    duration_string: Optional[str] = None
    thumbnail: Optional[str] = None
    formats: List[FormatInfo] = []
    subtitles: List[SubtitleInfo] = []
    auto_captions: List[SubtitleInfo] = []


class LinkResponse(BaseModel):
    success: bool
    type: str
    title: str
    year: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    artist: Optional[str] = None
    extracted: ExtractedInfo
    cached: bool = False
    proxy_url: Optional[str] = None


# ==================== Helpers ====================
def build_queries(data: LinkRequest) -> List[str]:
    queries: List[str] = []
    title = data.title.strip()
    year = (data.year or "").strip()

    if data.type == "music":
        base = f"{data.artist} - {title}" if data.artist else title
        queries = [
            f"{base} official audio",
            f"{base} official video",
            f"{base} lyrics",
            base,
        ]
        return _unique(queries)

    if data.type == "tv" and data.season is not None and data.episode is not None:
        se = f"S{data.season:02d}E{data.episode:02d}"
        if data.episode_title:
            ep = data.episode_title.strip()
            queries.append(f"{title} {se} {ep}")
            queries.append(f"{title} {ep}")
        queries.append(f"{title} {se}")
        queries.append(f"{title} season {data.season} episode {data.episode}")
        if year:
            queries.append(f"{title} {year} {se}")
    else:
        if year:
            queries.append(f"{title} {year} full movie")
            queries.append(f"{title} {year}")
        queries.append(f"{title} full movie")
        queries.append(title)

    return _unique(queries)


def _unique(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def get_ydl_opts(quality: str, media_type: str) -> dict:
    """إعدادات مرنة ومستقرة مع يوتيوب"""
    if media_type == "music" or quality == "audio":
        fmt = "bestaudio/best"
    else:
        format_map = {
            "best": "best",
            "1080": "best[height<=1080]/best",
            "720": "best[height<=720]/best",
            "480": "best[height<=480]/best",
        }
        fmt = format_map.get(quality, "best")

    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": fmt,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ar", "en", "en-US", "en-GB"],
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 1,
        "ignore_no_formats_error": True,
        "youtube_include_dash_manifest": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
            }
        },
    }


def title_similarity(a: str, b: str) -> float:
    a = re.sub(r"[^\w\s]", "", (a or "").lower())
    b = re.sub(r"[^\w\s]", "", (b or "").lower())
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def quality_label(height: Optional[int], has_video: bool) -> str:
    if not has_video:
        return "audio"
    if not height:
        return "unknown"
    if height >= 2160:
        return "2160p"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    if height >= 360:
        return "360p"
    return f"{height}p"


def parse_subtitles(sub_dict: Optional[dict], preferred: str = None) -> List[dict]:
    result = []
    if not sub_dict:
        return result
    for lang, entries in sub_dict.items():
        if not entries:
            continue
        best = next((e for e in entries if e.get("ext") in ("vtt", "srt")), entries[0])
        if not best.get("url"):
            continue
        result.append({
            "language": lang,
            "name": best.get("name") or lang,
            "ext": best.get("ext"),
            "url": best["url"],
        })
    if preferred:
        result.sort(key=lambda s: (0 if preferred.lower() in s["language"].lower() else 1, s["language"]))
    return result[:10]


def extract_with_yt_dlp(
    queries: List[str],
    quality: str,
    media_type: str,
    expected_title: str,
    preferred_lang: str = None,
) -> Dict[str, Any]:
    ydl_opts = get_ydl_opts(quality, media_type)
    best_result = None
    best_score = -1.0
    last_error = None

    for query in queries:
        search = query if query.startswith(("http://", "https://", "ytsearch")) else f"ytsearch5:{query}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search, download=False)
                if not info:
                    continue

                candidates = info.get("entries") or [info]

                for video in candidates:
                    if not video or not video.get("id"):
                        continue

                    if not video.get("formats") and not video.get("url"):
                        try:
                            video = ydl.extract_info(
                                video.get("url") or video.get("webpage_url") or f"https://www.youtube.com/watch?v={video['id']}",
                                download=False
                            )
                        except Exception:
                            continue
                        if not video:
                            continue

                    vtitle = video.get("title") or ""
                    score = title_similarity(expected_title, vtitle) * 55.0

                    lower = vtitle.lower()
                    if any(w in lower for w in ("official", "full movie", "full episode", "official audio", "full film")):
                        score += 18
                    if any(w in lower for w in ("trailer", "teaser", "clip", "scene", "reaction", "review", "behind the scenes")):
                        score -= 35

                    duration = video.get("duration") or 0
                    if media_type == "movie" and 2400 < duration < 14400:
                        score += 12
                    elif media_type == "tv" and 900 < duration < 5400:
                        score += 12
                    elif media_type == "music" and 60 < duration < 900:
                        score += 15

                    if score > best_score:
                        best_score = score
                        best_result = video
                        if score >= 70:
                            break

                if best_score >= 70:
                    break

        except Exception as e:
            last_error = str(e)
            logger.warning("Query failed [%s]: %s", query[:70], e)
            continue

    if not best_result:
        raise HTTPException(
            status_code=404,
            detail=f"No suitable result found. Last error: {last_error or 'unknown'}"
        )

    video = best_result

    formats = []
    seen = set()
    for f in (video.get("formats") or []):
        url = f.get("url")
        if not url:
            continue
        if f.get("ext") == "mhtml" or "storyboard" in str(f.get("format_note", "")).lower():
            continue
        if f.get("protocol") == "mhtml":
            continue
        has_video = f.get("vcodec") not in (None, "none")
        has_audio = f.get("acodec") not in (None, "none")
        if not has_video and not has_audio:
            continue
        height = f.get("height")
        q = quality_label(height, has_video)
        ext = f.get("ext") or "unknown"
        key = (q, ext, has_video, has_audio)
        if key in seen:
            continue
        seen.add(key)
        formats.append({
            "quality": q,
            "ext": ext,
            "height": height,
            "fps": f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "has_audio": has_audio,
            "has_video": has_video,
            "url": url,
        })

    formats.sort(key=lambda x: (x.get("height") or 0, x.get("filesize") or 0), reverse=True)
    formats = formats[:10]

    direct_url = video.get("url")
    if not direct_url and formats:
        for f in formats:
            if f["has_video"] and f["has_audio"]:
                direct_url = f["url"]
                break
        if not direct_url:
            direct_url = formats[0]["url"]

    return {
        "source_title": video.get("title") or expected_title,
        "webpage_url": video.get("webpage_url") or video.get("original_url"),
        "direct_url": direct_url,
        "duration": video.get("duration"),
        "duration_string": video.get("duration_string"),
        "thumbnail": video.get("thumbnail"),
        "formats": formats,
        "subtitles": parse_subtitles(video.get("subtitles"), preferred_lang),
        "auto_captions": parse_subtitles(video.get("automatic_captions"), preferred_lang),
    }


# ==================== Endpoints ====================
@app.get("/")
async def root():
    return {
        "status": "ok",
        "version": "3.1.0",
        "uptime_seconds": int(time.time() - _start_time),
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "3.1.0",
        "cache_size": len(cache),
        "uptime_seconds": int(time.time() - _start_time),
        "proxy_enabled": ENABLE_PROXY,
        "api_key_required": bool(API_SECRET),
    }


@app.post("/get-link", response_model=LinkResponse, dependencies=[Depends(verify_api_key)])
async def get_link(data: LinkRequest, request: Request):
    if data.type == "tv" and (data.season is None or data.episode is None):
        raise HTTPException(status_code=400, detail="For type=tv you must send season and episode")

    cache_key = (
        f"{data.type}|{data.title.lower()}|{data.year}|{data.season}|{data.episode}|"
        f"{(data.episode_title or '').lower()}|{(data.artist or '').lower()}|{data.quality}|{data.language}"
    )
    if cache_key in cache:
        cached = cache[cache_key]
        cached["cached"] = True
        return cached

    queries = build_queries(data)
    logger.info("Search [%s] → %s", data.type, queries[0] if queries else data.title)

    async with _semaphore:
        loop = asyncio.get_running_loop()
        try:
            result_data = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: extract_with_yt_dlp(
                        queries=queries,
                        quality=data.quality,
                        media_type=data.type,
                        expected_title=data.title,
                        preferred_lang=data.language,
                    ),
                ),
                timeout=REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Extraction timed out. Try again.")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Extraction error")
            raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)[:180]}")

    proxy_url = None
    if ENABLE_PROXY and result_data.get("direct_url"):
        base = str(request.base_url).rstrip("/")
        proxy_url = f"{base}/stream?url={quote(result_data['direct_url'], safe='')}"

    response = LinkResponse(
        success=True,
        type=data.type,
        title=data.title,
        year=data.year,
        season=data.season,
        episode=data.episode,
        episode_title=data.episode_title,
        artist=data.artist,
        extracted=ExtractedInfo(
            source_title=result_data["source_title"],
            webpage_url=result_data.get("webpage_url"),
            direct_url=result_data.get("direct_url"),
            duration=result_data.get("duration"),
            duration_string=result_data.get("duration_string"),
            thumbnail=result_data.get("thumbnail"),
            formats=[FormatInfo(**f) for f in result_data.get("formats", [])],
            subtitles=[SubtitleInfo(**s) for s in result_data.get("subtitles", [])],
            auto_captions=[SubtitleInfo(**s) for s in result_data.get("auto_captions", [])],
        ),
        cached=False,
        proxy_url=proxy_url,
    )

    cache[cache_key] = response.model_dump()
    return response


@app.get("/stream", dependencies=[Depends(verify_api_key)])
async def stream_proxy(url: str = Query(..., min_length=10)):
    if not ENABLE_PROXY:
        raise HTTPException(status_code=403, detail="Proxy is disabled. Set ENABLE_PROXY=true")

    async def iterfile():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail="Upstream error")
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    media_type = "application/octet-stream"
    lower = url.lower()
    if ".m3u8" in lower:
        media_type = "application/vnd.apple.mpegurl"
    elif any(x in lower for x in (".m4a", ".mp3", ".opus", ".aac")):
        media_type = "audio/mpeg"
    elif any(x in lower for x in (".mp4", ".webm", ".mkv")):
        media_type = "video/mp4"

    return StreamingResponse(
        iterfile(),
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"success": False, "detail": exc.detail})
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "detail": "Internal server error"})
