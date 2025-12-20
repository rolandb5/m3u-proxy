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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List
from datetime import datetime
import statistics
import logging

import aiohttp
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
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

# Groups that should NOT have (provider) (group) suffixes added
# These are typically groups with "Auto Channel Sync" enabled in Dispatcharr
# Format: comma-separated list of group name patterns (case-insensitive substring match)
# Example: "PPV,EVENT,VIAPLAY,DAZN,FEYENOORD,MAX PPV,ESPN PPV"
NO_SUFFIX_GROUPS_RAW = os.environ.get('NO_SUFFIX_GROUPS', '')
NO_SUFFIX_GROUPS = [g.strip().upper() for g in NO_SUFFIX_GROUPS_RAW.split(',') if g.strip()]

def should_skip_suffix(group_name: str) -> bool:
    """Check if this group should NOT have (provider) (group) suffix added."""
    if not NO_SUFFIX_GROUPS or not group_name:
        return False
    group_upper = group_name.upper()
    for pattern in NO_SUFFIX_GROUPS:
        if pattern in group_upper:
            return True
    return False

# In-memory cache: key -> {'content': ..., 'timestamp': ...}
cache = {}

# Global aiohttp session (connection pooling)
http_session: aiohttp.ClientSession = None


# =============================================================================
# PERFORMANCE MONITORING
# =============================================================================

@dataclass
class EndpointStats:
    """Statistics for a single endpoint."""
    requests: int = 0
    errors: int = 0
    response_times: List[float] = field(default_factory=list)
    bytes_sent: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    last_request: float = 0
    
    def record_request(self, duration_ms: float, bytes_sent: int = 0, error: bool = False, cache_hit: bool = False):
        self.requests += 1
        self.last_request = time.time()
        self.bytes_sent += bytes_sent
        if error:
            self.errors += 1
        if cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        # Keep last 1000 response times for percentile calculations
        self.response_times.append(duration_ms)
        if len(self.response_times) > 1000:
            self.response_times = self.response_times[-1000:]
    
    def get_percentile(self, p: int) -> float:
        if not self.response_times:
            return 0
        sorted_times = sorted(self.response_times)
        idx = int(len(sorted_times) * p / 100)
        return sorted_times[min(idx, len(sorted_times) - 1)]
    
    def to_dict(self) -> dict:
        return {
            'requests': self.requests,
            'errors': self.errors,
            'error_rate': f"{(self.errors / self.requests * 100):.1f}%" if self.requests > 0 else "0%",
            'cache_hit_rate': f"{(self.cache_hits / self.requests * 100):.1f}%" if self.requests > 0 else "0%",
            'bytes_sent': self.bytes_sent,
            'bytes_sent_human': format_bytes(self.bytes_sent),
            'response_time_ms': {
                'avg': round(statistics.mean(self.response_times), 1) if self.response_times else 0,
                'min': round(min(self.response_times), 1) if self.response_times else 0,
                'max': round(max(self.response_times), 1) if self.response_times else 0,
                'p50': round(self.get_percentile(50), 1),
                'p95': round(self.get_percentile(95), 1),
                'p99': round(self.get_percentile(99), 1),
            },
            'last_request': datetime.fromtimestamp(self.last_request).isoformat() if self.last_request else None
        }


class PerformanceMonitor:
    """Tracks performance metrics for the proxy."""
    
    def __init__(self):
        self.start_time = time.time()
        self.endpoints: Dict[str, EndpointStats] = defaultdict(EndpointStats)
        self.upstream_times: List[float] = []
        self.total_upstream_requests = 0
        self.total_upstream_errors = 0
        self.active_requests = 0
        self.peak_active_requests = 0
    
    def record_request(self, endpoint: str, duration_ms: float, bytes_sent: int = 0, 
                       error: bool = False, cache_hit: bool = False):
        self.endpoints[endpoint].record_request(duration_ms, bytes_sent, error, cache_hit)
    
    def record_upstream(self, duration_ms: float, error: bool = False):
        self.total_upstream_requests += 1
        if error:
            self.total_upstream_errors += 1
        self.upstream_times.append(duration_ms)
        if len(self.upstream_times) > 1000:
            self.upstream_times = self.upstream_times[-1000:]
    
    def request_started(self):
        self.active_requests += 1
        self.peak_active_requests = max(self.peak_active_requests, self.active_requests)
    
    def request_finished(self):
        self.active_requests = max(0, self.active_requests - 1)
    
    def get_upstream_percentile(self, p: int) -> float:
        if not self.upstream_times:
            return 0
        sorted_times = sorted(self.upstream_times)
        idx = int(len(sorted_times) * p / 100)
        return sorted_times[min(idx, len(sorted_times) - 1)]
    
    def get_stats(self) -> dict:
        uptime_seconds = time.time() - self.start_time
        total_requests = sum(e.requests for e in self.endpoints.values())
        total_errors = sum(e.errors for e in self.endpoints.values())
        total_bytes = sum(e.bytes_sent for e in self.endpoints.values())
        
        # Get LRU cache stats
        norm_cache = normalize_channel_name.cache_info()
        event_cache = is_event_channel.cache_info()
        
        return {
            'uptime': format_duration(uptime_seconds),
            'uptime_seconds': round(uptime_seconds, 1),
            'active_requests': self.active_requests,
            'peak_active_requests': self.peak_active_requests,
            'totals': {
                'requests': total_requests,
                'errors': total_errors,
                'error_rate': f"{(total_errors / total_requests * 100):.1f}%" if total_requests > 0 else "0%",
                'bytes_sent': total_bytes,
                'bytes_sent_human': format_bytes(total_bytes),
                'requests_per_minute': round(total_requests / (uptime_seconds / 60), 2) if uptime_seconds > 0 else 0,
            },
            'upstream': {
                'requests': self.total_upstream_requests,
                'errors': self.total_upstream_errors,
                'error_rate': f"{(self.total_upstream_errors / self.total_upstream_requests * 100):.1f}%" if self.total_upstream_requests > 0 else "0%",
                'response_time_ms': {
                    'avg': round(statistics.mean(self.upstream_times), 1) if self.upstream_times else 0,
                    'min': round(min(self.upstream_times), 1) if self.upstream_times else 0,
                    'max': round(max(self.upstream_times), 1) if self.upstream_times else 0,
                    'p50': round(self.get_upstream_percentile(50), 1),
                    'p95': round(self.get_upstream_percentile(95), 1),
                    'p99': round(self.get_upstream_percentile(99), 1),
                }
            },
            'caches': {
                'response_cache': {
                    'entries': len(cache),
                    'max_age_hours': CACHE_HOURS
                },
                'normalization_cache': {
                    'hits': norm_cache.hits,
                    'misses': norm_cache.misses,
                    'size': norm_cache.currsize,
                    'max_size': norm_cache.maxsize,
                    'hit_rate': f"{(norm_cache.hits / (norm_cache.hits + norm_cache.misses) * 100):.1f}%" if (norm_cache.hits + norm_cache.misses) > 0 else "0%"
                },
                'event_detection_cache': {
                    'hits': event_cache.hits,
                    'misses': event_cache.misses,
                    'size': event_cache.currsize,
                    'hit_rate': f"{(event_cache.hits / (event_cache.hits + event_cache.misses) * 100):.1f}%" if (event_cache.hits + event_cache.misses) > 0 else "0%"
                }
            },
            'endpoints': {name: stats.to_dict() for name, stats in sorted(self.endpoints.items())}
        }


def format_bytes(b: int) -> str:
    """Format bytes as human readable."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def format_duration(seconds: float) -> str:
    """Format seconds as human readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds // 60:.0f}m {seconds % 60:.0f}s"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:.0f}h {minutes:.0f}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days:.0f}d {hours:.0f}h"


# Global performance monitor
perf = PerformanceMonitor()


# =============================================================================
# PRE-COMPILED REGEX PATTERNS (for performance)
# =============================================================================

# Phase 1: Provider wrappers
RE_WRAPPER = re.compile(r'┃[^┃]+┃\s*')
RE_NL_COLON = re.compile(r'^NL:\s*', re.IGNORECASE)
RE_NL_PIPE = re.compile(r'^NL\|\s*', re.IGNORECASE)
RE_UK_PIPE = re.compile(r'^UK\|\s*', re.IGNORECASE)
RE_PLAY_PLUS = re.compile(r'^PLAY\+:\s*', re.IGNORECASE)
RE_OD = re.compile(r'^OD:\s*', re.IGNORECASE)
RE_ZONE_PLUS = re.compile(r'^ZONE\+\s*', re.IGNORECASE)  # "ZONE+ " prefix
# Additional country prefixes
RE_COUNTRY_PREFIX = re.compile(r'^[A-Z]{2}:\s*', re.IGNORECASE)  # Any "XX: " prefix

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
# CHANNEL NAME ALIASES (map variations to canonical names)
# =============================================================================
# These are applied AFTER all other normalization, matching against uppercase
CHANNEL_ALIASES = {
    'E!': 'E! ENTERTAINMENT',
    'EEN': 'E! ENTERTAINMENT',
    'DREAM WORKS': 'DREAMWORKS',
    'M TV': 'MTV',
    'RTV-7': 'RTV 7',
    # Add more aliases here as needed
}


# =============================================================================
# CREDENTIAL PARSING
# =============================================================================

def extract_provider_name(host: str) -> str:
    """
    Extract a clean provider name from hostname.
    
    Examples:
        gaminghub8k.xyz -> gaminghub8k
        line.tivi-ott.net -> tivi-ott
        iptv-provider.com -> iptv-provider
    """
    # Remove common TLDs
    for tld in ['.xyz', '.net', '.com', '.org', '.tv', '.io', '.me', '.cc', '.co']:
        if host.endswith(tld):
            host = host[:-len(tld)]
            break
    
    # If hostname has subdomains (like line.tivi-ott), use the main part
    parts = host.split('.')
    if len(parts) > 1:
        # Use the last part (main domain) unless it's very short
        main_part = parts[-1]
        if len(main_part) < 3 and len(parts) > 1:
            main_part = parts[-2]
        return main_part
    
    return host


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
    
    # Extract clean provider name for display
    provider_name = extract_provider_name(host)
    
    return {
        'username': real_user,
        'password': password,
        'host': host,
        'port': port,
        'base_url': f"http://{host}:{port}",
        'provider_name': provider_name
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
    name = RE_ZONE_PLUS.sub('', name)  # Remove "ZONE+ " prefix
    name = RE_COUNTRY_PREFIX.sub('', name)  # Catch any remaining "XX: " prefixes
    
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
    
    # === PHASE 8: Apply channel aliases (exact match after normalization) ===
    if name in CHANNEL_ALIASES:
        name = CHANNEL_ALIASES[name]
    
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
# ASYNC HTTP HELPERS (with performance tracking)
# =============================================================================

async def fetch_url(url: str, timeout: int = 60) -> bytes:
    """Fetch a URL using connection-pooled session. Tracks upstream timing."""
    start = time.time()
    error = False
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            return await response.read()
    except Exception:
        error = True
        raise
    finally:
        duration_ms = (time.time() - start) * 1000
        perf.record_upstream(duration_ms, error=error)


async def fetch_url_text(url: str, timeout: int = 60) -> str:
    """Fetch a URL and return text. Tracks upstream timing."""
    start = time.time()
    error = False
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            return await response.text()
    except Exception:
        error = True
        raise
    finally:
        duration_ms = (time.time() - start) * 1000
        perf.record_upstream(duration_ms, error=error)


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

def process_streams_json(data: list, category_map: dict = None, provider_name: str = None) -> list:
    """
    Normalize names in stream list with provider and group name suffixes.
    
    Format: NORMALIZED_NAME (PROVIDER_NAME) (GROUP_NAME)
    Example: NPO 1 (gaminghub8k) (┃NL┃ BASIS TV+)
    
    Groups matching NO_SUFFIX_GROUPS patterns will NOT have suffixes added
    (for groups with Auto Channel Sync enabled in Dispatcharr).
    """
    if not isinstance(data, list):
        return data
    
    if category_map is None:
        category_map = {}
    
    processed = []
    skipped_count = 0
    
    for stream in data:
        if isinstance(stream, dict) and 'name' in stream:
            if not should_skip_channel(stream['name']):
                # Normalize the base name
                normalized_name = normalize_channel_name(stream['name'])
                
                # Get group name from category map
                category_id = str(stream.get('category_id', ''))
                group_name = category_map.get(category_id, '')
                
                # Check if this group should skip suffixes
                if should_skip_suffix(group_name):
                    stream['name'] = normalized_name
                    skipped_count += 1
                else:
                    # Build suffix: (provider) (group)
                    suffix_parts = []
                    if provider_name:
                        suffix_parts.append(f"({provider_name})")
                    if group_name:
                        suffix_parts.append(f"({group_name})")
                    
                    if suffix_parts:
                        stream['name'] = f"{normalized_name} {' '.join(suffix_parts)}"
                    else:
                        stream['name'] = normalized_name
                
                processed.append(stream)
        else:
            processed.append(stream)
    
    if skipped_count > 0:
        logger.info(f"Skipped suffixes for {skipped_count} streams (NO_SUFFIX_GROUPS)")
    
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


async def generate_m3u_from_api(provider: dict) -> str:
    """
    Generate M3U playlist from Xtream Codes API.
    Used when provider disables M3U downloads but keeps API active.
    
    Format: NORMALIZED_NAME (PROVIDER_NAME) (GROUP_NAME)
    Example: NPO 1 (gaminghub8k) (┃NL┃ BASIS TV+)
    """
    provider_name = provider.get('provider_name', '')
    logger.info(f"Generating M3U from API for {provider['host']} (provider: {provider_name})")
    
    # Fetch categories first for group names
    categories_url = (f"{provider['base_url']}/player_api.php?"
                      f"username={provider['username']}&password={provider['password']}"
                      f"&action=get_live_categories")
    
    streams_url = (f"{provider['base_url']}/player_api.php?"
                   f"username={provider['username']}&password={provider['password']}"
                   f"&action=get_live_streams")
    
    try:
        # Fetch both in parallel
        categories_task = fetch_url_text(categories_url)
        streams_task = fetch_url_text(streams_url)
        
        categories_raw, streams_raw = await asyncio.gather(categories_task, streams_task)
        
        categories_data = json.loads(categories_raw)
        streams_data = json.loads(streams_raw)
        
        # Build category ID -> name map
        category_map = {}
        if isinstance(categories_data, list):
            for cat in categories_data:
                if isinstance(cat, dict) and 'category_id' in cat:
                    category_map[str(cat['category_id'])] = cat.get('category_name', 'Unknown')
        
        logger.info(f"Loaded {len(category_map)} categories, {len(streams_data) if isinstance(streams_data, list) else 0} streams")
        
        # Build M3U with provider and group name suffixes
        lines = ['#EXTM3U']
        
        if isinstance(streams_data, list):
            for stream in streams_data:
                if not isinstance(stream, dict):
                    continue
                
                name = stream.get('name', '')
                if not name or should_skip_channel(name):
                    continue
                
                # Normalize the base name
                normalized_name = normalize_channel_name(name)
                
                # Get group name
                category_id = str(stream.get('category_id', ''))
                group_title = category_map.get(category_id, '')
                
                # Check if this group should skip suffixes
                if should_skip_suffix(group_title):
                    final_name = normalized_name
                else:
                    # Final name format: NORMALIZED_NAME (PROVIDER) (GROUP_NAME)
                    suffix_parts = []
                    if provider_name:
                        suffix_parts.append(f"({provider_name})")
                    if group_title:
                        suffix_parts.append(f"({group_title})")
                    
                    if suffix_parts:
                        final_name = f"{normalized_name} {' '.join(suffix_parts)}"
                    else:
                        final_name = normalized_name
                
                stream_id = stream.get('stream_id', '')
                epg_channel_id = stream.get('epg_channel_id', '')
                stream_icon = stream.get('stream_icon', '')
                
                # Build EXTINF line
                extinf_parts = ['-1']
                
                if epg_channel_id:
                    extinf_parts.append(f'tvg-id="{epg_channel_id}"')
                
                extinf_parts.append(f'tvg-name="{final_name}"')
                
                if stream_icon:
                    extinf_parts.append(f'tvg-logo="{stream_icon}"')
                
                if group_title:
                    extinf_parts.append(f'group-title="{group_title}"')
                
                extinf_line = f"#EXTINF:{' '.join(extinf_parts)},{final_name}"
                
                # Build DIRECT stream URL to provider (not through proxy)
                if provider['port'] == 80:
                    direct_base = f"http://{provider['host']}"
                else:
                    direct_base = f"http://{provider['host']}:{provider['port']}"
                
                stream_url = f"{direct_base}/live/{provider['username']}/{provider['password']}/{stream_id}.ts"
                
                lines.append(extinf_line)
                lines.append(stream_url)
        
        m3u_content = '\n'.join(lines)
        logger.info(f"Generated M3U with {(len(lines) - 1) // 2} channels")
        
        return m3u_content
        
    except Exception as e:
        logger.error(f"Failed to generate M3U from API: {e}")
        raise


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
    logger.info("  ✓ Performance monitoring (/stats, /metrics)")
    logger.info("")
    logger.info(f"Server running on port {PORT}")
    logger.info(f"Cache duration: {CACHE_HOURS} hours")
    logger.info("")
    logger.info("Monitoring endpoints:")
    logger.info(f"  http://localhost:{PORT}/health  - Health check")
    logger.info(f"  http://localhost:{PORT}/stats   - Performance stats (JSON)")
    logger.info(f"  http://localhost:{PORT}/metrics - Prometheus metrics")
    logger.info(f"  http://localhost:{PORT}/cache   - Cache details")
    logger.info(f"  http://localhost:{PORT}/clear   - Clear all caches")
    logger.info("=" * 60)
    
    yield
    
    # Shutdown: Close HTTP session
    await http_session.close()


app = FastAPI(title="M3U Normalizing Proxy", lifespan=lifespan)

# Add CORS middleware (allow Dispatcharr web interface to access streams)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, HEAD, OPTIONS, etc.)
    allow_headers=["*"],  # Allow all headers
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "Content-Type"],
)

# Add Gzip compression (min 500 bytes)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def performance_middleware(request: Request, call_next):
    """Track request timing and stats for all endpoints."""
    # Skip stats/health to avoid recursion
    if request.url.path in ['/stats', '/health', '/metrics']:
        return await call_next(request)
    
    perf.request_started()
    start_time = time.time()
    error = False
    response = None
    
    try:
        response = await call_next(request)
        if response.status_code >= 400:
            error = True
        return response
    except Exception as e:
        error = True
        raise
    finally:
        perf.request_finished()
        duration_ms = (time.time() - start_time) * 1000
        
        # Determine endpoint name
        path = request.url.path
        if path.startswith('/live/'):
            endpoint = '/live/*'
        elif path.startswith('/movie/'):
            endpoint = '/movie/*'
        elif path.startswith('/series/'):
            endpoint = '/series/*'
        else:
            endpoint = path
        
        # Get response size if available
        content_length = 0
        if response and hasattr(response, 'headers'):
            content_length = int(response.headers.get('content-length', 0))
        
        # Check if it was a cache hit (indicated by fast response < 5ms without upstream)
        cache_hit = duration_ms < 5 and endpoint in ['/get.php', '/playlist.m3u', '/', '/player_api.php']
        
        perf.record_request(endpoint, duration_ms, content_length, error, cache_hit)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return PlainTextResponse("OK")


@app.get("/stats")
async def get_stats():
    """Get comprehensive performance statistics."""
    return JSONResponse(perf.get_stats())


@app.get("/metrics")
async def get_metrics():
    """Prometheus-compatible metrics endpoint."""
    stats = perf.get_stats()
    lines = [
        "# HELP m3u_proxy_uptime_seconds Proxy uptime in seconds",
        "# TYPE m3u_proxy_uptime_seconds gauge",
        f"m3u_proxy_uptime_seconds {stats['uptime_seconds']}",
        "",
        "# HELP m3u_proxy_requests_total Total requests by endpoint",
        "# TYPE m3u_proxy_requests_total counter",
    ]
    
    for endpoint, data in stats['endpoints'].items():
        safe_endpoint = endpoint.replace('/', '_').replace('*', 'all').strip('_')
        lines.append(f'm3u_proxy_requests_total{{endpoint="{safe_endpoint}"}} {data["requests"]}')
    
    lines.extend([
        "",
        "# HELP m3u_proxy_errors_total Total errors by endpoint",
        "# TYPE m3u_proxy_errors_total counter",
    ])
    
    for endpoint, data in stats['endpoints'].items():
        safe_endpoint = endpoint.replace('/', '_').replace('*', 'all').strip('_')
        lines.append(f'm3u_proxy_errors_total{{endpoint="{safe_endpoint}"}} {data["errors"]}')
    
    lines.extend([
        "",
        "# HELP m3u_proxy_active_requests Current active requests",
        "# TYPE m3u_proxy_active_requests gauge",
        f"m3u_proxy_active_requests {stats['active_requests']}",
        "",
        "# HELP m3u_proxy_upstream_requests_total Total upstream requests",
        "# TYPE m3u_proxy_upstream_requests_total counter",
        f"m3u_proxy_upstream_requests_total {stats['upstream']['requests']}",
        "",
        "# HELP m3u_proxy_cache_hit_ratio Normalization cache hit ratio",
        "# TYPE m3u_proxy_cache_hit_ratio gauge",
    ])
    
    norm_cache = stats['caches']['normalization_cache']
    total = norm_cache['hits'] + norm_cache['misses']
    hit_ratio = norm_cache['hits'] / total if total > 0 else 0
    lines.append(f"m3u_proxy_cache_hit_ratio {hit_ratio:.4f}")
    
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


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


@app.get("/test")
async def test_normalization(name: str = None):
    """Test channel name normalization. Usage: /test?name=NL: NPO1 ᴿᴬᵂ ◉"""
    if not name:
        # Default test cases
        test_cases = [
            "NL: NPO1 ᴿᴬᵂ ◉",
            "NL: NPO 2 ᴴᴰ",
            "┃NL┃ RTL4 HD",
            "NL| SBS6 FHD 50FPS",
            "NPO1",
            "ESPN1 HD",
            "24KITCHEN",
            "Film1 Premiere",
        ]
        results = {tc: normalize_channel_name(tc) for tc in test_cases}
        return JSONResponse({
            "test_cases": results,
            "usage": "Add ?name=YOUR_CHANNEL_NAME to test a specific name"
        })
    
    original = name
    normalized = normalize_channel_name(name)
    return JSONResponse({
        "original": original,
        "normalized": normalized,
        "changed": original != normalized
    })


@app.head("/live/{username}/{password}/{stream_path:path}")
@app.head("/movie/{username}/{password}/{stream_path:path}")
@app.head("/series/{username}/{password}/{stream_path:path}")
async def redirect_stream_head(username: str, password: str, stream_path: str, request: Request):
    """
    Redirect HEAD requests to the actual provider.
    Same as GET - we don't proxy video traffic.
    """
    stream_type = request.url.path.split('/')[1]
    
    provider = parse_credentials(username, password)
    if not provider:
        return PlainTextResponse("Invalid credentials", status_code=400)
    
    real_url = f"{provider['base_url']}/{stream_type}/{provider['username']}/{provider['password']}/{stream_path}"
    
    logger.info(f"Stream HEAD REDIRECT: {stream_type}/{stream_path} -> {real_url}")
    
    return RedirectResponse(url=real_url, status_code=302)


@app.get("/live/{username}/{password}/{stream_path:path}")
@app.get("/movie/{username}/{password}/{stream_path:path}")
@app.get("/series/{username}/{password}/{stream_path:path}")
async def redirect_stream(username: str, password: str, stream_path: str, request: Request):
    """
    Redirect stream requests to the actual provider.
    
    This ensures video traffic goes DIRECTLY to the provider,
    not through this proxy. The proxy only handles:
    - M3U playlist normalization
    - API response normalization
    
    Video streams are redirected, not proxied.
    """
    stream_type = request.url.path.split('/')[1]
    
    provider = parse_credentials(username, password)
    if not provider:
        return PlainTextResponse("Invalid credentials", status_code=400)
    
    # Build the direct URL to the provider
    real_url = f"{provider['base_url']}/{stream_type}/{provider['username']}/{provider['password']}/{stream_path}"
    
    logger.info(f"Stream REDIRECT: {stream_type}/{stream_path} -> {real_url}")
    
    # Return a redirect to the actual provider
    # 302 Found is used so clients don't cache the redirect
    return RedirectResponse(url=real_url, status_code=302)


@app.get("/get.php")
@app.get("/playlist.m3u")
@app.get("/")
async def get_playlist(username: str = None, password: str = None):
    """Get and normalize M3U playlist. Falls back to API generation if provider disables M3U."""
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
        # First, try the standard M3U endpoint
        url = (f"{provider['base_url']}/get.php?"
               f"username={provider['username']}&password={provider['password']}"
               f"&type=m3u_plus&output=ts")
        raw = await fetch_url_text(url)
        
        # Check if provider returned empty M3U (some providers disable this)
        if not raw or not raw.strip() or raw.strip() == '#EXTM3U':
            logger.warning(f"Provider {provider['host']} returned empty M3U, generating from API...")
            raw = await generate_m3u_from_api(provider)
            # No need to process - already normalized during generation
            set_cache(cache_key, raw)
            return Response(content=raw, media_type="application/x-mpegurl")
        
        processed = process_m3u(raw)
        set_cache(cache_key, processed)
        return Response(content=processed, media_type="application/x-mpegurl")
    except Exception as e:
        logger.error(f"M3U fetch error: {e}")
        # Try API fallback on any error
        try:
            logger.info("Attempting API fallback for M3U generation...")
            generated = await generate_m3u_from_api(provider)
            set_cache(cache_key, generated)
            return Response(content=generated, media_type="application/x-mpegurl")
        except Exception as fallback_error:
            logger.error(f"API fallback also failed: {fallback_error}")
            return PlainTextResponse(f"Error: {e}. Fallback failed: {fallback_error}", status_code=500)


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
        # For live streams, we need categories first to build group name suffixes
        category_map = {}
        if action == 'get_live_streams':
            # Fetch categories in parallel with streams
            categories_url = (f"{provider['base_url']}/player_api.php?"
                              f"username={provider['username']}&password={provider['password']}"
                              f"&action=get_live_categories")
            
            categories_task = fetch_url_text(categories_url)
            streams_task = fetch_url_text(real_url)
            
            categories_raw, raw = await asyncio.gather(categories_task, streams_task)
            
            # Build category map
            try:
                categories_data = json.loads(categories_raw)
                if isinstance(categories_data, list):
                    for cat in categories_data:
                        if isinstance(cat, dict) and 'category_id' in cat:
                            category_map[str(cat['category_id'])] = cat.get('category_name', '')
                logger.info(f"Loaded {len(category_map)} categories for stream naming")
            except Exception as e:
                logger.warning(f"Failed to load categories: {e}")
        else:
            raw = await fetch_url_text(real_url)
        
        if action in normalize_actions:
            data = json.loads(raw)
            data = process_streams_json(data, category_map, provider.get('provider_name', ''))
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
