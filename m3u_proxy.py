#!/usr/bin/env python3
"""
Transparent Xtream Codes Normalizing Proxy for Dispatcharr

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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, parse_qs, urlencode
import logging

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


# =============================================================================
# CREDENTIAL PARSING
# =============================================================================

def parse_credentials(username: str, password: str) -> dict:
    """
    Parse the username to extract provider info.
    
    Format: realuser@host:port  OR  realuser@http://host:port
    Examples:
      - john@provider.com:8080        -> user=john, host=provider.com, port=8080
      - john@http://provider.com:8080 -> user=john, host=provider.com, port=8080
      - john@provider.com             -> user=john, host=provider.com, port=80
      - john                          -> ERROR (no provider specified)
    """
    if '@' not in username:
        return None
    
    # Split on last @ (in case username contains @)
    at_pos = username.rfind('@')
    real_user = username[:at_pos]
    host_part = username[at_pos + 1:]
    
    # Strip protocol if present (http:// or https://)
    if host_part.startswith('http://'):
        host_part = host_part[7:]
    elif host_part.startswith('https://'):
        host_part = host_part[8:]
    
    # Parse host:port
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
# NORMALIZATION RULES
# =============================================================================

def normalize_channel_name(name: str) -> str:
    """
    Apply all normalization rules to a channel name.
    """
    if not name:
        return name
    
    original = name
    
    # === PHASE 1: Remove provider wrappers ===
    name = re.sub(r'┃[^┃]+┃\s*', '', name)
    name = re.sub(r'^NL:\s*', '', name)
    name = re.sub(r'^NL\|\s*', '', name)
    name = re.sub(r'^UK\|\s*', '', name)
    name = re.sub(r'^PLAY\+:\s*', '', name)
    name = re.sub(r'^OD:\s*', '', name)
    
    # === PHASE 2: Remove special Unicode characters ===
    name = re.sub(r'\s*ⱽᴵᴾ\s*', '', name)
    name = re.sub(r'\s*ᴿᴬᵂ\s*', '', name)
    name = re.sub(r'\s*ᴴᴰ\s*', '', name)
    name = re.sub(r'\s*ᵁᴴᴰ\s*', '', name)
    name = re.sub(r'\s*ᴳᴼᴸᴰ\s*', '', name)
    name = re.sub(r'\s*⏺ʳᵉᶜ\s*', '', name)
    name = re.sub(r'\s*◉\s*', '', name)
    name = re.sub(r'\s*ᵁᴴᴰ\s*³⁸⁴⁰ᴾ\s*', '', name)
    name = re.sub(r'☰+\s*|\s*☰+', '', name)
    name = re.sub(r'≡+\s*|\s*≡+', '', name)
    
    # === PHASE 3: Remove quality/format tags ===
    name = re.sub(r'\s*\|\s*8K\s*EXCLUSIVE\s*', '', name)
    name = re.sub(r'\s*-\s*NO EVENT STREAMING\s*-?\s*', ' ', name)
    name = re.sub(r'^:\s*', '', name)
    name = re.sub(r'\s*\|\s*$', '', name)
    name = re.sub(r'\s+(HD|FHD|4K|8K|8K\+|UHD|SD|LQ|HEVC)\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+FHD\s+50FPS\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+8K\+\s*UHD\s*$', '', name, flags=re.IGNORECASE)
    
    # === PHASE 4: Normalize spacing for Dutch channels ===
    name = re.sub(r'^NPO(\d)', r'NPO \1', name, flags=re.IGNORECASE)
    name = re.sub(r'^SBS(\d)', r'SBS \1', name, flags=re.IGNORECASE)
    name = re.sub(r'^Net\s*5$', 'NET 5', name, flags=re.IGNORECASE)
    name = re.sub(r'^ESPN(\d)', r'ESPN \1', name, flags=re.IGNORECASE)
    name = re.sub(r'^RTL(\d)', r'RTL \1', name, flags=re.IGNORECASE)
    name = re.sub(r'^Film\s*1\s+', 'Film1 ', name, flags=re.IGNORECASE)
    
    # === PHASE 5: Final cleanup ===
    name = re.sub(r'\s{2,}', ' ', name)
    name = name.strip()
    
    return name


def should_skip_channel(name: str) -> bool:
    """Check if this is a header/placeholder."""
    if not name:
        return True
    if re.match(r'^#{2,}.*#{2,}$', name):
        return True
    if re.match(r'^☰+.*☰+$', name):
        return True
    if re.match(r'^≡+.*≡+$', name):
        return True
    if re.match(r'^-+$', name):
        return True
    return False


# =============================================================================
# HTTP HELPERS
# =============================================================================

def fetch_url(url: str, timeout: int = 60) -> bytes:
    """Fetch a URL and return raw bytes."""
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    with urlopen(req, timeout=timeout) as response:
        return response.read()


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
    
    while i < len(lines):
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
                prefix = re.sub(
                    r'tvg-name="([^"]*)"',
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
# HTTP REQUEST HANDLER
# =============================================================================

class ProxyHandler(BaseHTTPRequestHandler):
    
    def send_json(self, data: str, status: int = 200):
        content = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)
    
    def send_m3u(self, data: str):
        content = data.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-mpegurl')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)
    
    def send_text(self, data: str, status: int = 200):
        content = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)
    
    def send_error_json(self, message: str):
        self.send_json(json.dumps({
            'user_info': {'status': 'error', 'message': message},
            'error': message
        }), 400)
    
    def proxy_binary(self, url: str, content_type: str = None):
        """Proxy binary content (streams, images, etc.)"""
        try:
            req = Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urlopen(req, timeout=30) as response:
                self.send_response(200)
                ct = content_type or response.getheader('Content-Type', 'application/octet-stream')
                self.send_header('Content-Type', ct)
                cl = response.getheader('Content-Length')
                if cl:
                    self.send_header('Content-Length', cl)
                self.end_headers()
                
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            logger.error(f"Proxy error: {e}")
            self.send_text(str(e), 500)
    
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        
        # === Health check (no auth needed) ===
        if path == '/health':
            self.send_text('OK')
            return
        
        # === Cache stats ===
        if path == '/cache':
            stats = {k: {'age_hours': round((time.time() - v['timestamp']) / 3600, 2)} 
                     for k, v in cache.items()}
            self.send_json(json.dumps({'entries': len(cache), 'items': stats}, indent=2))
            return
        
        # === Clear cache ===
        if path == '/clear':
            cache.clear()
            self.send_text('Cache cleared!')
            return
        
        # === Live/VOD/Series streams - handle BEFORE query param check ===
        # These have credentials in the path: /live/username/password/stream.ts
        if path.startswith('/live/') or path.startswith('/movie/') or path.startswith('/series/'):
            parts = path.split('/')
            if len(parts) >= 5:
                stream_type = parts[1]  # live, movie, or series
                path_username = parts[2]  # user@host:port
                path_password = parts[3]  # password
                stream_path = '/'.join(parts[4:])  # stream ID and extension
                
                provider = parse_credentials(path_username, path_password)
                if not provider:
                    self.send_text('Invalid credentials in stream URL', 400)
                    return
                
                real_url = (f"{provider['base_url']}/{stream_type}/"
                           f"{provider['username']}/{provider['password']}/{stream_path}")
                
                logger.info(f"Stream: {stream_type}/{stream_path} -> {provider['host']}:{provider['port']}")
                self.proxy_binary(real_url)
                return
        
        # === Extract credentials from request ===
        username = query.get('username', [None])[0]
        password = query.get('password', [None])[0]
        
        if not username or not password:
            self.send_error_json('Missing username or password')
            return
        
        provider = parse_credentials(username, password)
        if not provider:
            self.send_error_json(
                'Invalid username format. Use: realuser@provider.com:port\n'
                'Example: john@iptv-server.com:8080'
            )
            return
        
        logger.info(f"Request: {path} -> {provider['host']}:{provider['port']}")
        
        # === M3U Playlist ===
        if path in ['/get.php', '/playlist.m3u', '/']:
            cache_key = get_cache_key(provider, 'm3u')
            cached = get_cached(cache_key)
            
            if cached:
                logger.info("Serving M3U from cache")
                self.send_m3u(cached)
                return
            
            try:
                url = (f"{provider['base_url']}/get.php?"
                       f"username={provider['username']}&password={provider['password']}"
                       f"&type=m3u_plus&output=ts")
                raw = fetch_url(url).decode('utf-8', errors='replace')
                processed = process_m3u(raw)
                set_cache(cache_key, processed)
                self.send_m3u(processed)
            except Exception as e:
                logger.error(f"M3U fetch error: {e}")
                self.send_text(f"Error: {e}", 500)
            return
        
        # === Xtream Codes API ===
        if path == '/player_api.php':
            action = query.get('action', [None])[0]
            
            # Build the real provider URL
            real_params = {
                'username': provider['username'],
                'password': provider['password'],
            }
            if action:
                real_params['action'] = action
            
            # Add any extra params (category_id, stream_id, etc.)
            for key, values in query.items():
                if key not in ['username', 'password', 'action']:
                    real_params[key] = values[0]
            
            real_url = f"{provider['base_url']}/player_api.php?{urlencode(real_params)}"
            cache_key = get_cache_key(provider, 'api', real_params)
            
            # Actions that return stream lists - normalize these
            normalize_actions = ['get_live_streams', 'get_vod_streams', 'get_series']
            
            # Actions that should NOT be cached (real-time data)
            no_cache_actions = ['get_short_epg', 'get_simple_data_table']
            
            # Check cache for cacheable actions
            if action not in no_cache_actions:
                cached = get_cached(cache_key)
                if cached:
                    logger.info(f"Serving {action or 'auth'} from cache")
                    self.send_json(cached)
                    return
            
            try:
                logger.info(f"Fetching from upstream: {real_url}")
                raw = fetch_url(real_url).decode('utf-8')
                
                # Normalize stream names for relevant actions
                if action in normalize_actions:
                    data = json.loads(raw)
                    data = process_streams_json(data)
                    raw = json.dumps(data)
                
                # Modify server info to point to our proxy
                if action is None:  # Auth/server info request
                    data = json.loads(raw)
                    if 'server_info' in data:
                        # Keep original server info but we're the proxy
                        pass
                    raw = json.dumps(data)
                
                # Cache if appropriate
                if action not in no_cache_actions:
                    set_cache(cache_key, raw)
                
                self.send_json(raw)
                
            except HTTPError as e:
                logger.error(f"Upstream HTTP error: {e.code} {e.reason} for {real_url}")
                self.send_error_json(f"Upstream server returned {e.code}: {e.reason}")
            except URLError as e:
                logger.error(f"Upstream connection error: {e.reason} for {real_url}")
                self.send_error_json(f"Cannot connect to upstream: {e.reason}")
            except Exception as e:
                logger.error(f"API error: {e} for {real_url}")
                self.send_error_json(str(e))
            return
        
        
        # === EPG/XMLTV ===
        if path == '/xmltv.php' or 'xmltv' in path.lower():
            url = (f"{provider['base_url']}/xmltv.php?"
                   f"username={provider['username']}&password={provider['password']}")
            try:
                data = fetch_url(url, timeout=120)
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', len(data))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_text(f"Error: {e}", 500)
            return
        
        # === Panel API (if provider supports it) ===
        if path == '/panel_api.php':
            real_params = {
                'username': provider['username'],
                'password': provider['password'],
            }
            for key, values in query.items():
                if key not in ['username', 'password']:
                    real_params[key] = values[0]
            
            real_url = f"{provider['base_url']}/panel_api.php?{urlencode(real_params)}"
            try:
                raw = fetch_url(real_url).decode('utf-8')
                self.send_json(raw)
            except Exception as e:
                self.send_error_json(str(e))
            return
        
        # === 404 ===
        self.send_text('Not Found', 404)
    
    def log_message(self, format, *args):
        if '/live/' not in self.path and '/movie/' not in self.path and '/series/' not in self.path:
            logger.info(f"{self.address_string()} - {format % args}")


# =============================================================================
# MAIN
# =============================================================================

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    
    logger.info("=" * 60)
    logger.info("Xtream Codes Normalizing Proxy")
    logger.info("=" * 60)
    logger.info("")
    logger.info("NO CONFIGURATION NEEDED!")
    logger.info("")
    logger.info("In Dispatcharr, add an Xtream Codes account:")
    logger.info(f"  Server:   <this-server-ip>")
    logger.info(f"  Port:     {PORT}")
    logger.info("  Username: youruser@provider.com:port")
    logger.info("  Password: yourpassword")
    logger.info("")
    logger.info("Example username: john@iptv-server.com:8080")
    logger.info("")
    logger.info(f"Cache duration: {CACHE_HOURS} hours")
    logger.info("")
    logger.info("Endpoints:")
    logger.info(f"  http://localhost:{PORT}/health  - Health check")
    logger.info(f"  http://localhost:{PORT}/cache   - Cache stats")
    logger.info(f"  http://localhost:{PORT}/clear   - Clear cache")
    logger.info("=" * 60)
    
    server.serve_forever()


if __name__ == '__main__':
    run_server()
