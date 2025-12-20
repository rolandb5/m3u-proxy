# Stream-Mapparr Bug Report: Short Channel Names Fail to Match Despite Correct Configuration

## Summary

Short channel names (e.g., "NPO 2", "RTL 4", "NPO 3") fail to match streams even when:
- Streams exist in selected stream groups
- All built-in tag filters are enabled
- Fuzzy match threshold is set very low (30)
- Longer channel names with similar patterns match successfully

## Environment

- **Stream-Mapparr Version**: v0.7.0
- **Dispatcharr Platform**: Proxmox LXC (Debian 13)
- **Database**: PostgreSQL
- **Channel Database**: Netherlands (v2025-12-14-v2)

## Configuration

```
Profile Name(s): Roland
Selected Channel Groups: Algemeen
Selected Stream Groups: ┃NL┃ CANAL+ LIVE, ┃NL┃ NEDERLAND 4K ULTRA, ┃NL┃ ZIGGO KABEL
Selected M3U Sources: (all M3U sources)
Fuzzy Match Threshold: 30
Overwrite Streams: True
Visible Channel Limit: 1

Tag Filter Settings:
- Ignore Quality Tags: True
- Ignore Regional Tags: True
- Ignore Geographic Tags: True
- Ignore Misc Tags: True
- Custom Ignore Tags: (none)
```

## Steps to Reproduce

1. Configure Stream-Mapparr with the settings above
2. Enable Netherlands channel database
3. Select stream groups that contain Dutch channels with `┃NL┃` or `┃CANAL+┃` prefixes
4. Run "Match & Assign Streams" preview
5. Observe that short channel names (NPO 1, NPO 2, NPO 3, RTL 4, RTL 5, etc.) show 0 matches

## Expected Behavior

Channel `NPO 2` should match streams like:
- `┃NL┃ NPO 2 4K` → after tag removal → `NPO 2` (exact match)
- `┃CANAL+┃ NPO 2 HD` → after tag removal → `NPO 2` (exact match)
- `┃NL┃ NPO 2 8K+ UHD` → after tag removal → `NPO 2` (exact match)

## Actual Behavior

- Channel `NPO 2` → **0 matches**
- Channel `NPO 3` → **0 matches**
- Channel `RTL 4` → **0 matches**
- Channel `RTL 5` → **0 matches**
- Channel `RTL 7` → **0 matches**
- Channel `RTL 8` → **0 matches**
- Channel `SBS6 HD` → **0 matches**
- Channel `NET 5` → **0 matches**

## Evidence: Streams Exist in Selected Groups

### Database Query for NPO 2:
```sql
SELECT s.name, cg.name FROM dispatcharr_channels_stream s 
JOIN dispatcharr_channels_channelgroup cg ON s.channel_group_id = cg.id 
WHERE s.name LIKE '%NPO 2%' AND s.name NOT LIKE '%EXTRA%';
```

### Results:
```
         name          |               name                
-----------------------+-----------------------------------
 ┃NL┃ NPO 2 HD  ⏺ʳᵉᶜ   | ┃NL┃ NEDERLAND HD | TERUGKIJKEN ⏺
 ┃NL┃ NPO 2 4K         | ┃NL┃ NEDERLAND 4K ULTRA          ← SELECTED
 ┃NL┃ NPO 2 FHD 50FPS  | ┃NL┃ ODIDO FHD 50FPS
 ┃NL┃ NPO 2 HD         | ┃NL┃ ODIDO HD
 ┃NLZIET┃ NPO 2 FHD    | ┃NL┃ NLZIET LIVE
 ┃NLZIET┃ NPO 2 HD     | ┃NL┃ NLZIET LIVE
 PLAY+: NPO 2 ᴴᴰ       | NL| CANAL+ ONLINE VERMAAK ᴴᴰ
 PLAY+: NPO 2 ᴿᴬᵂ      | NL| CANAL+ ONLINE VERMAAK ᴿᴬᵂ
 OD: NPO 2 ᴴᴰ          | NL| ODIDO VERMAAK ᴴᴰ ᴳᴼᴸᴰ
 OD: NPO 2 ᴿᴬᵂ         | NL| ODIDO VERMAAK ᴿᴬᵂ ᴳᴼᴸᴰ
 NL: NPO 2 Extra ᴿᴬᵂ ◉ | NL| ALGEMEEN HD/4K
 NL: NPO 2 LQ          | NL| NETHERLANDS SD
 ┃CANAL+┃ NPO 2 HD     | ┃NL┃ CANAL+ LIVE                 ← SELECTED
 ┃NL┃ NPO 2 8K         | ┃NL┃ BASIS TV+
 ┃NL┃ NPO 2  8K+ UHD   | ┃NL┃ ZIGGO KABEL                 ← SELECTED
```

**Streams in selected groups: 3** ✅

### Similar Query for NPO 1:
```
         name          |               name                
-----------------------+-----------------------------------
 ┃NL┃ NPO 1 4K         | ┃NL┃ NEDERLAND 4K ULTRA          ← SELECTED
 ┃CANAL+┃ NPO 1 HD     | ┃NL┃ CANAL+ LIVE                 ← SELECTED
 ┃NL┃ NPO 1  8K+ UHD   | ┃NL┃ ZIGGO KABEL                 ← SELECTED
 ... (13 rows total)
```

**Streams in selected groups: 3** ✅

## Contrast: Longer Names DO Match

From the same CSV export, these **longer** channel names match successfully:

| Channel Name | Matches | Matched Streams |
|--------------|---------|-----------------|
| NPO 1 EXTRA | 3 | ┃NL┃ NPO 1 EXTRA 8K+ UHD; ┃NL┃ NPO 1 EXTRA 4K; ┃CANAL+┃ NPO 1 EXTRA |
| NPO 2 EXTRA | 3 | ┃NL┃ NPO 2 EXTRA 8K+ UHD; ┃NL┃ NPO 2 EXTRA 4K; ┃CANAL+┃ NPO 2 EXTRA |
| NPO POLITIEK EN NIEUWS | 3 | ┃NL┃ NPO POLITIEK EN NIEUWS 8K+ UHD; ... |
| COMEDY CENTRAL | 3 | ┃NL┃ COMEDY CENTRAL 8K+ UHD; ... |
| FILM1 FAMILY | 3 | ┃NL┃ FILM1 FAMILY 8K+ UHD; ... |
| RTL TELEKIDS | 2 | ┃NL┃ RTL TELEKIDS 8K+ UHD; ┃NL┃ RTL TELEKIDS 4K |
| RTL LOUNGE | 2 | ┃NL┃ RTL LOUNGE 8K+ UHD; ┃NL┃ RTL LOUNGE 4K |
| RTL CRIME | 1 | ┃NL┃ RTL CRIME 4K |

**Pattern**: Channels with 10+ characters match, channels with 5-6 characters don't.

## Potential Root Causes

### 1. Built-in Tag Filters Don't Handle Box Drawing Characters

The "Ignore Regional Tags" filter may not recognize:
- `┃NL┃` (U+2503 BOX DRAWINGS HEAVY VERTICAL)
- `┃CANAL+┃`
- `┃NLZIET┃`

It might only handle standard formats like:
- `NL|`
- `NL:`
- `UK|`

### 2. Fuzzy Matching Algorithm Issue with Short Strings

Short strings like "NPO 2" (5 chars) might:
- Have lower confidence scores
- Be filtered out by some minimum length threshold
- Produce too many false-positive partial matches

### 3. Unicode Character Handling

Special characters in stream names might not be stripped correctly:
- `┃` (U+2503)
- `ᴴᴰ` (superscript HD)
- `ᴿᴬᵂ` (superscript RAW)
- `⏺ʳᵉᶜ` (record symbol + superscript)

## Workaround Attempted

### Custom Ignore Tags (MADE IT WORSE)

Adding custom ignore tags:
```
┃CANAL+┃,┃NL┃,NL:,OD:,PLAY+:,ᴿᴬᵂ,ᴴᴰ,ᵁᴴᴰ,ⱽᴵᴾ,ᴳᴼᴸᴰ,⏺ʳᵉᶜ,◉,FHD 50FPS,8K+ UHD
```

**Result**: ALL matching broke (0 matches for everything). Removing custom tags restored partial matching.

This suggests the custom ignore tags feature may have a parsing bug with special characters.

## Impact

- Major Dutch channels (NPO 1, NPO 2, NPO 3, RTL 4, RTL 5, RTL 7, RTL 8, SBS 6, NET 5) cannot be auto-mapped
- Users must manually assign streams for the most popular channels
- Defeats the purpose of automatic stream mapping for primary content

## Suggested Fix

1. **Add support for box drawing characters** in the "Ignore Regional Tags" filter:
   - `┃NL┃` → strip
   - `┃CANAL+┃` → strip
   - `┃NLZIET┃` → strip
   - `┃AR┃`, `┃UK┃`, etc. → strip

2. **Review fuzzy matching for short strings** - ensure channels with 3-6 characters can still match

3. **Fix custom ignore tags parsing** - special characters and Unicode should be handled correctly

## CSV Export Files

Attached exports showing the issue:
- `stream_mapparr_20251219_222313.csv` - With empty custom tags, NPO 2/RTL 4 still 0 matches
- `stream_mapparr_20251219_221348.csv` - With custom tags, same result
- `stream_mapparr_20251219_215850.csv` - Intermittent 0 matches for everything

## Additional Notes

- Threshold of 30 should match anything with >30% similarity
- "NPO 2" vs "NPO 2" after tag stripping = 100% match
- Even if tags aren't stripped, "NPO 2" vs "┃NL┃ NPO 2 4K" should exceed 30% threshold
- The fact that "NPO 1 EXTRA" matches but "NPO 1" doesn't suggests a length-based issue

---

**Reported by**: User via debugging session
**Date**: 2025-12-19
**Priority**: High (affects core functionality for major channels)

