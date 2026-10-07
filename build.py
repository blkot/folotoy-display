#!/usr/bin/env python3
"""Build the public Folotoy Display GitHub Pages feed."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests
import yaml
from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "content.yaml"
DIST_PATH = ROOT / "dist"
MAX_DEVICE_JSON_BYTES = 16_384
MAX_HTTP_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_BACKGROUND = (11, 14, 13)
AVATAR_SIZE = 48
LEGACY_AVATAR_SIZE = 32
USER_AGENT = "folotoy-display-pages/1.0"
LEVELS = {
    "NONE": "0",
    "FIRST_QUARTILE": "1",
    "SECOND_QUARTILE": "2",
    "THIRD_QUARTILE": "3",
    "FOURTH_QUARTILE": "4",
}
EVENT_TYPE_NAMES = {
    "CommitCommentEvent": "commit_comment",
    "CreateEvent": "create",
    "DeleteEvent": "delete",
    "ForkEvent": "fork",
    "GollumEvent": "wiki",
    "IssueCommentEvent": "comment",
    "IssuesEvent": "issue",
    "MemberEvent": "member",
    "PublicEvent": "public",
    "PullRequestEvent": "pull_request",
    "PullRequestReviewCommentEvent": "review_comment",
    "PullRequestReviewEvent": "review",
    "PushEvent": "push",
    "ReleaseEvent": "release",
    "WatchEvent": "star",
}


class BuildError(RuntimeError):
    """An input or required source cannot be safely published."""


class HttpFailure(RuntimeError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {urlsplit(url).hostname or 'remote host'}")
        self.status = status


def require_ascii_text(value: Any, field: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise BuildError(f"{field} must be text")
    if len(value) > limit:
        raise BuildError(f"{field} exceeds {limit} characters")
    if not allow_empty and not value:
        raise BuildError(f"{field} must not be empty")
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        raise BuildError(f"{field} must contain printable ASCII only")
    return value


def external_text(value: Any, limit: int) -> str:
    """Convert public profile text to a bounded printable-ASCII field."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    printable = "".join(char if 0x20 <= ord(char) <= 0x7E else " " for char in ascii_text)
    return " ".join(printable.split())[:limit]


def checked_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BuildError(f"{field} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise BuildError(f"{field} is not a real date") from exc
    return value


def load_config() -> dict[str, Any]:
    try:
        raw = CONFIG_PATH.read_bytes()
    except OSError as exc:
        raise BuildError("Could not read content.yaml") from exc
    if len(raw) > 32_768:
        raise BuildError("content.yaml exceeds the 32 KiB input limit")
    try:
        config = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BuildError("content.yaml is not valid UTF-8 YAML") from exc
    if not isinstance(config, dict):
        raise BuildError("content.yaml must contain a YAML mapping")
    allowed = {"username", "display_name", "headline", "recent_repositories", "activity_items"}
    unknown = set(config) - allowed
    if unknown:
        raise BuildError(f"Unknown content.yaml field(s): {', '.join(sorted(map(str, unknown)))}")
    if set(config) != allowed:
        missing = allowed - set(config)
        raise BuildError(f"content.yaml is missing field(s): {', '.join(sorted(missing))}")

    username = require_ascii_text(config["username"], "username", 39, allow_empty=False)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
        raise BuildError("username must be a valid GitHub login")
    for field, limit in (("display_name", 63), ("headline", 36)):
        if config[field] is not None:
            config[field] = require_ascii_text(config[field], field, limit)

    repo_items = config["recent_repositories"]
    if repo_items is not None:
        if not isinstance(repo_items, list) or len(repo_items) > 3:
            raise BuildError("recent_repositories must be null or a list of at most 3 entries")
        checked_repos = []
        for index, item in enumerate(repo_items):
            label = f"recent_repositories[{index}]"
            if not isinstance(item, dict) or set(item) != {"full_name", "pushed_date"}:
                raise BuildError(f"{label} must contain full_name and pushed_date")
            checked_repos.append({
                "full_name": require_ascii_text(item["full_name"], f"{label}.full_name", 95, allow_empty=False),
                "pushed_date": checked_date(item["pushed_date"], f"{label}.pushed_date"),
            })
        config["recent_repositories"] = checked_repos

    activity_items = config["activity_items"]
    if activity_items is not None:
        if not isinstance(activity_items, list) or len(activity_items) > 4:
            raise BuildError("activity_items must be null or a list of at most 4 entries")
        checked_activity = []
        for index, item in enumerate(activity_items):
            label = f"activity_items[{index}]"
            if not isinstance(item, dict) or set(item) != {"type", "repository", "date"}:
                raise BuildError(f"{label} must contain type, repository, and date")
            checked_activity.append({
                "type": require_ascii_text(item["type"], f"{label}.type", 23, allow_empty=False),
                "repository": require_ascii_text(item["repository"], f"{label}.repository", 95, allow_empty=False),
                "date": checked_date(item["date"], f"{label}.date"),
            })
        config["activity_items"] = checked_activity
    return config


class HttpClient:
    """Small bounded HTTP client; authentication is restricted to api.github.com."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.tokens = []
        for token in (os.environ.get("PROFILE_TOKEN", ""), os.environ.get("GITHUB_TOKEN", "")):
            if token and token not in self.tokens:
                self.tokens.append(token)

    def request(self, method: str, url: str, *, api: bool = False, body: dict[str, Any] | None = None,
                max_bytes: int = MAX_HTTP_BYTES, accept: str = "application/json") -> bytes:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            raise BuildError("Refusing a non-HTTPS or credentialed request URL")
        if api and parts.hostname != "api.github.com":
            raise BuildError("GitHub credentials may be sent only to api.github.com")
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if api:
            headers["X-GitHub-Api-Version"] = "2022-11-28"

        token_index = 0
        for attempt in range(3):
            try:
                request_headers = dict(headers)
                if api and token_index < len(self.tokens):
                    request_headers["Authorization"] = f"Bearer {self.tokens[token_index]}"
                response = self.session.request(
                    method,
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=(5, 15),
                    allow_redirects=False,
                    stream=True,
                )
                with response:
                    if (api and response.status_code in (401, 403, 429)
                            and token_index + 1 < len(self.tokens)):
                        token_index += 1
                        continue
                    if (api and method.upper() == "GET" and response.status_code in (401, 403)
                            and token_index < len(self.tokens)):
                        token_index = len(self.tokens)
                        continue
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise HttpFailure(response.status_code, url)
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > max_bytes:
                        raise BuildError("Remote response exceeds its size limit")
                    chunks = bytearray()
                    for chunk in response.iter_content(chunk_size=32_768):
                        if chunk:
                            chunks.extend(chunk)
                            if len(chunks) > max_bytes:
                                raise BuildError("Remote response exceeds its size limit")
                    return bytes(chunks)
            except (requests.RequestException, HttpFailure) as exc:
                if isinstance(exc, HttpFailure) and exc.status not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise BuildError("Remote request did not complete")

    def api_json(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        raw = self.request(method, f"https://api.github.com{path}", api=True, body=body)
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BuildError("GitHub API returned invalid JSON") from exc

    def public_bytes(self, url: str, *, max_bytes: int = MAX_HTTP_BYTES,
                     accept: str = "application/octet-stream") -> bytes:
        return self.request("GET", url, max_bytes=max_bytes, accept=accept)


class AchievementParser(HTMLParser):
    def __init__(self, username: str) -> None:
        super().__init__(convert_charrefs=True)
        self.expected_username = username.lower()
        self.profile_username = ""
        self.badges: list[dict[str, str]] = []
        self.seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in ("img", "meta"):
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            if attributes.get("property", "").lower() == "profile:username":
                self.profile_username = attributes.get("content", "").lower()
            return
        classes = attributes.get("class", "").split()
        if "achievement-badge-sidebar" not in classes:
            return
        src = attributes.get("src", "")
        parsed = urlsplit(src)
        if parsed.scheme != "https" or parsed.hostname != "github.githubassets.com" or parsed.username or parsed.password:
            return

        hover_parts = [part for part in urlsplit(attributes.get("data-hovercard-url", "")).path.split("/") if part]
        key = ""
        if "achievements" in hover_parts:
            index = hover_parts.index("achievements")
            if index + 1 < len(hover_parts):
                key = hover_parts[index + 1]
        alt = attributes.get("alt", "").strip()
        label = re.sub(r"^Achievement:\s*", "", alt, flags=re.IGNORECASE).strip()
        if not key:
            key = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
        key = external_text(key, 47).strip(" -").lower()
        label = external_text(label or key.replace("-", " ").title(), 47)
        if not key or key in self.seen:
            return
        self.seen.add(key)
        self.badges.append({"key": key, "label": label, "src": src})


class GitHubSource:
    GRAPHQL_CALENDAR = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                contributionLevel
                date
              }
            }
          }
        }
      }
    }
    """

    def __init__(self, client: HttpClient, username: str) -> None:
        self.client = client
        self.username = username
        self.quoted_username = quote(username, safe="")

    def profile(self) -> dict[str, Any]:
        result = self.client.api_json(f"/users/{self.quoted_username}")
        if not isinstance(result, dict) or result.get("login", "").lower() != self.username.lower():
            raise BuildError("GitHub returned no matching public profile")
        required_counts = ("public_repos", "followers", "following")
        for field in required_counts:
            if not isinstance(result.get(field), int) or result[field] < 0:
                raise BuildError("GitHub profile is missing required public counts")
        if not isinstance(result.get("avatar_url"), str):
            result["avatar_url"] = ""
        return result

    def calendar(self) -> dict[str, Any]:
        result = self.client.api_json("/graphql", method="POST", body={
            "query": self.GRAPHQL_CALENDAR,
            "variables": {"login": self.username},
        })
        try:
            if result.get("errors"):
                raise BuildError("GitHub GraphQL calendar query failed")
            calendar = result["data"]["user"]["contributionsCollection"]["contributionCalendar"]
            weeks = calendar["weeks"]
            total = calendar["totalContributions"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise BuildError("GitHub returned an invalid contribution calendar") from exc
        if not isinstance(weeks, list) or not 1 <= len(weeks) <= 54:
            raise BuildError("GitHub contribution calendar has an invalid week count")
        if not isinstance(total, int) or total < 0:
            raise BuildError("GitHub returned an invalid contribution total")

        entries: dict[date, tuple[int, str]] = {}
        for week in weeks:
            days = week.get("contributionDays") if isinstance(week, dict) else None
            if not isinstance(days, list):
                raise BuildError("GitHub contribution calendar week is invalid")
            for day in days:
                if not isinstance(day, dict):
                    raise BuildError("GitHub returned an invalid contribution day")
                try:
                    day_date = date.fromisoformat(day["date"])
                    count = int(day["contributionCount"])
                    level = LEVELS[day["contributionLevel"]]
                except (KeyError, TypeError, ValueError) as exc:
                    raise BuildError("GitHub returned an invalid contribution day") from exc
                if count < 0:
                    raise BuildError("GitHub returned a negative contribution count")
                if day_date in entries:
                    raise BuildError("GitHub returned a duplicate contribution date")
                entries[day_date] = (min(count, 65_535), level)
        if not entries:
            raise BuildError("GitHub contribution calendar contains no dates")

        min_date = min(entries)
        max_date = max(entries)
        first_date = min_date - timedelta(days=(min_date.weekday() + 1) % 7)
        last_date = max_date + timedelta(days=(5 - max_date.weekday()) % 7)
        week_count = ((last_date - first_date).days + 1) // 7
        if not 1 <= week_count <= 54:
            raise BuildError("GitHub contribution calendar has an invalid date range")

        today = datetime.now(timezone.utc).date()
        levels: list[str] = []
        counts: list[int] = []
        for offset in range(week_count * 7):
            day_date = first_date + timedelta(days=offset)
            found = entries.get(day_date)
            if found is None or day_date > today:
                levels.append("x")
                counts.append(0)
                continue
            count, level = found
            levels.append(level)
            counts.append(count)
        return {
            "available": True,
            "stale": False,
            "week_count": week_count,
            "total": total,
            "first_date": first_date.isoformat(),
            "last_date": last_date.isoformat(),
            "levels": "".join(levels),
            "counts": counts,
        }

    def activity(self) -> list[dict[str, str]]:
        result = self.client.api_json(f"/users/{self.quoted_username}/events/public?per_page=100")
        if not isinstance(result, list):
            raise BuildError("GitHub returned an invalid public event list")
        items = []
        for event in result:
            if not isinstance(event, dict):
                continue
            event_type = EVENT_TYPE_NAMES.get(event.get("type"), "")
            repository = event.get("repo")
            created_at = event.get("created_at")
            if not event_type or not isinstance(repository, dict) or not isinstance(repository.get("name"), str):
                continue
            if not isinstance(created_at, str) or len(created_at) < 10:
                continue
            try:
                event_date = checked_date(created_at[:10], "event date")
            except BuildError:
                continue
            items.append({
                "type": external_text(event_type, 23),
                "repository": external_text(repository["name"], 95),
                "date": event_date,
            })
            if len(items) == 4:
                break
        return items

    def repositories(self) -> list[dict[str, str]]:
        result = self.client.api_json(
            f"/users/{self.quoted_username}/repos?sort=pushed&direction=desc&per_page=100"
        )
        if not isinstance(result, list):
            raise BuildError("GitHub returned an invalid repository list")
        items = []
        for repository in result:
            if not isinstance(repository, dict):
                continue
            full_name = repository.get("full_name")
            pushed_at = repository.get("pushed_at")
            if not isinstance(full_name, str) or not isinstance(pushed_at, str) or len(pushed_at) < 10:
                continue
            try:
                pushed_date = checked_date(pushed_at[:10], "repository pushed date")
            except BuildError:
                continue
            items.append({
                "full_name": external_text(full_name, 95),
                "pushed_date": pushed_date,
            })
            if len(items) == 3:
                break
        return items

    def achievements(self) -> list[dict[str, str]]:
        profile_url = f"https://github.com/{self.quoted_username}"
        page = self.client.public_bytes(profile_url, max_bytes=MAX_HTTP_BYTES, accept="text/html")
        parser = AchievementParser(self.username)
        try:
            parser.feed(page.decode("utf-8", "replace"))
            parser.close()
        except Exception as exc:
            raise BuildError("Could not parse the public GitHub profile page") from exc
        if parser.profile_username != parser.expected_username:
            raise BuildError("GitHub profile HTML identity marker is missing or mismatched")
        if not parser.badges:
            raise BuildError("GitHub achievement badge markup was not found")
        return parser.badges[:6]


def valid_pages_base(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname
            or not parts.hostname.lower().endswith(".github.io")
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.query or parts.fragment):
        raise BuildError("PAGES_BASE_URL must be an HTTPS GitHub Pages URL")
    path = parts.path.rstrip("/")
    if path not in ("", "/folotoy-display"):
        raise BuildError("PAGES_BASE_URL must point to the folotoy-display Pages site")
    return f"https://{parts.hostname}{path}"


class AssetStore:
    def __init__(self, client: HttpClient, pages_base: str, output_dir: Path) -> None:
        self.client = client
        self.pages_base = pages_base.rstrip("/") + "/"
        self.output_dir = output_dir
        self.asset_dir = output_dir / "assets"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.cached: dict[str, bytes] = {}

    def _store(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"assets/{digest}.rgb565"
        target = self.output_dir / relative
        target.write_bytes(raw)
        self.cached[relative] = raw
        return relative

    def convert_and_store(self, url: str, width: int, height: int, *, avatar: bool) -> str:
        parts = urlsplit(url)
        expected_host = "avatars.githubusercontent.com" if avatar else "github.githubassets.com"
        if (parts.scheme != "https" or parts.hostname != expected_host or parts.username or parts.password
                or parts.port not in (None, 443)):
            raise BuildError("Image URL is outside the approved public GitHub image host")
        source = self.client.public_bytes(url, max_bytes=MAX_IMAGE_BYTES, accept="image/*")
        try:
            with Image.open(io.BytesIO(source)) as original:
                if original.width < 1 or original.height < 1 or original.width > 4096 or original.height > 4096:
                    raise BuildError("Image dimensions exceed the conversion limit")
                if original.width * original.height > 12_000_000:
                    raise BuildError("Image pixel count exceeds the conversion limit")
                image = ImageOps.fit(original.convert("RGBA"), (width, height), method=Image.Resampling.LANCZOS)
                background = Image.new("RGBA", (width, height), (*IMAGE_BACKGROUND, 255))
                rgb_image = Image.alpha_composite(background, image).convert("RGB")
        except BuildError:
            raise
        except Exception as exc:
            raise BuildError("GitHub image could not be decoded") from exc

        raw = bytearray()
        for red, green, blue in rgb_image.getdata():
            pixel = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            raw.extend((pixel & 0xFF, pixel >> 8))
        expected = width * height * 2
        if len(raw) != expected:
            raise BuildError("RGB565 conversion produced an invalid image length")
        return self._store(bytes(raw))

    def reuse(self, image: Any, expected_width: int, expected_height: int) -> dict[str, Any] | None:
        if not isinstance(image, dict):
            return None
        path = image.get("path")
        if (image.get("width") != expected_width or image.get("height") != expected_height
                or image.get("format") != "rgb565le" or not isinstance(path, str)
                or not re.fullmatch(r"assets/[0-9a-f]{64}\.rgb565", path)):
            return None
        raw = self.cached.get(path)
        if raw is None:
            url = urljoin(self.pages_base, path)
            try:
                raw = self.client.public_bytes(url, max_bytes=MAX_IMAGE_BYTES)
            except Exception:
                return None
        expected_length = expected_width * expected_height * 2
        if len(raw) != expected_length or hashlib.sha256(raw).hexdigest() != Path(path).stem:
            return None
        target = self.output_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        self.cached[path] = raw
        return {"path": path, "width": expected_width, "height": expected_height, "format": "rgb565le"}


def load_previous(client: HttpClient, pages_base: str, username: str) -> dict[str, Any] | None:
    try:
        raw = client.public_bytes(urljoin(pages_base.rstrip("/") + "/", "device.json"), max_bytes=MAX_DEVICE_JSON_BYTES)
        previous = json.loads(raw)
    except Exception:
        return None
    if (not isinstance(previous, dict) or previous.get("schema_version") != 1
            or not isinstance(previous.get("profile"), dict)
            or str(previous["profile"].get("username", "")).lower() != username.lower()):
        return None
    return previous


def unavailable_calendar() -> dict[str, Any]:
    return {
        "available": False,
        "stale": False,
        "week_count": 0,
        "total": 0,
        "first_date": "",
        "last_date": "",
        "levels": "",
        "counts": [],
    }


def warn_optional(section: str, error: Exception) -> None:
    reason = f"HTTP {error.status}" if isinstance(error, HttpFailure) else type(error).__name__
    print(f"Warning: {section} refresh failed ({reason}); using same-user cached data when available.", file=sys.stderr)


def valid_previous_module(name: str, module: Any) -> bool:
    if (not isinstance(module, dict) or module.get("available") is not True
            or not isinstance(module.get("stale"), bool)):
        return False
    try:
        if name == "calendar":
            week_count = module["week_count"]
            total = module["total"]
            first = checked_date(module["first_date"], "cached calendar first_date")
            last = checked_date(module["last_date"], "cached calendar last_date")
            levels = module["levels"]
            counts = module["counts"]
            if (not isinstance(week_count, int) or isinstance(week_count, bool) or not 1 <= week_count <= 54
                    or not isinstance(total, int) or isinstance(total, bool) or total < 0
                    or not isinstance(levels, str) or len(levels) != week_count * 7
                    or not re.fullmatch(r"[01234x]+", levels)
                    or not isinstance(counts, list) or len(counts) != week_count * 7
                    or any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65_535
                           for value in counts)):
                return False
            if any(level == "x" and counts[index] != 0 for index, level in enumerate(levels)):
                return False
            first_date = date.fromisoformat(first)
            expected_last = first_date + timedelta(days=week_count * 7 - 1)
            return first_date.weekday() == 6 and last == expected_last.isoformat()
        items = module.get("items")
        if not isinstance(items, list):
            return False
        limit = {"activity": 4, "repositories": 3, "achievements": 6}.get(name)
        if limit is None or len(items) > limit:
            return False
        if name == "activity":
            for item in items:
                if not isinstance(item, dict) or set(item) != {"type", "repository", "date"}:
                    return False
                require_ascii_text(item["type"], "cached activity type", 23, allow_empty=False)
                require_ascii_text(item["repository"], "cached activity repository", 95, allow_empty=False)
                checked_date(item["date"], "cached activity date")
        elif name == "repositories":
            for item in items:
                if not isinstance(item, dict) or set(item) != {"full_name", "pushed_date"}:
                    return False
                require_ascii_text(item["full_name"], "cached repository name", 95, allow_empty=False)
                checked_date(item["pushed_date"], "cached repository date")
        else:
            keys = set()
            for item in items:
                if not isinstance(item, dict) or set(item) != {"key", "label", "image"}:
                    return False
                key = require_ascii_text(item["key"], "cached achievement key", 47, allow_empty=False)
                require_ascii_text(item["label"], "cached achievement label", 47, allow_empty=False)
                if key in keys:
                    return False
                keys.add(key)
                if item["image"] is not None and not isinstance(item["image"], dict):
                    return False
        return True
    except (BuildError, KeyError, TypeError, ValueError):
        return False


def previous_module(previous: dict[str, Any] | None, name: str, default: dict[str, Any],
                    *, assets: AssetStore, now_username: str) -> dict[str, Any]:
    if not previous or str(previous.get("profile", {}).get("username", "")).lower() != now_username.lower():
        return default
    module = previous.get(name)
    if not valid_previous_module(name, module):
        return default
    reused = json.loads(json.dumps(module))
    reused["stale"] = True
    if name == "calendar":
        return reused
    if name == "achievements":
        items = reused.get("items", [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item["image"] = assets.reuse(item.get("image"), 24, 24)
        return reused
    return reused


def previous_avatar_size(image: Any) -> int | None:
    if not isinstance(image, dict):
        return None
    width = image.get("width")
    height = image.get("height")
    if (not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)
            or width != height or width not in (AVATAR_SIZE, LEGACY_AVATAR_SIZE)):
        return None
    return width


def retain_previous_images(previous: dict[str, Any] | None, assets: AssetStore, username: str) -> None:
    if not previous or str(previous.get("profile", {}).get("username", "")).lower() != username.lower():
        return
    profile = previous.get("profile", {})
    if isinstance(profile, dict):
        avatar = profile.get("avatar")
        size = previous_avatar_size(avatar)
        if size is not None:
            assets.reuse(avatar, size, size)
    achievements = previous.get("achievements")
    if isinstance(achievements, dict) and isinstance(achievements.get("items"), list):
        for badge in achievements["items"][:6]:
            if isinstance(badge, dict):
                assets.reuse(badge.get("image"), 24, 24)


def make_profile(profile: dict[str, Any], config: dict[str, Any], assets: AssetStore,
                 previous: dict[str, Any] | None) -> dict[str, Any]:
    username = config["username"]
    name_source = config["display_name"] if config["display_name"] is not None else profile.get("name")
    headline_source = config["headline"] if config["headline"] is not None else profile.get("bio")
    name = (require_ascii_text(name_source, "display_name", 63) if config["display_name"] is not None
            else external_text(name_source or username, 63) or username)
    headline = (require_ascii_text(headline_source, "headline", 36) if config["headline"] is not None
                else external_text(headline_source, 36))

    avatar = None
    avatar_url = profile.get("avatar_url", "")
    avatar_download_failed = False
    if avatar_url:
        try:
            path = assets.convert_and_store(avatar_url, AVATAR_SIZE, AVATAR_SIZE, avatar=True)
            avatar = {"path": path, "width": AVATAR_SIZE, "height": AVATAR_SIZE, "format": "rgb565le"}
        except Exception as exc:
            warn_optional("avatar image", exc)
            avatar_download_failed = True
    if avatar is None and previous and str(previous.get("profile", {}).get("username", "")).lower() == username.lower():
        previous_avatar = previous.get("profile", {}).get("avatar")
        avatar = assets.reuse(previous_avatar, AVATAR_SIZE, AVATAR_SIZE)
        if avatar is None and avatar_download_failed:
            size = previous_avatar_size(previous_avatar)
            if size == LEGACY_AVATAR_SIZE:
                avatar = assets.reuse(previous_avatar, size, size)

    return {
        "username": username,
        "name": name,
        "headline": headline,
        "public_repos": profile["public_repos"],
        "followers": profile["followers"],
        "following": profile["following"],
        "avatar": avatar,
    }


def build_activity(source: GitHubSource, config: dict[str, Any], previous: dict[str, Any] | None,
                   assets: AssetStore) -> dict[str, Any]:
    if config["activity_items"] is not None:
        items = sorted(config["activity_items"], key=lambda item: item["date"], reverse=True)
        return {"available": True, "stale": False, "items": items}
    try:
        items = sorted(source.activity(), key=lambda item: item["date"], reverse=True)
        return {"available": True, "stale": False, "items": items}
    except Exception as exc:
        warn_optional("activity", exc)
        return previous_module(previous, "activity", {"available": False, "stale": False, "items": []},
                               assets=assets, now_username=config["username"])


def build_repositories(source: GitHubSource, config: dict[str, Any], previous: dict[str, Any] | None,
                       assets: AssetStore) -> dict[str, Any]:
    if config["recent_repositories"] is not None:
        items = sorted(config["recent_repositories"], key=lambda item: item["pushed_date"], reverse=True)
        return {"available": True, "stale": False, "items": items}
    try:
        return {"available": True, "stale": False, "items": source.repositories()}
    except Exception as exc:
        warn_optional("repositories", exc)
        return previous_module(previous, "repositories", {"available": False, "stale": False, "items": []},
                               assets=assets, now_username=config["username"])


def build_calendar(source: GitHubSource, previous: dict[str, Any] | None,
                   assets: AssetStore, username: str) -> dict[str, Any]:
    try:
        return source.calendar()
    except Exception as exc:
        warn_optional("calendar", exc)
        return previous_module(previous, "calendar", unavailable_calendar(), assets=assets,
                               now_username=username)


def build_achievements(source: GitHubSource, previous: dict[str, Any] | None,
                       assets: AssetStore, username: str) -> dict[str, Any]:
    prior_items: dict[str, Any] = {}
    if previous and str(previous.get("profile", {}).get("username", "")).lower() == username.lower():
        prior_module = previous.get("achievements")
        if isinstance(prior_module, dict) and isinstance(prior_module.get("items"), list):
            prior_items = {item.get("key"): item for item in prior_module["items"]
                           if isinstance(item, dict) and isinstance(item.get("key"), str)}
    try:
        scraped = source.achievements()
    except Exception as exc:
        warn_optional("achievements", exc)
        return previous_module(previous, "achievements", {"available": False, "stale": False, "items": []},
                               assets=assets, now_username=username)

    items = []
    reused_any = False
    for badge in scraped[:6]:
        image = None
        try:
            path = assets.convert_and_store(badge["src"], 24, 24, avatar=False)
            image = {"path": path, "width": 24, "height": 24, "format": "rgb565le"}
        except Exception as exc:
            warn_optional(f"achievement image {badge['key']}", exc)
            old_badge = prior_items.get(badge["key"])
            if isinstance(old_badge, dict):
                image = assets.reuse(old_badge.get("image"), 24, 24)
                reused_any = reused_any or image is not None
        items.append({"key": badge["key"], "label": badge["label"], "image": image})
    return {"available": True, "stale": reused_any, "items": items}


def render_index(data: dict[str, Any], payload_size: int) -> str:
    username = html.escape(data["profile"]["username"])
    name = html.escape(data["profile"]["name"])
    generated = html.escape(data["generated_at"])
    repository = os.environ.get("GITHUB_REPOSITORY", "blkot/folotoy-display")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        repository = "blkot/folotoy-display"
    source_url = html.escape(f"https://github.com/{repository}", quote=True)
    sections = ("calendar", "activity", "repositories", "achievements")
    rows = "".join(
        f"<tr><th>{html.escape(section.title())}</th><td>"
        f"{'available' if data[section]['available'] else 'unavailable'}"
        f"{' · stale' if data[section]['stale'] else ''}</td></tr>"
        for section in sections
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Folotoy Display feed</title>
  <style>
    :root {{ color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }}
    body {{ max-width: 46rem; margin: 3rem auto; padding: 0 1.25rem; }}
    h1 {{ margin-bottom: .25rem; }}
    .muted {{ opacity: .72; }}
    table {{ width: 100%; border-collapse: collapse; margin: 1.5rem 0; }}
    th, td {{ padding: .55rem .25rem; border-bottom: 1px solid #8886; text-align: left; }}
    th {{ width: 45%; }}
    code {{ overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>Folotoy Display feed</h1>
  <p>Public device data for <a href="https://github.com/{username}">{name} (@{username})</a>.</p>
  <p><a href="device.json">Open device.json</a></p>
  <table><tbody>{rows}</tbody></table>
  <p class="muted">Generated {generated} · {payload_size} JSON bytes · refresh hint 300 seconds</p>
  <p class="muted">Source configuration: <a href="{source_url}">GitHub repository</a></p>
</body>
</html>
"""


def pages_base_url(username: str) -> str:
    configured = os.environ.get("PAGES_BASE_URL")
    if configured:
        return valid_pages_base(configured)
    return f"https://{username.lower()}.github.io/folotoy-display"


def main() -> None:
    config = load_config()
    username = config["username"]
    client = HttpClient()
    pages_base = pages_base_url(username)
    previous = load_previous(client, pages_base, username)
    stage_name = tempfile.mkdtemp(prefix=".dist-build-", dir=ROOT)
    stage = Path(stage_name)
    try:
        assets = AssetStore(client, pages_base, stage)
        retain_previous_images(previous, assets, username)
        source = GitHubSource(client, username)
        profile_source = source.profile()
        profile = make_profile(profile_source, config, assets, previous)
        calendar = build_calendar(source, previous, assets, username)
        activity = build_activity(source, config, previous, assets)
        repositories = build_repositories(source, config, previous, assets)
        achievements = build_achievements(source, previous, assets, username)

        data = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "refresh_seconds": 300,
            "profile": profile,
            "calendar": calendar,
            "activity": activity,
            "repositories": repositories,
            "achievements": achievements,
        }
        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_DEVICE_JSON_BYTES:
            raise BuildError(f"device.json is {len(payload)} bytes; limit is {MAX_DEVICE_JSON_BYTES}")
        (stage / "device.json").write_bytes(payload)
        (stage / "index.html").write_text(render_index(data, len(payload)), encoding="utf-8")

        if DIST_PATH.exists():
            shutil.rmtree(DIST_PATH)
        os.replace(stage, DIST_PATH)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except BuildError as exc:
        raise SystemExit(f"Build failed: {exc}") from exc
    except Exception as exc:
        raise SystemExit(f"Build failed: {type(exc).__name__}") from exc
