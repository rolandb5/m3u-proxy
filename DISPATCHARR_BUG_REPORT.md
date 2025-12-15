# Dispatcharr Bug Report: API Pagination Skips Streams with High IDs

## Summary

The Dispatcharr streams API (`/api/channels/streams/`) fails to return streams that have high database IDs, causing them to be invisible to plugins like Stream-Mapparr.

## Environment

- **Dispatcharr Version**: Latest (as of 2025-12-15)
- **Platform**: Proxmox LXC (Debian 13)
- **Database**: PostgreSQL
- **Total Streams**: 2850

## Steps to Reproduce

1. Have a large number of streams in the database (2800+)
2. Add a new stream that gets assigned a high ID (e.g., 9785 when other streams are around 8000-8200)
3. Use the paginated streams API: `/api/channels/streams/?page=X&page_size=100`
4. Observe that the high-ID stream is NOT returned in any page

## Expected Behavior

All streams in the database should be returned by the paginated API, regardless of their ID value.

## Actual Behavior

Streams with very high IDs (at the end of the ID sequence) are not returned by the API.

### Detailed Evidence

**Database Query (all streams exist):**
```sql
SELECT id, name, channel_group_id FROM dispatcharr_channels_stream 
WHERE channel_group_id = 173 ORDER BY id;

-- Results:
--  8155 | NPO 1  | 173
--  8157 | NPO 3  | 173
--  8158 | RTL 4  | 173
--  ... (25 more streams) ...
--  9785 | NPO 2  | 173   <-- HIGH ID, NOT RETURNED BY API
```

**API Behavior:**
- Total streams reported: 2850
- Streams in group 173 in database: 28
- Streams in group 173 returned by API: **27** (missing 1)
- Missing stream: ID 9785 (NPO 2)

**Stream-Mapparr Debug Logs:**
```
[Stream-Mapparr] DEBUG NPO stream: id=8155, name=NPO 1, channel_group=173  ✅
[Stream-Mapparr] DEBUG NPO stream: id=8157, name=NPO 3, channel_group=173  ✅
# NO entry for id=9785 (NPO 2) with channel_group=173 ❌
[Stream-Mapparr] Filtered streams from 2850 to 27 based on stream groups: NL| NETHERLANDS SD
```

## Impact

- Plugins relying on the streams API (like Stream-Mapparr) cannot see or match these "invisible" streams
- Users may experience missing channel assignments with no obvious cause
- The issue is hard to diagnose because the streams DO exist in the database

## Workaround

Delete the affected stream and refresh the M3U source so it gets re-added with a new (lower) ID:

```bash
su - postgres -c "psql -d dispatcharr_db -c \"DELETE FROM dispatcharr_channels_stream WHERE id = 9785;\""
# Then refresh M3U account in UI
```

## Suspected Cause

The pagination logic in the streams API viewset may have an off-by-one error or is using a different ordering/filtering that excludes the last few records.

Possible locations to investigate:
- `apps/channels/views.py` - StreamViewSet
- `apps/channels/serializers.py` - StreamSerializer
- Pagination class configuration

## Additional Context

- The issue was reproducible across multiple Dispatcharr restarts
- The issue persisted after clearing caches
- Only deleting the stream from the database and re-syncing the M3U source resolved it
- The high ID (9785) was caused by the stream being deleted and re-added, assigning a new auto-increment ID

## Logs

Full debug logs available upon request.

---

**Reporter**: Roland B.  
**Date**: 2025-12-15  
**Debugging Session**: ~4 hours with extensive database and API analysis

