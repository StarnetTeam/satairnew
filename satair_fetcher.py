import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


PROJECT_ID = "satair-6983b"
BASE_URL = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"
# لا نحتاج competitions أو teams أثناء بناء الناتج؛ جلبهما كان يضاعف طلبات Firestore ويزيد احتمال 429.
COLLECTIONS = ("matchDays", "channels")
DEFAULT_CACHE_FILE = "satair_cache.json"
DEFAULT_RAW_CACHE_FILE = "satair_raw_cache.json"
DEFAULT_OUTPUT_FILE = "matches.json"

# الافتراضي: اليوم الحالي، وغدًا، وبعد غد.
# استخدم --future-only إذا أردت النافذة المستقبلية فقط بدءًا من الغد.
AUDIO_CHANNEL_TERMS = (
    "bein", "be in", "بين سبورت", "بين سبورتس", "بين", "shahid", "شاهد",
    "tod", "تود", "abu dhabi premium", "ابوظبي بريميم", "أبو ظبي بريميم",
    "stc", "mbc", "ام بي سي", "إم بي سي",
)
EXCLUDED_COMPETITION_TERMS = (
    "كأس البرتغال", "الدوري البرتغالي", "دوري أذربيجان", "الدوري الأذربيجاني",
    "الدوري اليوناني", "الدوري النمساوي", "الدوري النرويجي", "الدوري المجري",
    "الدوري الكرواتي", "الدوري الصربي", "الدوري السويدي", "الدوري السلوفيني",
    "الدوري الروماني", "الدوري الروسي", "الدوري الدنماركي", "الدوري البولندي",
    "الدوري الكويتي", "الدوري الإماراتي", "الدوري الاماراتي", "الدوري الجزائري",
    "الدوري الأرجنتيني", "الدوري الارجنتيني", "الدوري البرازيلي", "الدوري البرازيلى",
    "الدوري البلجيكي", "الدوري الاسكتلندي", "الدوري الأسكتلندي",
)
NATIONAL_TERMS = ("منتخب", "منتخبات", "national team", "nations", "كأس العالم", "اليورو", "euro", "world cup")
COMMENTATOR_KEYS = (
    "commentator", "commentators", "commentatorName", "commentator_name",
    "commentatorAudio", "commentator_audio", "audioCommentator", "audio_commentator",
    "commentatorId", "commentator_id", "voiceCommentator", "voice_commentator",
    "المعلق", "المعلقين", "اسم المعلق", "اسم المعلقين", "التعليق", "معلق",
)


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=0.7,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}), raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"Accept": "application/json", "User-Agent": "SatAirFetcher/2.0"})
    return session


def parse_value(value: Any) -> Any:
    if value is None or not isinstance(value, dict):
        return value
    for key in ("stringValue", "booleanValue", "integerValue", "doubleValue", "timestampValue", "referenceValue", "bytesValue"):
        if key in value:
            return value[key]
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return [parse_value(x) for x in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {k: parse_value(v) for k, v in value["mapValue"].get("fields", {}).items()}
    return value


def fetch_collection(session: requests.Session, collection: str) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/{collection}"
    documents: List[Dict[str, Any]] = []
    token: Optional[str] = None
    while True:
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        response = session.get(url, params=params, timeout=(10, 45))
        response.raise_for_status()
        payload = response.json()
        for doc in payload.get("documents", []):
            doc_id = doc.get("name", "").rsplit("/", 1)[-1]
            item = {"id": doc_id}
            item.update({k: parse_value(v) for k, v in doc.get("fields", {}).items()})
            documents.append(item)
        token = payload.get("nextPageToken")
        if not token:
            return documents


def _required_channel_ids(match_days: List[Dict[str, Any]]) -> Set[str]:
    """Find channel IDs only for non-excluded matches in the requested date window."""
    today = datetime.now().date()
    target_dates = {(today + timedelta(days=i)).isoformat() for i in range(3)}
    required: Set[str] = set()
    for day in match_days:
        day_date = extract_date(day.get("date") or day.get("day") or day.get("matchDate"))
        if day_date not in target_dates:
            continue
        candidates = ([day] if any(k in day for k in ("competition", "team1", "team2", "homeTeam", "awayTeam")) else [])
        candidates += [m for m in (day.get("matches") or day.get("games") or []) if isinstance(m, dict)]
        for item in candidates:
            competition = str(item.get("competition") or item.get("competitionName") or "").strip()
            if competition and is_excluded_competition(competition):
                continue
            required.update(as_id_list(item.get("channelIds") or item.get("channels") or []))
    return required


def _save_raw_cache(path: Path, data: Dict[str, List[Dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def fetch_satair_data(raw_cache_path: Path = Path(DEFAULT_RAW_CACHE_FILE)) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch matchDays first; fetch channels only when required IDs are not already cached."""
    try:
        cached_raw = json.loads(raw_cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cached_raw = {}
    cached_match_days = cached_raw.get("matchDays", []) if isinstance(cached_raw.get("matchDays", []), list) else []
    cached_channels = cached_raw.get("channels", []) if isinstance(cached_raw.get("channels", []), list) else []
    results: Dict[str, List[Dict[str, Any]]] = {"matchDays": [], "channels": cached_channels}
    session = make_session()

    try:
        results["matchDays"] = fetch_collection(session, "matchDays")
        _save_raw_cache(raw_cache_path, results)
    except requests.RequestException as exc:
        results["matchDays"] = cached_match_days
        message = "استخدام كاش المباريات السابق" if cached_match_days else "لا يوجد كاش للمباريات"
        print(f"تحذير: تعذر جلب matchDays: {exc}؛ {message}.", file=sys.stderr)

    # إذا لم توجد مباريات أو كانت كل القنوات موجودة في الكاش، لا نرسل طلب channels.
    required_ids = _required_channel_ids(results["matchDays"])
    cached_ids = {str(ch.get("id")) for ch in cached_channels if isinstance(ch, dict)}
    missing_ids = required_ids - cached_ids
    if required_ids and missing_ids:
        try:
            results["channels"] = fetch_collection(session, "channels")
            _save_raw_cache(raw_cache_path, results)
        except requests.RequestException as exc:
            results["channels"] = cached_channels
            print(f"تحذير: تعذر تحديث channels: {exc}؛ سيتم استخدام الكاش المتاح.", file=sys.stderr)
    return results


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[إأآٱ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text)


def extract_date(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("date") or value.get("value") or value.get("timestamp")
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{4})[-/]([01]?\d)[-/]([0-3]?\d)", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def load_cache(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"teams": {}, "channels": {}, "channels_meta": {}, "competitions": {}}


def save_cache(path: Path, cache: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def cached_logo(cache: Dict[str, Dict[str, str]], kind: str, key: Any, logo: Any) -> str:
    bucket = cache.setdefault(kind, {})
    cache_key = str(key or "").strip()
    if logo:
        bucket[cache_key] = str(logo)
        return str(logo)
    return bucket.get(cache_key, "")


def normalize_match(match: Dict[str, Any], match_date: str) -> Dict[str, Any]:
    home_name = match.get("team1") or match.get("homeTeam") or match.get("home")
    away_name = match.get("team2") or match.get("awayTeam") or match.get("away")
    home = home_name if isinstance(home_name, dict) else {"name": home_name, "logo": match.get("logo1")}
    away = away_name if isinstance(away_name, dict) else {"name": away_name, "logo": match.get("logo2")}
    return {
        "id": match.get("id"), "date": match_date,
        "competition": match.get("competition") or match.get("competitionName"),
        "competitionLogo": match.get("competitionLogo") or match.get("competition_logo"),
        "competitionStage": match.get("competitionStage") or match.get("stage"),
        "home": {"name": home.get("name") or home.get("teamName"), "logo": home.get("logo") or home.get("image")},
        "away": {"name": away.get("name") or away.get("teamName"), "logo": away.get("logo") or away.get("image")},
        "time": match.get("time") or match.get("matchTime"), "status": match.get("status"),
        "stadium": match.get("stadium"), "live": bool(match.get("live", False)),
        "channelIds": match.get("channelIds") or match.get("channels") or [],
        "channelCommentators": match.get("channelCommentators") or match.get("channel_commentators") or {},
        "commentator": extract_commentator(match),
    }


def _usable_commentator_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text and normalize_text(text) not in {"غير محدد", "غير معروف", "لم يحدد", "لم يحدد بعد", "unknown", "none", "null"}:
            return text
    return ""


def _commentator_names(value: Any) -> List[str]:
    """Extract names from strings, arrays, or Firestore map objects."""
    if isinstance(value, str):
        text = _usable_commentator_text(value)
        return [text] if text else []
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_commentator_names(item))
        return result
    if isinstance(value, dict):
        # Common object forms: {name: ...}, {displayName: ...}, {arabicName: ...}.
        for name_key in ("name", "displayName", "arabicName", "fullName", "title", "value", "text"):
            text = _usable_commentator_text(value.get(name_key))
            if text:
                return [text]
        result: List[str] = []
        for key, item in value.items():
            if normalize_text(key) in {normalize_text(k) for k in COMMENTATOR_KEYS}:
                result.extend(_commentator_names(item))
        return result
    return []


def extract_commentator(value: Any) -> Any:
    """Search recursively because the app may nest commentator under broadcast/audio maps."""
    found: List[str] = []
    seen: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_norm = normalize_text(key).replace(" ", "")
                is_commentator_key = (
                    key_norm in {normalize_text(k).replace(" ", "") for k in COMMENTATOR_KEYS}
                    or "commentator" in key_norm
                    or "معلق" in key_norm
                )
                if is_commentator_key:
                    names = _commentator_names(item)
                    # SatAir stores channelCommentators as {channelId: {name: ...}}.
                    if isinstance(item, dict) and not names:
                        for child in item.values():
                            names.extend(_commentator_names(child))
                    for name in names:
                        marker = normalize_text(name)
                        if marker not in seen:
                            seen.add(marker)
                            found.append(name)
                # Continue descending for nested Firestore maps and broadcast objects.
                if isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found if found else "غير محدد"


def commentator_value(match: Dict[str, Any]) -> Any:
    value = match.get("commentator")
    if isinstance(value, list) and value:
        return " / ".join(str(item) for item in value)
    return _usable_commentator_text(value) or "غير محدد"


def is_excluded_competition(name: str) -> bool:
    normalized = normalize_text(name)
    if any(term in normalized for term in map(normalize_text, NATIONAL_TERMS)):
        return False
    return any(normalize_text(term) in normalized for term in EXCLUDED_COMPETITION_TERMS)


def as_id_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    ids = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id") or item.get("channelId") or item.get("reference")
        if item:
            ids.append(str(item).rsplit("/", 1)[-1])
    return ids


def process_data(raw: Dict[str, List[Dict[str, Any]]], cache_path: Path, include_today: bool = True, days: int = 3) -> Dict[str, Any]:
    now = datetime.now()
    first_day = now.date() if include_today else now.date() + timedelta(days=1)
    target_dates = [(first_day + timedelta(days=i)).isoformat() for i in range(days)]
    target_set = set(target_dates)
    cache = load_cache(cache_path)
    channels_map = {str(ch.get("id")): ch for ch in raw.get("channels", [])}
    matches: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for day in raw.get("matchDays", []):
        day_date = extract_date(day.get("date") or day.get("day") or day.get("matchDate"))
        if day_date not in target_set:
            continue
        candidates = ([day] if any(k in day for k in ("competition", "team1", "team2", "homeTeam", "awayTeam")) else [])
        candidates += [m for m in (day.get("matches") or day.get("games") or []) if isinstance(m, dict)]
        for item in candidates:
            match = normalize_match(item, day_date)
            match_key = str(match.get("id") or f"{day_date}|{match['home'].get('name')}|{match['away'].get('name')}|{match.get('time')}")
            if match_key in seen:
                continue
            seen.add(match_key)
            matches.append(match)

    championships: Dict[str, Dict[str, Any]] = {}
    for match in matches:
        comp_name = str(match.get("competition") or "بطولة غير معروفة").strip()
        if is_excluded_competition(comp_name):
            continue
        comp_key = normalize_text(comp_name)
        comp = championships.setdefault(comp_key, {"name": comp_name, "logo": "", "stage": match.get("competitionStage"), "matches": []})
        comp["logo"] = cached_logo(cache, "competitions", comp_key, match.get("competitionLogo"))
        home_key, away_key = normalize_text(match["home"]["name"]), normalize_text(match["away"]["name"])
        match["home"]["logo"] = cached_logo(cache, "teams", home_key, match["home"].get("logo"))
        match["away"]["logo"] = cached_logo(cache, "teams", away_key, match["away"].get("logo"))
        audio, regular = [], []
        for ch_id in as_id_list(match.get("channelIds")):
            ch = channels_map.get(ch_id, {"id": ch_id})
            cached_meta = cache.setdefault("channels_meta", {}).get(ch_id, {})
            if not isinstance(cached_meta, dict):
                cached_meta = {}
            name = str(ch.get("displayName") or ch.get("name") or ch.get("channelName") or cached_meta.get("name") or "قناة غير معروفة").strip()
            key = normalize_text(name)
            logo_value = ch.get("logo") or ch.get("image") or cached_meta.get("logo") or cache.setdefault("channels", {}).get(ch_id, "")
            logo_value = cached_logo(cache, "channels", key, logo_value)
            # نحفظ أيضًا بالـ ID حتى يعمل الكاش إذا اختفى سجل القناة وبقي channelId فقط.
            if ch_id:
                cache.setdefault("channels_meta", {})[ch_id] = {"name": name, "logo": logo_value, "satellite": ch.get("satellite") or cached_meta.get("satellite", "")}
                if logo_value:
                    cache.setdefault("channels", {})[ch_id] = logo_value
            item = {"name": name, "logo": logo_value, "satellite": ch.get("satellite") or cached_meta.get("satellite", "")}
            channel_commentators = match.get("channelCommentators") or {}
            channel_commentator = channel_commentators.get(ch_id) if isinstance(channel_commentators, dict) else None
            channel_names = _commentator_names(channel_commentator)
            is_audio_channel = any(term in key for term in map(normalize_text, AUDIO_CHANNEL_TERMS)) or bool(channel_names)
            if is_audio_channel:
                item["commentator"] = " / ".join(channel_names) if channel_names else commentator_value(match)
                audio.append(item)
            else:
                regular.append(item)
        comp["matches"].append({"id": match.get("id"), "date": match["date"], "home": match["home"], "away": match["away"], "time": match.get("time"), "status": match.get("status") or "لم تبدأ", "score": match.get("score") or "---", "channels": regular, "audio": audio, "commentator": commentator_value(match)})

    save_cache(cache_path, cache)
    return {"last_updated": now.strftime("%Y-%m-%d %H:%M:%S"), "requested_dates": target_dates, "data": sorted(championships.values(), key=lambda c: len(c["matches"]), reverse=True)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SatAir matches reliably")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--cache", default=DEFAULT_CACHE_FILE)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--include-today", action="store_true", help="توافق عكسي؛ اليوم مشمول افتراضيًا")
    parser.add_argument("--future-only", action="store_true", help="ابدأ من الغد بدلًا من اليوم الحالي")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days يجب أن يكون أكبر من صفر")
    raw = fetch_satair_data()
    final = process_data(raw, Path(args.cache), not args.future_only, args.days)
    output = Path(args.output)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    total = sum(len(c["matches"]) for c in final["data"])
    print(f"تم التحديث. الأيام: {', '.join(final['requested_dates'])} | البطولات: {len(final['data'])} | المباريات: {total}")


if __name__ == "__main__":
    main()
