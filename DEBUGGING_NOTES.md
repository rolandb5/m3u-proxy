# M3U Proxy & Stream-Mapparr Debugging Notes

## Overview

This document captures debugging insights, architecture understanding, and solutions discovered while troubleshooting the M3U proxy and Stream-Mapparr integration with Dispatcharr.

---

## Architecture

### Components

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  IPTV Provider  │────▶│   M3U Proxy     │────▶│   Dispatcharr   │
│  (Tivione, 8K)  │     │ (192.168.1.141) │     │ (192.168.1.217) │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                                ┌─────────────────┐
                                                │ Stream-Mapparr  │
                                                │    (Plugin)     │
                                                └─────────────────┘
```

### Key Concepts

| Term | Description |
|------|-------------|
| **Streams** | Raw IPTV feeds from providers, stored in `dispatcharr_channels_stream` |
| **Channels** | User-defined entries in Dispatcharr that streams get mapped to |
| **Channel Groups** | Categories for organizing channels (e.g., "Algemeen") |
| **Stream Groups** | Categories from IPTV provider (e.g., "NL\| NETHERLANDS SD") |
| **Profiles** | Collections of channels that Stream-Mapparr processes |

### Stream-Mapparr Matching Flow

1. Load channels from selected **Profile** (e.g., "Roland")
2. Load ALL streams via paginated API (`/api/channels/streams/?page=X&page_size=100`)
3. Filter streams by **Stream Groups** setting (uses `channel_group` field)
4. Filter streams by **M3U Sources** setting (uses `m3u_account` field)
5. For each channel, fuzzy match against filtered stream names
6. Apply matches based on threshold (default: 85, user set: 92)

---

## Dispatcharr Database

### Connection

```bash
su - postgres -c "psql -d dispatcharr_db -c \"YOUR_QUERY\""
```

### Key Tables

| Table | Purpose |
|-------|---------|
| `dispatcharr_channels_stream` | All streams from M3U sources |
| `dispatcharr_channels_channel` | User-defined channels |
| `dispatcharr_channels_channelgroup` | Channel/stream group definitions |

### Useful Queries

#### Find streams by name
```sql
SELECT id, name, channel_group_id, m3u_account_id 
FROM dispatcharr_channels_stream 
WHERE name ILIKE '%NPO%' 
ORDER BY name;
```

#### Find channel group ID by name
```sql
SELECT id, name 
FROM dispatcharr_channels_channelgroup 
WHERE name ILIKE '%NETHERLANDS%SD%';
```

#### Check for hidden characters in stream names
```sql
SELECT id, name, encode(name::bytea, 'hex') as hex_name 
FROM dispatcharr_channels_stream 
WHERE id IN (8155, 9785, 8157);
```

#### Count streams and check pagination position
```sql
SELECT COUNT(*) as total FROM dispatcharr_channels_stream;
SELECT COUNT(*) as streams_before FROM dispatcharr_channels_stream WHERE id < 9785;
```

#### Full stream details
```sql
SELECT * FROM dispatcharr_channels_stream WHERE id IN (8155, 8157, 9785);
```

---

## M3U Proxy Normalization

### Location
- Server: `192.168.1.141:8765`
- Code: `/Users/rolandbo@backbase.com/Documents/Coding Projects/m3u-proxy/m3u_proxy.py`

### Normalization Rules Applied

1. Remove quality tags: `4K`, `8K`, `UHD`, `FHD`, `HD`, `SD`, `8K+`, `8K+ UHD`
2. Remove provider prefixes: `NL|`, `NL :`, country codes
3. Remove special characters and extra whitespace
4. Add space to NPO channels: `NPO1` → `NPO 1`

### Channels That SKIP Normalization

The proxy skips normalization for:
- **PPV channels**: Contains "PPV"
- **Event channels**: Contains date patterns like `12/25` or `25.12`
- **Formula 1**: Contains `F1`, `FORMULE`, `FORMULA`
- **MotoGP**: Contains `MOTOGP`
- **Viaplay F1/F2/F3**: Matches `VIAPLAY F[123]`

### Testing Normalization

```bash
curl -s "http://192.168.1.141:8765/player_api.php?username=USER@provider:port&password=PASS&action=get_live_streams" | \
  python3 -c "import sys,json; streams=json.load(sys.stdin); [print(s['name']) for s in streams if 'NPO' in s['name']]"
```

---

## Stream-Mapparr Configuration

### Settings Path
Dispatcharr UI → Plugins → Stream-Mapparr

### Key Settings

| Setting | Description | Current Value |
|---------|-------------|---------------|
| Profile Name | Which profile's channels to process | `Roland` |
| Channel Groups | Filter channels by group | `Algemeen` |
| Stream Groups | Filter streams by group | `NL\| NETHERLANDS SD` |
| M3U Sources | Filter by M3U account | (all) |
| Fuzzy Match Threshold | Minimum similarity score (0-100) | `92` |
| Visible Channel Limit | Channels per group to enable | `1` |

### Channel Database Files

Located in `/data/plugins/stream-mapparr/`:
- `NL_channels.json` - Netherlands (MUST exist for Netherlands matching)
- `US_channels.json` - United States
- `UK_channels.json` - United Kingdom
- etc.

**Note**: If `NL_channels.json` is missing, copy from another source or create it.

---

## Known Issues & Solutions

### Issue 1: NPO 2 Not Matching

**Symptoms:**
- NPO 1 matches ✅
- NPO 3 matches ✅
- NPO 2 shows 0 matches ❌

**Root Cause Discovery:**
- NPO 2 stream had database ID 9785 (very high)
- NPO 1/3 had IDs 8155/8157 (consecutive)
- NPO 2 was position 2849 out of 2850 streams (last page!)
- Stream was added/updated AFTER preview was run

**Investigation showed:**
- All three streams exist in same group (channel_group_id = 173)
- All have same m3u_account_id (2)
- All have correct names with no hidden characters
- Hex verification: `NPO 2` = `4e504f2032` ✅

**Potential Issues:**
1. Pagination bug on last page of stream loading
2. Timing - stream added after preview ran
3. Stream-Mapparr stops fetching before reaching high-ID streams

### Issue 2: Missing NL_channels.json

**Symptoms:**
- "Enable Netherlands" shows in UI but doesn't work
- Matching doesn't use Dutch channel database

**Solution:**
```bash
cp /data/plugins/channel-maparr/NL_channels.json /data/plugins/stream-mapparr/NL_channels.json
```

Or use the IN_channels.json workaround (rename/copy Netherlands data into India file and enable India).

### Issue 3: Credential Parsing Errors

**Symptoms:**
- `400 Bad Request` errors
- URL encoding issues with `@` and `:`

**Solution:**
M3U proxy strips `http://` and `https://` from username. Format should be:
```
Username: USER@provider.com:port
Password: PASSWORD
```

---

## Stream-Mapparr Code Analysis

### Plugin Location
- Dispatcharr: `/data/plugins/stream-mapparr/plugin.py`
- GitHub: `https://github.com/rolandb5/Stream-Mapparr`

### Pagination Logic (potential issue)

```python
# From plugin.py lines ~2397-2447
page = 1
while True:
    endpoint = f"/api/channels/streams/?page={page}&page_size=100"
    streams_response = self._get_api_data(endpoint, ...)
    
    if isinstance(streams_response, dict) and 'results' in streams_response:
        results = streams_response['results']
        if not results:
            break
        all_streams_data.extend(results)
        if len(results) < 100:  # Last page
            break
        page += 1
```

### Stream Group Filtering

```python
# Lines ~2449-2461
if selected_stream_groups_str:
    selected_stream_groups = [g.strip() for g in selected_stream_groups_str.split(',')]
    valid_stream_group_ids = [group_name_to_id[name] for name in selected_stream_groups if name in group_name_to_id]
    filtered_streams = [s for s in all_streams_data if s.get('channel_group') in valid_stream_group_ids]
```

### Fuzzy Matching

```python
# Uses fuzzy_matcher.py
# Stages:
# 1. Exact match (after normalization)
# 2. Substring matching
# 3. Token-sort fuzzy matching with Levenshtein distance
```

---

## Debugging Commands

### Check Stream-Mapparr logs
```bash
docker logs -f dispatcharr 2>&1 | grep -i "stream-mapparr\|NPO"
```

### List plugin files
```bash
ls -la /data/plugins/stream-mapparr/
```

### Check processed data (if exists)
```bash
cat /data/plugins/stream-mapparr/processed_data.json | python3 -m json.tool | grep -A5 "NPO"
```

### Database connection test
```bash
su - postgres -c "psql -d dispatcharr_db -c '\dt'"
```

---

## CSV Output Analysis

### Location
Generated after Preview/Run, check Downloads or configured export path.

### Key columns
- `will_update`: Yes/No
- `threshold`: Match threshold used
- `channel_id`: Dispatcharr channel ID
- `channel_name`: Channel being matched
- `matched_streams`: Count of matched streams
- `stream_names`: Names of matched streams

### Interpreting Results

```csv
Yes,92,5915,NPO 2,0,
No,87,5915,  └─ (at threshold 87),1,NPO 3
```

This means:
- Channel "NPO 2" (ID 5915) found 0 streams at threshold 92
- At threshold 87, it would match "NPO 3" (wrong!)
- Indicates the correct stream "NPO 2" is not in the loaded stream list

---

## Environment Details

| Component | Location/Value |
|-----------|----------------|
| M3U Proxy | 192.168.1.141:8765 |
| Dispatcharr | 192.168.1.217 |
| Database | PostgreSQL `dispatcharr_db` |
| Platform | Proxmox (not Docker) |
| Stream-Mapparr | v0.6.0 |
| Timezone | Europe/Paris (CET/CEST) |

---

## Next Steps / TODO

- [ ] Verify NPO 2 matches after re-running Preview
- [ ] Investigate Stream-Mapparr pagination for last-page edge cases
- [ ] Consider lowering fuzzy threshold from 92 to 85 for better matches
- [ ] Monitor for similar "last stream not matching" issues

---

*Last updated: 2025-12-15*

