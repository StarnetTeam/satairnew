import requests
import json
from datetime import datetime

def fetch_satair_data():
    """جلب جميع البيانات من Firestore (سكربت SatAir)."""
    project_id = "satair-6983b"
    base_url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"

    collections = ["matchDays", "competitions", "teams", "channels"]
    results = {}

    def parse_value(v):
        if v is None:
            return None
        if 'stringValue' in v: return v['stringValue']
        if 'booleanValue' in v: return v['booleanValue']
        if 'integerValue' in v: return v['integerValue']
        if 'arrayValue' in v:
            return [parse_value(item) for item in v['arrayValue'].get('values', [])]
        if 'mapValue' in v:
            return {k: parse_value(val) for k, val in v['mapValue'].get('fields', {}).items()}
        return None

    for collection in collections:
        url = f"{base_url}/{collection}"
        all_docs = []
        token = None
        try:
            while True:
                params = {"pageSize": 1000}
                if token:
                    params["pageToken"] = token
                response = requests.get(url, params=params)
                if response.status_code != 200:
                    print(f"Error fetching {collection}: HTTP {response.status_code}")
                    break
                data = response.json()
                documents = data.get('documents', [])
                for doc in documents:
                    doc_id = doc.get('name', '').split('/')[-1]
                    fields = doc.get('fields', {})
                    processed_fields = {"id": doc_id}
                    for key, value in fields.items():
                        processed_fields[key] = parse_value(value)
                    all_docs.append(processed_fields)
                token = data.get('nextPageToken')
                if not token:
                    break
            results[collection] = all_docs
        except Exception as e:
            print(f"Error fetching {collection}: {e}")

    return results


def normalize_match(match):
    """توحيد حقول المباراة من البنية المسطحة الحالية إلى البنية القياسية."""
    return {
        "id": match.get('id'),
        "competition": match.get('competition'),
        "competitionLogo": match.get('competitionLogo'),
        "competitionStage": match.get('competitionStage'),
        "home": {
            "name": match.get('team1'),
            "logo": match.get('logo1'),
        },
        "away": {
            "name": match.get('team2'),
            "logo": match.get('logo2'),
        },
        "time": match.get('time'),
        "status": match.get('status'),
        "stadium": match.get('stadium'),
        "broadcastOn": match.get('broadcastOn'),
        "live": match.get('live', False),
        "channelIds": match.get('channelIds', []),
        "commentator": match.get('commentator'),
        "commentatorAudio": match.get('commentatorAudio'),
        "commentatorImage": match.get('commentatorImage'),
    }


def process_data(raw_data):
    """معالجة البيانات الخام (بنية matchDays المسطحة الحالية + المتداخلة القديمة)."""
    today = datetime.now().strftime("%Y-%m-%d")

    channels_map = {ch['id']: ch for ch in raw_data.get('channels', [])}

    matches_today = []

    # النمط الأول: مباريات مسطحة مباشرة في مستندات matchDays
    for day in raw_data.get('matchDays', []):
        if day.get('date') == today and 'competition' in day:
            matches_today.append(normalize_match(day))

    # النمط الثاني: مباريات متداخلة داخل مصفوفة matches (بنية قديمة/بديلة)
    for day in raw_data.get('matchDays', []):
        if day.get('date') == today:
            for match in day.get('matches', []) or []:
                matches_today.append(normalize_match(match))

    # تجميع المباريات حسب البطولة
    championships = {}
    for match in matches_today:
        comp_name = (match.get('competition') or "").strip()
        if not comp_name:
            comp_name = "بطولة غير معروفة"

        if comp_name not in championships:
            championships[comp_name] = {
                "name": comp_name,
                "logo": match.get('competitionLogo') or "",
                "stage": match.get('competitionStage'),
                "matches": [],
            }

        match_channels = []
        seen_channels = set()
        for ch_id in match.get('channelIds', []) or []:
            if ch_id in seen_channels:
                continue
            seen_channels.add(ch_id)
            ch = channels_map.get(ch_id)
            if ch:
                match_channels.append({
                    "name": ch.get('displayName') or ch.get('name') or "قناة غير معروفة",
                    "logo": ch.get('logo') or "",
                    "satellite": ch.get('satellite') or "",
                })

        status = match.get('status') or "لم تبدأ"
        score = match.get('score') or "---"

        championships[comp_name]["matches"].append({
            "home": {
                "name": match.get('home', {}).get('name') or "Unknown",
                "logo": match.get('home', {}).get('logo') or "",
            },
            "away": {
                "name": match.get('away', {}).get('name') or "Unknown",
                "logo": match.get('away', {}).get('logo') or "",
            },
            "time": match.get('time'),
            "status": status,
            "score": score,
            "channels": match_channels,
            "commentator": match.get('commentator') or "غير محدد",
        })

    # ترتيب البطولات بعدد المباريات تنازلياً
    sorted_champs = sorted(
        championships.values(),
        key=lambda c: len(c['matches']),
        reverse=True
    )

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": sorted_champs
    }


if __name__ == "__main__":
    raw = fetch_satair_data()
    if raw:
        final = process_data(raw)
        with open('matches.json', 'w', encoding='utf-8') as f:
            json.dump(final, f, ensure_ascii=False, indent=4)
        total_matches = sum(len(c['matches']) for c in final['data'])
        print(f"Data updated from SatAir source. Championships: {len(final['data'])}, Matches: {total_matches}")
