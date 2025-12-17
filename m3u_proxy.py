#!/usr/bin/env python3
"""
Transparent Xtream Codes Normalizing Proxy for Dispatcharr

HIGH-PERFORMANCE ASYNC VERSION with:
- FastAPI + Uvicorn (async, production-ready)
- Connection pooling (aiohttp ClientSession)
- Gzip compression
- LRU caching for normalized names

NO CONFIGURATION NEEDED - just run it!

The proxy extracts provider info from the username field:
  Username format: realuser@provider.com:port
  
Example in Dispatcharr:
  Server:   your-proxy-ip
  Port:     8765
  Username: john@iptv-provider.com:8080
  Password: secretpass

The proxy will:
  1. Parse "john@iptv-provider.com:8080" 
  2. Connect to http://iptv-provider.com:8080 with user "john"
  3. Normalize all channel names
  4. Return clean data to Dispatcharr

Supports multiple providers - just add different accounts in Dispatcharr!
"""

import os
import re
import json
import time
import hashlib
import asyncio
from functools import lru_cache
from contextlib import asynccontextmanager
from urllib.parse import urlencode
import logging

import aiohttp
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Server settings
PORT = int(os.environ.get('PORT', 8765))
CACHE_HOURS = float(os.environ.get('CACHE_HOURS', 6))

# In-memory cache: key -> {'content': ..., 'timestamp': ...}
cache = {}

# Global aiohttp session (connection pooling)
http_session: aiohttp.ClientSession = None


# =============================================================================
# PRE-COMPILED REGEX PATTERNS (for performance)
# =============================================================================

# Phase 1: Provider wrappers
RE_WRAPPER = re.compile(r'┃[^┃]+┃\s*')
RE_NL_COLON = re.compile(r'^NL:\s*')
RE_NL_PIPE = re.compile(r'^NL\|\s*')
RE_UK_PIPE = re.compile(r'^UK\|\s*')
RE_PLAY_PLUS = re.compile(r'^PLAY\+:\s*')
RE_OD = re.compile(r'^OD:\s*')

# Phase 2: Special Unicode characters
RE_VIP = re.compile(r'\s*ⱽᴵᴾ\s*')
RE_RAW = re.compile(r'\s*ᴿᴬᵂ\s*')
RE_HD_SUPER = re.compile(r'\s*ᴴᴰ\s*')
RE_UHD_SUPER = re.compile(r'\s*ᵁᴴᴰ\s*')
RE_GOLD = re.compile(r'\s*ᴳᴼᴸᴰ\s*')
RE_REC = re.compile(r'\s*⏺ʳᵉᶜ\s*')
RE_CIRCLE = re.compile(r'\s*◉\s*')
RE_UHD_3840 = re.compile(r'\s*ᵁᴴᴰ\s*³⁸⁴⁰ᴾ\s*')
RE_BARS = re.compile(r'☰+\s*|\s*☰+')
RE_TRIPLE = re.compile(r'≡+\s*|\s*≡+')

# Phase 3: Quality/format tags
RE_8K_EXCLUSIVE = re.compile(r'\s*\|\s*8K\s*EXCLUSIVE\s*')
RE_NO_EVENT = re.compile(r'\s*-\s*NO EVENT STREAMING\s*-?\s*')
RE_COLON_START = re.compile(r'^:\s*')
RE_PIPE_END = re.compile(r'\s*\|\s*$')
RE_8K_PLUS_UHD = re.compile(r'\s+8K\+\s*UHD\s*$', re.IGNORECASE)
RE_8K_PLUS = re.compile(r'\s+8K\+\s*$', re.IGNORECASE)
RE_QUALITY = re.compile(r'\s+(HD|FHD|4K|8K|UHD|SD|LQ|HEVC)\s*$', re.IGNORECASE)
RE_FHD_50FPS = re.compile(r'\s+FHD\s+50FPS\s*$', re.IGNORECASE)

# Phase 4: Dutch channel spacing
RE_NPO = re.compile(r'^NPO(\d)', re.IGNORECASE)
RE_SBS = re.compile(r'^SBS(\d)', re.IGNORECASE)
RE_NET5 = re.compile(r'^Net\s*5$', re.IGNORECASE)
RE_ESPN = re.compile(r'^ESPN(\d)', re.IGNORECASE)
RE_RTL = re.compile(r'^RTL(\d)', re.IGNORECASE)
RE_FILM1 = re.compile(r'^Film\s*1\s+', re.IGNORECASE)
RE_NUM_LETTER = re.compile(r'^(\d+)([A-Za-z])')

# Phase 5: TV/LITE suffix spacing
RE_LETTER_TV = re.compile(r'([A-Za-z])TV$', re.IGNORECASE)
RE_PUNCT_TV = re.compile(r'([!?])TV$', re.IGNORECASE)
RE_NUM_TV = re.compile(r'(\d)TV$', re.IGNORECASE)
RE_TV_NUM = re.compile(r'TV(\d)', re.IGNORECASE)
RE_LITE_TV = re.compile(r'([A-Za-z])LITE\s*TV$', re.IGNORECASE)
RE_LITE = re.compile(r'([A-Za-z])LITE$', re.IGNORECASE)

# Phase 6: Decade apostrophes
RE_DECADE = re.compile(r"(\d+)'s", re.IGNORECASE)

# Phase 7: Multi-space cleanup
RE_MULTI_SPACE = re.compile(r'\s{2,}')

# Event detection patterns
RE_EVENT_DATE = re.compile(r'@\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d', re.IGNORECASE)
RE_F1_PREFIX = re.compile(r'^F1:', re.IGNORECASE)
RE_VIAPLAY_F = re.compile(r'VIAPLAY\s+F[123]', re.IGNORECASE)
RE_SPORT_EVENT = re.compile(r'^[A-Za-z\s\-]+:\s+.+@')

# Skip channel patterns
RE_HASH_HEADER = re.compile(r'^#{2,}.*#{2,}$')
RE_BAR_HEADER = re.compile(r'^☰+.*☰+$')
RE_TRIPLE_HEADER = re.compile(r'^≡+.*≡+$')
RE_DASH_LINE = re.compile(r'^-+$')

# M3U processing
RE_TVG_NAME = re.compile(r'tvg-name="([^"]*)"')

# Accent translation table (much faster than multiple replace() calls)
ACCENT_TABLE = str.maketrans({
    'â': 'A', 'Â': 'A', 'ê': 'E', 'Ê': 'E', 'î': 'I', 'Î': 'I',
    'ô': 'O', 'Ô': 'O', 'û': 'U', 'Û': 'U', 'ë': 'E', 'Ë': 'E',
    'ï': 'I', 'Ï': 'I', 'ü': 'U', 'Ü': 'U', 'é': 'E', 'É': 'E',
    'è': 'E', 'È': 'E', 'à': 'A', 'À': 'A', 'ö': 'O', 'Ö': 'O',
    'ä': 'A', 'Ä': 'A'
})


# =============================================================================
# CREDENTIAL PARSING
# =============================================================================

def parse_credentials(username: str, password: str) -> dict:
    """
    Parse the username to extract provider info.
    
    Format: realuser@host:port  OR  realuser@http://host:port
    """
    if '@' not in username:
        return None
    
    at_pos = username.rfind('@')
    real_user = username[:at_pos]
    host_part = username[at_pos + 1:]
    
    if host_part.startswith('http://'):
        host_part = host_part[7:]
    elif host_part.startswith('https://'):
        host_part = host_part[8:]
    
    if ':' in host_part:
        host, port = host_part.rsplit(':', 1)
        try:
            port = int(port)
        except ValueError:
            port = 80
    else:
        host = host_part
        port = 80
    
    return {
        'username': real_user,
        'password': password,
        'host': host,
        'port': port,
        'base_url': f"http://{host}:{port}"
    }


# =============================================================================
# NORMALIZATION RULES (with LRU cache)
# =============================================================================

@lru_cache(maxsize=10000)
def normalize_channel_name(name: str) -> str:
    """
    Apply all normalization rules to a channel name.
    LRU cached - repeated names are instant.
    """
    if not name:
        return name
    
    if is_event_channel(name):
        return name
    
    # === PHASE 1: Remove provider wrappers ===
    name = RE_WRAPPER.sub('', name)
    name = RE_NL_COLON.sub('', name)
    name = RE_NL_PIPE.sub('', name)
    name = RE_UK_PIPE.sub('', name)
    name = RE_PLAY_PLUS.sub('', name)
    name = RE_OD.sub('', name)
    
    # === PHASE 2: Remove special Unicode characters ===
    name = RE_VIP.sub('', name)
    name = RE_RAW.sub('', name)
    name = RE_HD_SUPER.sub('', name)
    name = RE_UHD_SUPER.sub('', name)
    name = RE_GOLD.sub('', name)
    name = RE_REC.sub('', name)
    name = RE_CIRCLE.sub('', name)
    name = RE_UHD_3840.sub('', name)
    name = RE_BARS.sub('', name)
    name = RE_TRIPLE.sub('', name)
    
    # === PHASE 3: Remove quality/format tags ===
    name = RE_8K_EXCLUSIVE.sub('', name)
    name = RE_NO_EVENT.sub(' ', name)
    name = RE_COLON_START.sub('', name)
    name = RE_PIPE_END.sub('', name)
    name = RE_8K_PLUS_UHD.sub('', name)
    name = RE_8K_PLUS.sub('', name)
    name = RE_QUALITY.sub('', name)
    name = RE_FHD_50FPS.sub('', name)
    
    # === PHASE 4: Normalize spacing for Dutch channels ===
    name = RE_NPO.sub(r'NPO \1', name)
    name = RE_SBS.sub(r'SBS \1', name)
    name = RE_NET5.sub('NET 5', name)
    name = RE_ESPN.sub(r'ESPN \1', name)
    name = RE_RTL.sub(r'RTL \1', name)
    name = RE_FILM1.sub('Film1 ', name)
    name = RE_NUM_LETTER.sub(r'\1 \2', name)
    
    # === PHASE 5: Add space before TV/LITE suffix when missing ===
    name = RE_LETTER_TV.sub(r'\1 TV', name)
    name = RE_PUNCT_TV.sub(r'\1 TV', name)
    name = RE_NUM_TV.sub(r'\1 TV', name)
    name = RE_TV_NUM.sub(r'TV \1', name)
    name = RE_LITE_TV.sub(r'\1 LITE TV', name)
    name = RE_LITE.sub(r'\1 LITE', name)
    
    # === PHASE 6: Normalize special characters ===
    name = name.translate(ACCENT_TABLE)
    name = RE_DECADE.sub(r'\1S', name)
    
    # === PHASE 7: Final cleanup and UPPERCASE ===
    name = RE_MULTI_SPACE.sub(' ', name)
    name = name.strip().upper()
    
    return name


def should_skip_channel(name: str) -> bool:
    """Check if this is a header/placeholder."""
    if not name:
        return True
    if RE_HASH_HEADER.match(name):
        return True
    if RE_BAR_HEADER.match(name):
        return True
    if RE_TRIPLE_HEADER.match(name):
        return True
    if RE_DASH_LINE.match(name):
        return True
    return False


@lru_cache(maxsize=10000)
def is_event_channel(name: str) -> bool:
    """Check if this is an event/PPV/F1 channel. LRU cached."""
    if not name:
        return False
    
    if RE_EVENT_DATE.search(name):
        return True
    if RE_F1_PREFIX.match(name):
        return True
    if '┃F1 TV┃' in name or '┃F1TV┃' in name:
        return True
    if RE_VIAPLAY_F.search(name):
        return True
    
    name_upper = name.upper()
    if 'FORMULE' in name_upper or 'MOTOGP' in name_upper:
        return True
    
    if RE_SPORT_EVENT.match(name):
        return True
    
    return False


# =============================================================================
# ASYNC HTTP HELPERS
# =============================================================================

async def fetch_url(url: str, timeout: int = 60) -> bytes:
    """Fetch a URL using connection-pooled session."""
    async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        return await response.read()


async def fetch_url_text(url: str, timeout: int = 60) -> str:
    """Fetch a URL and return text."""
    async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        return await response.text()


def get_cache_key(provider: dict, endpoint: str, params: dict = None) -> str:
    """Generate a cache key for a request."""
    key_data = f"{provider['base_url']}:{endpoint}:{json.dumps(params or {}, sort_keys=True)}"
    return hashlib.md5(key_data.encode()).hexdigest()


def get_cached(key: str) -> str:
    """Get from cache if fresh."""
    if key in cache:
        age_hours = (time.time() - cache[key]['timestamp']) / 3600
        if age_hours < CACHE_HOURS:
            return cache[key]['content']
    return None


def set_cache(key: str, content: str):
    """Store in cache."""
    cache[key] = {'content': content, 'timestamp': time.time()}


# =============================================================================
# DATA PROCESSING
# =============================================================================

def process_streams_json(data: list) -> list:
    """Normalize names in stream list."""
    if not isinstance(data, list):
        return data
    
    processed = []
    for stream in data:
        if isinstance(stream, dict) and 'name' in stream:
            if not should_skip_channel(stream['name']):
                stream['name'] = normalize_channel_name(stream['name'])
                processed.append(stream)
        else:
            processed.append(stream)
    return processed


def process_m3u(content: str) -> str:
    """Normalize M3U playlist."""
    lines = content.split('\n')
    output = []
    i = 0
    num_lines = len(lines)
    
    while i < num_lines:
        line = lines[i]
        
        if line.startswith('#EXTINF:'):
            comma_pos = line.rfind(',')
            if comma_pos != -1:
                prefix = line[:comma_pos + 1]
                name = line[comma_pos + 1:]
                
                if should_skip_channel(name):
                    i += 2
                    continue
                
                name = normalize_channel_name(name)
                prefix = RE_TVG_NAME.sub(
                    lambda m: f'tvg-name="{normalize_channel_name(m.group(1))}"',
                    prefix
                )
                line = prefix + name
            output.append(line)
        else:
            output.append(line)
        i += 1
    
    return '\n'.join(output)


# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global http_session
    
    # Startup: Create connection-pooled HTTP session
    connector = aiohttp.TCPConnector(
        limit=100,  # Max concurrent connections
        limit_per_host=30,  # Max per host
        ttl_dns_cache=300,  # DNS cache 5 minutes
        keepalive_timeout=60  # Keep connections alive
    )
    http_session = aiohttp.ClientSession(
        connector=connector,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    )
    
    logger.info("=" * 60)
    logger.info("Xtream Codes Normalizing Proxy (Async + FastAPI)")
    logger.info("=" * 60)
    logger.info("")
    logger.info("Performance features:")
    logger.info("  ✓ Async I/O (handles thousands of connections)")
    logger.info("  ✓ Connection pooling (reuses upstream connections)")
    logger.info("  ✓ Gzip compression (smaller responses)")
    logger.info("  ✓ LRU cache for normalized names")
    logger.info("")
    logger.info(f"Server running on port {PORT}")
    logger.info(f"Cache duration: {CACHE_HOURS} hours")
    logger.info("=" * 60)
    
    yield
    
    # Shutdown: Close HTTP session
    await http_session.close()


app = FastAPI(title="M3U Normalizing Proxy", lifespan=lifespan)

# Add Gzip compression (min 500 bytes)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return PlainTextResponse("OK")


@app.get("/cache")
async def cache_stats():
    """Cache statistics."""
    stats = {k: {'age_hours': round((time.time() - v['timestamp']) / 3600, 2)} 
             for k, v in cache.items()}
    norm_cache_info = normalize_channel_name.cache_info()
    return JSONResponse({
        'entries': len(cache),
        'items': stats,
        'normalization_cache': {
            'hits': norm_cache_info.hits,
            'misses': norm_cache_info.misses,
            'size': norm_cache_info.currsize
        }
    })


@app.get("/clear")
async def clear_cache():
    """Clear all caches."""
    cache.clear()
    normalize_channel_name.cache_clear()
    is_event_channel.cache_clear()
    return PlainTextResponse("Cache cleared!")


@app.get("/live/{username}/{password}/{stream_path:path}")
@app.get("/movie/{username}/{password}/{stream_path:path}")
@app.get("/series/{username}/{password}/{stream_path:path}")
async def proxy_stream(username: str, password: str, stream_path: str, request: Request):
    """Proxy live/VOD/series streams."""
    stream_type = request.url.path.split('/')[1]
    
    provider = parse_credentials(username, password)
    if not provider:
        return PlainTextResponse("Invalid credentials", status_code=400)
    
    real_url = f"{provider['base_url']}/{stream_type}/{provider['username']}/{provider['password']}/{stream_path}"
    
    logger.info(f"Stream: {stream_type}/{stream_path} -> {provider['host']}:{provider['port']}")
    
    async def stream_response():
        async with http_session.get(real_url) as response:
            async for chunk in response.content.iter_chunked(65536):
                yield chunk
    
    return StreamingResponse(stream_response(), media_type="application/octet-stream")


@app.get("/get.php")
@app.get("/playlist.m3u")
@app.get("/")
async def get_playlist(username: str = None, password: str = None):
    """Get and normalize M3U playlist."""
    if not username or not password:
        return JSONResponse({'error': 'Missing username or password'}, status_code=400)
    
    provider = parse_credentials(username, password)
    if not provider:
        return JSONResponse({'error': 'Invalid username format'}, status_code=400)
    
    cache_key = get_cache_key(provider, 'm3u')
    cached = get_cached(cache_key)
    
    if cached:
        logger.info("Serving M3U from cache")
        return Response(content=cached, media_type="application/x-mpegurl")
    
    try:
        url = (f"{provider['base_url']}/get.php?"
               f"username={provider['username']}&password={provider['password']}"
               f"&type=m3u_plus&output=ts")
        raw = await fetch_url_text(url)
        processed = process_m3u(raw)
        set_cache(cache_key, processed)
        return Response(content=processed, media_type="application/x-mpegurl")
    except Exception as e:
        logger.error(f"M3U fetch error: {e}")
        return PlainTextResponse(f"Error: {e}", status_code=500)


@app.get("/player_api.php")
async def player_api(request: Request, username: str = None, password: str = None, action: str = None):
    """Xtream Codes API endpoint."""
    if not username or not password:
        return JSONResponse({'error': 'Missing username or password'}, status_code=400)
    
    provider = parse_credentials(username, password)
    if not provider:
        return JSONResponse({'error': 'Invalid username format'}, status_code=400)
    
    logger.info(f"API: {action or 'auth'} -> {provider['host']}:{provider['port']}")
    
    # Build upstream URL
    real_params = {
        'username': provider['username'],
        'password': provider['password'],
    }
    if action:
        real_params['action'] = action
    
    # Add extra params
    for key, value in request.query_params.items():
        if key not in ['username', 'password', 'action']:
            real_params[key] = value
    
    real_url = f"{provider['base_url']}/player_api.php?{urlencode(real_params)}"
    cache_key = get_cache_key(provider, 'api', real_params)
    
    # Only normalize live IPTV streams, not VOD/Series (titles need different handling)
    normalize_actions = ['get_live_streams']
    no_cache_actions = ['get_short_epg', 'get_simple_data_table']
    
    # Check cache
    if action not in no_cache_actions:
        cached = get_cached(cache_key)
        if cached:
            logger.info(f"Serving {action or 'auth'} from cache")
            return Response(content=cached, media_type="application/json")
    
    try:
        raw = await fetch_url_text(real_url)
        
        if action in normalize_actions:
            data = json.loads(raw)
            data = process_streams_json(data)
            raw = json.dumps(data)
        
        if action not in no_cache_actions:
            set_cache(cache_key, raw)
        
        return Response(content=raw, media_type="application/json")
        
    except aiohttp.ClientError as e:
        logger.error(f"Upstream error: {e}")
        return JSONResponse({'error': str(e)}, status_code=502)
    except Exception as e:
        logger.error(f"API error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get("/xmltv.php")
async def get_epg(username: str = None, password: str = None):
    """Get EPG/XMLTV data."""
    if not username or not password:
        return JSONResponse({'error': 'Missing credentials'}, status_code=400)
    
    provider = parse_credentials(username, password)
    if not provider:
        return JSONResponse({'error': 'Invalid username format'}, status_code=400)
    
    url = (f"{provider['base_url']}/xmltv.php?"
           f"username={provider['username']}&password={provider['password']}")
    
    try:
        data = await fetch_url(url, timeout=120)
        return Response(content=data, media_type="application/xml")
    except Exception as e:
        return PlainTextResponse(f"Error: {e}", status_code=500)


@app.get("/panel_api.php")
async def panel_api(request: Request, username: str = None, password: str = None):
    """Panel API endpoint."""
    if not username or not password:
        return JSONResponse({'error': 'Missing credentials'}, status_code=400)
    
    provider = parse_credentials(username, password)
    if not provider:
        return JSONResponse({'error': 'Invalid username format'}, status_code=400)
    
    real_params = {
        'username': provider['username'],
        'password': provider['password'],
    }
    for key, value in request.query_params.items():
        if key not in ['username', 'password']:
            real_params[key] = value
    
    real_url = f"{provider['base_url']}/panel_api.php?{urlencode(real_params)}"
    
    try:
        raw = await fetch_url_text(real_url)
        return Response(content=raw, media_type="application/json")
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    uvicorn.run(
        "m3u_proxy:app",
        host="0.0.0.0",
        port=PORT,
        workers=4,  # Multiple worker processes
        log_level="info",
        access_log=False  # Disable access log for performance
    )
