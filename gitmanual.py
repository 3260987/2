import json
import os
import re
import time
import feedparser
import requests

STATE_FILE = "gitmanual_seen.json"
DOWNLOAD_DIR = "HEBREW"  # עודכן לתיקיית HEBREW התואמת

ALLOWED_COUNTS = (5, 10, 15, 20, 30, 40, 100)
DEFAULT_COUNT = 5


def get_episodes_per_run():
    value = os.environ.get("EPISODES_PER_RUN", "").strip()
    if value.isdigit() and int(value) in ALLOWED_COUNTS:
        return int(value)
    if value:
        print(f"ערך לא תקין ב-EPISODES_PER_RUN: {value}. משתמש בברירת המחדל ({DEFAULT_COUNT}).")
    return DEFAULT_COUNT


EPISODES_PER_RUN = get_episodes_per_run()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def sanitize_filename(name):
    cleaned = name.replace("[", "'").replace("]", "'").replace(":", "")
    return re.sub(r'[\\/*?:"<>|]', "", cleaned).strip() or "podcast_episode"

def download_podcast(url, filename, folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for attempt in range(1, 4):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            f.write(chunk)
            return path
        except Exception as e:
            print(f"ניסיון {attempt} נכשל בהורדת {filename}: {e}")
            if attempt == 3:
                raise e
            time.sleep(5)

def main():
    rss_url = os.environ.get("RSS_URL")
    if not rss_url:
        print("לא הוגדרה כתובת RSS.")
        return

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    state = load_state()
    print(f"בודק את הפיד: {rss_url}")
    print(f"כמות פרקים להורדה בהרצה זו: {EPISODES_PER_RUN}")
    
    parsed = feedparser.parse(rss_url)
    if not parsed.entries:
        print("לא נמצאו פרקים בפיד.")
        return

    feed_title = parsed.feed.get("title", "Podcast")
    
    words = feed_title.strip().split()[:2]
    slug = "-".join(words).lower()
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    if not slug:
        slug = "podcast"
        
    with open("env_output.env", "w", encoding="utf-8") as env_file:
        env_file.write(f"PODCAST_SLUG={slug}\n")

    seen = set(state.get(rss_url, []))
    
    entries = parsed.entries
    ids = [e.get("id") or e.get("link") for e in entries]

    if rss_url not in state:
        state[rss_url] = []
        seen = set()

    downloaded_count = 0
    for entry, eid in reversed(list(zip(entries, ids))):
        if downloaded_count >= EPISODES_PER_RUN:
            break

        if eid not in seen:
            title = entry.get("title", "New episode")
            enclosures = entry.get("enclosures", [])
            
            if not enclosures:
                continue

            audio_url = enclosures[0].get("href")
            safe_name = sanitize_filename(f"{feed_title} - {title}")
            filename = f"{safe_name}.mp3"

            print(f"מוריד: {filename}")
            try:
                download_podcast(audio_url, filename, DOWNLOAD_DIR)
                seen.add(eid)
                downloaded_count += 1
            except Exception as e:
                print(f"שגיאה בהורדת {filename}: {e}")

    state[rss_url] = list(seen)
    save_state(state)

if __name__ == "__main__":
    main()
