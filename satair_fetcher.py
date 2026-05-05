import requests
import json
from datetime import datetime

def fetch_satair_data():
    project_id = "satair-6983b"
    base_url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"
    
    collections = ["matchDays", "competitions", "teams", "channels", "standings"]
    results = {}
    
    for collection in collections:
        url = f"{base_url}/{collection}?pageSize=1000"
        try:
            response = requests.get(url)
            if response.status_code == 200:
                data = response.json()
                documents = data.get('documents', [])
                processed_docs = []
                for doc in documents:
                    doc_id = doc.get('name', '').split('/')[-1]
                    fields = doc.get('fields', {})
                    processed_fields = {"id": doc_id}
                    
                    def parse_value(v):
                        if 'stringValue' in v: return v['stringValue']
                        if 'booleanValue' in v: return v['booleanValue']
                        if 'integerValue' in v: return v['integerValue']
                        if 'arrayValue' in v:
                            return [parse_value(item) for item in v['arrayValue'].get('values', [])]
                        if 'mapValue' in v:
                            return {k: parse_value(val) for k, val in v['mapValue'].get('fields', {}).items()}
                        return None

                    for key, value in fields.items():
                        processed_fields[key] = parse_value(value)
                    processed_docs.append(processed_fields)
                results[collection] = processed_docs
        except Exception as e:
            print(f"Error fetching {collection}: {e}")
            
    return results

def process_data(raw_data):
    today = datetime.now().strftime("%Y-%m-%d")
    
    teams_map = {t['id']: t for t in raw_data.get('teams', [])}
    comps_map = {c['id']: c for c in raw_data.get('competitions', [])}
    channels_map = {ch['id']: ch for ch in raw_data.get('channels', [])}
    
    today_matches_raw = []
    for day in raw_data.get('matchDays', []):
        if day.get('date') == today:
            today_matches_raw = day.get('matches', [])
            break
    
    championships = {}
    for match in today_matches_raw:
        comp_id = match.get('competitionId')
        comp_info = comps_map.get(comp_id, {"name": "بطولة غير معروفة", "logo": ""})
        comp_name = comp_info.get('name')
        
        if comp_name not in championships:
            championships[comp_name] = {
                "name": comp_name,
                "logo": comp_info.get('logo'),
                "matches": [],
                "standings": []
            }
            
        # Get team info from teams_map or match direct fields
        home_team_id = match.get('homeTeamId')
        away_team_id = match.get('awayTeamId')
        
        home_info = teams_map.get(home_team_id, {})
        away_info = teams_map.get(away_team_id, {})
        
        home_name = home_info.get('name') or match.get('homeTeamName') or "Unknown"
        home_logo = home_info.get('logo') or ""
        
        away_name = away_info.get('name') or match.get('awayTeamName') or "Unknown"
        away_logo = away_info.get('logo') or ""
        
        match_channels = []
        for ch_id in match.get('channelIds', []):
            ch = channels_map.get(ch_id)
            if ch:
                match_channels.append({
                    "name": ch.get('displayName') or ch.get('name') or "قناة غير معروفة",
                    "logo": ch.get('logo') or "",
                    "satellite": ch.get('satellite') or ""
                })
        
        championships[comp_name]["matches"].append({
            "home": {"name": home_name, "logo": home_logo},
            "away": {"name": away_name, "logo": away_logo},
            "time": match.get('time'),
            "status": match.get('status', 'لم تبدأ'),
            "score": f"{match.get('homeScore', '-')}-{match.get('awayScore', '-')}",
            "channels": match_channels,
            "commentator": match.get('commentator') or "غير محدد"
        })

    for standing in raw_data.get('standings', []):
        comp_id = standing.get('competitionId')
        comp_info = comps_map.get(comp_id)
        if comp_info:
            comp_name = comp_info.get('name')
            if comp_name in championships:
                table = []
                for row in standing.get('table', []):
                    team_id = row.get('teamId')
                    team_info = teams_map.get(team_id, {})
                    table.append({
                        "rank": row.get('rank'),
                        "team": team_info.get('name') or "Unknown",
                        "logo": team_info.get('logo') or "",
                        "points": row.get('points'),
                        "played": row.get('played')
                    })
                championships[comp_name]["standings"] = table

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": list(championships.values())
    }

if __name__ == "__main__":
    raw = fetch_satair_data()
    if raw:
        final = process_data(raw)
        with open('matches.json', 'w', encoding='utf-8') as f:
            json.dump(final, f, ensure_ascii=False, indent=4)
        print("Data updated from SatAir source.")
