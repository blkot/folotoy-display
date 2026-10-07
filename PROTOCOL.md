# Folotoy Display feed protocol

Base URL: `https://blkot.github.io/folotoy-display/`

Fetch `device.json` over HTTPS. JSON image paths are relative to the base URL. A `path` such as `assets/a1b2...rgb565` is fetched as `https://blkot.github.io/folotoy-display/assets/a1b2...rgb565`. These are public files; requests do not need credentials.

## Root object

`device.json` is UTF-8 JSON and is capped at 16,384 encoded bytes.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Fixed at `1`. |
| `generated_at` | string | UTC timestamp in ISO 8601 form, ending in `Z`. |
| `refresh_seconds` | integer | Fixed refresh hint of `300` seconds. |
| `profile` | object | Public identity, counts, and optional avatar. |
| `calendar` | object | Contribution grid and parallel counts. |
| `activity` | object | Up to four public or manually configured activity entries. |
| `repositories` | object | Up to three public or manually configured repositories. |
| `achievements` | object | Up to six GitHub achievement badges. |

Optional sections have `available` and `stale` booleans. When `available` is false, their `items` list is empty, or the calendar uses its documented empty form. When `stale` is true, the build could not refresh that section and reused same-user data from the previously published feed. A successful response containing no public items is available with an empty list; it is not a fetch failure.

## Profile

`profile` contains:

```json
{
  "username": "blkot",
  "name": "Display name",
  "headline": "Short public bio",
  "public_repos": 12,
  "followers": 34,
  "following": 56,
  "avatar": {
    "path": "assets/<content-hash>.rgb565",
    "width": 32,
    "height": 32,
    "format": "rgb565le"
  }
}
```

`avatar` may be `null`. The build uses the `display_name` and `headline` overrides in `content.yaml` when set; otherwise it uses the corresponding public GitHub profile values. Text is printable ASCII and bounded to the device field sizes: username 39 bytes, name 63, and headline 36.

## Calendar

`calendar` contains `available`, `stale`, `week_count`, `total`, `first_date`, `last_date`, `levels`, and `counts`.

When available, `week_count` is between 1 and 54. The date range is a Sunday-first grid with seven cells per week, oldest week first. `first_date` and `last_date` are the dates at the first and last grid cells, not necessarily dates with contributions. The `levels` string and `counts` array each have exactly `week_count * 7` entries. Index `week * 7 + weekday` uses weekday `0` for Sunday through `6` for Saturday. `levels` uses `0` through `4` for GitHub's contribution levels; `x` means the date is missing from the source calendar or is in the future. A corresponding `counts` entry is zero for `x`; other counts are integers from 0 through 65,535.

When unavailable, the calendar is represented by `available: false`, `stale: false`, `week_count: 0`, `total: 0`, empty `first_date`, `last_date`, and `levels` strings, and an empty `counts` array. The zero total is only a required placeholder; ignore all calendar values while `available` is false. When stale data is retained, `available` is true and `stale` is true, with the prior calendar grid and total.

## Activity and repositories

`activity` has `available`, `stale`, and `items`. Each item is:

```json
{"type":"push","repository":"blkot/FoloToy","date":"2026-10-01"}
```

`type` is at most 23 printable ASCII characters, `repository` at most 95, and `date` is a ten-character `YYYY-MM-DD` date. Activity is ordered newest first and contains at most four entries.

`repositories` has `available`, `stale`, and `items`. Each item is:

```json
{"full_name":"blkot/FoloToy","pushed_date":"2026-10-01"}
```

The list is ordered by recent push and contains at most three entries. Manual list configuration uses the same fields and limits.

## Achievements

`achievements` has `available`, `stale`, and `items`. Each item has `key`, `label`, and `image`. The list has at most six unique badges. `key` and `label` are at most 47 printable ASCII characters. `image` may be `null`; otherwise it has the same shape as the avatar image but with width and height `24`.

## RGB565LE image files

Each image is a headerless row-major RGB565 bitmap. A 32×32 avatar is exactly 2,048 bytes. A 24×24 badge is exactly 1,152 bytes. Each 16-bit pixel is stored little-endian: the low byte first, followed by the high byte. Bits are `RRRRRGGG GGGBBBBB` (5 red, 6 green, 5 blue). The build scales images to the target square and composites transparency over `#0B0E0D` before conversion.

## Refresh and offline behavior

The 300-second field is a device refresh hint. GitHub Actions checks for upstream changes hourly, and the Pages endpoint is a static public resource. A device should use bounded HTTPS requests, validate the schema and exact image lengths, and retain the last valid payload and image bytes in its runtime cache before replacing them. During a build, the previous generation's hashed image files are copied forward when reachable so a cached manifest can still resolve its image paths after redeployment. The current firmware cache is held in RAM; after a reboot the device needs a network connection to load the feed again. An unavailable section is not a zero-valued profile or a blank contribution calendar; skip it or use a valid device-local cached section.
