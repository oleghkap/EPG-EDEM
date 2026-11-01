import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/edem.xml.gz"
EPG_URL_2 = "https://epg.one/epg2.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'\b(hd|sd|fhd|4k|ru|рф)\b', '', t)
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s]', '', t)
    return t

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Создаем файл, если его нет или если он пустой (0 байт)
if (not os.path.exists("channels.txt") or os.path.getsize("channels.txt") == 0) and PLAYLIST_URL:
    print("Файл channels.txt отсутствует или пуст. Заполняем из плейлиста...")
    try:
        req = urllib.request.Request(PLAYLIST_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            playlist_text = response.read().decode('utf-8', errors='ignore')
            
        extracted_channels = set()
        for line in playlist_text.splitlines():
            m_id = re.search(r'tvg-id="([^"]+)"', line)
            if m_id:
                extracted_channels.add(m_id.group(1).strip())
            
            m_name = re.search(r'tvg-name="([^"]+)"', line)
            if m_name:
                extracted_channels.add(m_name.group(1).strip())
            
            if "," in line and not line.startswith("#"):
                parts = line.strip().split(",")
                if len(parts) > 1:
                    extracted_channels.add(parts[-1].strip())
                    
        with open("channels.txt", "w", encoding="utf-8") as f:
            for ch in sorted(extracted_channels):
                f.write(ch + "\n")
        print(f"Успешно записано каналов: {len(extracted_channels)}")
    except Exception as e:
        print(f"Не удалось создать channels.txt: {e}")

# Читаем готовый список каналов
valid_ids = set()
valid_names_normalized = set()

if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            valid_ids.add(line.lower())
            valid_names_normalized.add(normalize(line))
    print(f"Загружено каналов для фильтрации: {len(valid_names_normalized)}")
else:
    print("Ошибка: файл channels.txt пуст или отсутствует!")

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")

new_root = ET.Element("tv")
kept_channel_ids = set()
channel_count = 0

def process_channels(file_path):
    global channel_count
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip().lower()
                if ch_id in kept_channel_ids:
                    elem.clear()
                    continue
                    
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                keep = False
                
                if ch_id and ch_id in valid_ids:
                    keep = True
                else:
                    for dn in display_names:
                        norm_dn = normalize(dn)
                        if norm_dn and any(norm_dn == vn or norm_dn in vn or vn in norm_dn for vn in valid_names_normalized):
                            keep = True
                            break
                
                if keep and elem.get("id"):
                    kept_channel_ids.add(elem.get("id"))
                    new_root.append(elem)
                    channel_count += 1
                else:
                    elem.clear()

process_channels("raw_epg1.xml.gz")
process_channels("raw_epg2.xml.gz")

print(f"Всего отобрано каналов: {channel_count}")

# Собираем программы (вчера + 3 дня вперед)
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()

def process_programmes(file_path):
    global programme_count
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                prog_key = (ch_ref, start)
                
                if ch_ref in kept_channel_ids and start and prog_key not in added_programmes:
                    if min_date <= start[:8] <= max_date:
                        added_programmes.add(prog_key)
                        new_root.append(elem)
                        programme_count += 1
                    else:
                    	elem.clear()
                else:
                    elem.clear()

process_programmes("raw_epg1.xml.gz")
process_programmes("raw_epg2.xml.gz")

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
