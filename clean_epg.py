import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
from datetime import datetime, timedelta

EPG_URL = "https://epg.one/edem.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

EXCLUDE_KEYWORDS = [
    "US |", "USA |", "США", "US:", "USA:",
    "UA |", "UKR |", "Украина", "Україна", "UA:",
    "BY |", "Беларусь", "Белоруссия", "BY:"
]

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'\b(hd|sd|fhd|4k|ru|рф)\b', '', t)
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s]', '', t)
    return t

print("Загрузка плейлиста...")
valid_ids = set()
valid_names_normalized = set()

if PLAYLIST_URL:
    try:
        req = urllib.request.Request(PLAYLIST_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            playlist_text = response.read().decode('utf-8', errors='ignore')
            
        for line in playlist_text.splitlines():
            if any(kw.lower() in line.lower() for kw in EXCLUDE_KEYWORDS):
                continue
            
            m_id = re.search(r'tvg-id="([^"]+)"', line)
            if m_id:
                valid_ids.add(m_id.group(1).strip().lower())
            
            m_name = re.search(r'tvg-name="([^"]+)"', line)
            if m_name:
                valid_names_normalized.add(normalize(m_name.group(1)))
            
            if "," in line and not line.startswith("#"):
                parts = line.strip().split(",")
                if len(parts) > 1:
                    valid_names_normalized.add(normalize(parts[-1]))
        print(f"Найдено в плейлисте: ID={len(valid_ids)}, имен={len(valid_names_normalized)}")
    except Exception as e:
        print(f"Ошибка загрузки плейлиста: {e}")

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

print("Потоковая фильтрация каналов...")
new_root = ET.Element("tv")
kept_channel_ids = set()
channel_count = 0

# 1. Отбираем только нужные каналы
with gzip.open("raw_epg.xml.gz", "rb") as f_in:
    for event, elem in ET.iterparse(f_in, events=("end",)):
        if elem.tag == "channel":
            ch_id = elem.get("id", "").strip().lower()
            display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
            full_str = f"{ch_id} " + " ".join(display_names)
            
            # Исключаем нежелательные страны
            if any(kw.lower() in full_str.lower() for kw in EXCLUDE_KEYWORDS):
                elem.clear()
                continue
            
            keep = True
            if valid_ids or valid_names_normalized:
                keep = False
                if ch_id and ch_id in valid_ids:
                    keep = True
                else:
                    for dn in display_names:
                        norm_dn = normalize(dn)
                        if norm_dn and any(norm_dn in vn or vn in norm_dn for vn in valid_names_normalized):
                            keep = True
                            break
            
            if keep and elem.get("id"):
                kept_channel_ids.add(elem.get("id"))
                new_root.append(elem)
                channel_count += 1
            else:
                elem.clear()

print(f"Отобрано ваших каналов (без США/UA/BY): {channel_count}")

# 2. Добавляем программы передач строго для этих каналов (на 3 дня вперед со всеми описаниями)
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
with gzip.open("raw_epg.xml.gz", "rb") as f_in:
    for event, elem in ET.iterparse(f_in, events=("end",)):
        if elem.tag == "programme":
            ch_ref = elem.get("channel")
            start = elem.get("start", "")
            if ch_ref in kept_channel_ids and start:
                if min_date <= start[:8] <= max_date:
                    new_root.append(elem)
                    programme_count += 1
                else:
                    elem.clear()
            else:
                elem.clear()

print(f"Добавлено передач с описаниями: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
