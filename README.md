# Xtream Codes Normalizing Proxy

A transparent proxy that normalizes IPTV channel names for Dispatcharr.

## ✨ Zero Configuration

**No provider credentials in the proxy!** Just run it and configure everything in Dispatcharr.

## 🚀 Quick Start

### 1. Start the proxy

```bash
docker-compose up -d
```

That's it! No `.env` file needed.

### 2. Add provider in Dispatcharr

Go to **M3U & EPG Manager** → **Add Account** → **Xtream Codes**:

| Field | Value |
|-------|-------|
| **Server** | `your-proxy-ip` |
| **Port** | `8765` |
| **Username** | `youruser@provider.com:8080` |
| **Password** | `yourpassword` |

**The magic:** Put your real username, then `@`, then the provider host:port.

### Examples

| Provider URL | Proxy Username |
|--------------|----------------|
| `http://iptv.example.com:8080` | `john@iptv.example.com:8080` |
| `http://streams.provider.net:80` | `myuser@streams.provider.net:80` |
| `http://service.tv:25461` | `user123@service.tv:25461` |

---

## 🔄 Multiple Providers

Just add more accounts in Dispatcharr! Each can point to a different provider:

```
Account 1: user1@provider-a.com:8080
Account 2: user2@provider-b.net:25461
Account 3: user3@provider-c.tv:80
```

All go through the same proxy, all get normalized names.

---

## How It Works

```
Dispatcharr                    Proxy                      Provider
    │                            │                            │
    │  username: john@host:8080  │                            │
    │  password: secret          │                            │
    ├───────────────────────────►│                            │
    │                            │  username: john            │
    │                            │  password: secret          │
    │                            ├───────────────────────────►│
    │                            │                            │
    │                            │◄─── ┃NL┃ NPO 1 HD ─────────│
    │                            │◄─── NL: NPO1 ᴿᴬᵂ ──────────│
    │                            │                            │
    │◄────── NPO 1 ──────────────│  (normalized!)             │
    │◄────── NPO 1 ──────────────│                            │
```

---

## Normalization Examples

| Before (from provider) | After (from proxy) |
|------------------------|-------------------|
| `┃NL┃ NPO 1 HD` | `NPO 1` |
| `NL: NPO1 ᴿᴬᵂ ◉` | `NPO 1` |
| `PLAY+: NPO 1 ᴴᴰ` | `NPO 1` |
| `┃NL┃ SBS6 HD` | `SBS 6` |
| `NL\| RTL 4 FHD 50FPS` | `RTL 4` |
| `OD: Film1 Première ᵁᴴᴰ` | `Film1 Première` |

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `/health` | Health check |
| `/cache` | View cache stats |
| `/clear` | Clear all caches |

All Xtream Codes endpoints are supported:
- `/player_api.php` - Categories, streams, VOD, series
- `/get.php` - M3U playlist
- `/xmltv.php` - EPG
- `/live/...` - Live streams
- `/movie/...` - VOD streams
- `/series/...` - Series streams

---

## Environment Variables (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8765` | Port to listen on |
| `CACHE_HOURS` | `6` | How long to cache API responses |

---

## Run Without Docker

```bash
python3 m3u_proxy.py
```

Or with custom port:

```bash
PORT=9000 python3 m3u_proxy.py
```

---

## Troubleshooting

### "Invalid username format"

Make sure your username includes `@provider:port`:
- ❌ `john` 
- ✅ `john@provider.com:8080`

### Channels still duplicated

1. Delete old direct provider account from Dispatcharr
2. Delete existing channels
3. Add new account through the proxy
4. Enable Auto Channel Sync

### Connection timeout

- Check the provider host:port is correct
- Verify the proxy can reach your provider
- Check firewall rules

---

## Notes

- Uses only Python standard library (no dependencies)
- Streams are proxied in real-time (not cached)
- API responses cached for 6 hours (configurable)
- Supports unlimited providers through one proxy instance
