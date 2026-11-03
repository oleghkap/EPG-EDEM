import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import ssl
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/epg2.xml.gz"
EPG_URL_2 = "https://iptvx.one/epg/epg.xml.gz"
EPG_URL_3 = "https://epg.one/pp.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s\.]', '', t)
    return t

def get_base_name(text):
    if not text:
        return ""
    t = text.lower()
    # Убираем суффиксы качества и регионов для точного родственного сопоставления
    t = re.sub(r'\b(hd|fhd|sd|4k|hevc|uhd|\+\d+)\b', '', t)
    return normalize(t)

def download_file(url, dest):
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req, context=ctx) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Автоматическое создание channels.txt если отсутствует
if not os.path.exists("channels.txt") or os.path.getsize("channels.txt") == 0:
    if PLAYLIST_URL:
        try:
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(PLAYLIST_URL, headers={'User-Agent': 'VLC/3.0.16'})
            with urllib.request.urlopen(req, context=ctx) as response:
                playlist_text = response.read().decode('utf-8', errors='ignore')
            
            extracted_channels = []
            for line in playlist_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m_id = re.search(r'tvg-id="([^"]+)"', line, re.IGNORECASE)
                if m_id and m_id.group(1).strip():
                    extracted_channels.append(m_id.group(1).strip())
                m_name = re.search(r'tvg-name="([^"]+)"', line, re.IGNORECASE)
                if m_name and m_name.group(1).strip():
                    extracted_channels.append(m_name.group(1).strip())
                if line.startswith("#EXTINF") and "," in line:
                    ch_name = line.split(",")[-1].strip()
                    if ch_name:
                        extracted_channels.append(ch_name)
                        
            if extracted_channels:
                unique_channels = list(dict.fromkeys(extracted_channels))
                with open("channels.txt", "w", encoding="utf-8") as f:
                    for ch in unique_channels:
                        f.write(ch + "\n")
        except Exception as e:
            print(f"Ошибка плейлиста: {e}")

# Читаем целевые каналы из плейлиста
target_channels = []
if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                target_channels.append(line)

print(f"Загружено целевых каналов из плейлиста: {len(target_channels)}")

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")
download_file(EPG_URL_3, "raw_epg3.xml.gz")

# Индексируем каналы каждого источника (точное имя и базовое имя)
def index_channels(file_path):
    exact_map = {}
    base_map = {}
    if not os.path.exists(file_path):
        return exact_map, base_map
    
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                names_to_check = display_names + ([ch_id] if ch_id else [])
                for name in names_to_check:
                    norm = normalize(name)
                    base = get_base_name(name)
                    if norm and norm not in exact_map:
                        exact_map[norm] = (elem, ch_id)
                    if base and base not in base_map:
                        base_map[base] = (elem, ch_id)
    return exact_map, base_map

print("Индексация каналов из источников...")
sources_exact = {}
sources_base = {}
sources_exact[1], sources_base[1] = index_channels("raw_epg1.xml.gz")
sources_exact[2], sources_base[2] = index_channels("raw_epg2.xml.gz")
sources_exact[3], sources_base[3] = index_channels("raw_epg3.xml.gz")

new_root = ET.Element("tv")
assigned_channels_per_source = {1: set(), 2: set(), 3: set()}
channel_count = 0
resolved_mapping = []

# Строгое сопоставление с приоритетом 1 -> 2 -> 3
for target in target_channels:
    t_norm = normalize(target)
    t_base = get_base_name(target)
    
    found = False
    for s_id in [1, 2, 3]:
        if t_norm in sources_exact[s_id]:
            elem, ch_id = sources_exact[s_id][t_norm]
            resolved_mapping.append((target, s_id, elem, ch_id))
            assigned_channels_per_source[s_id].add(ch_id)
            found = True
            break
        elif t_base in sources_base[s_id]:
            elem, ch_id = sources_base[s_id][t_base]
            resolved_mapping.append((target, s_id, elem, ch_id))
            assigned_channels_per_source[s_id].add(ch_id)
            found = True
            break
    if found:
        channel_count += 1

print(f"Успешно сопоставлено каналов: {channel_count}")

# Добавляем элементы каналов в результирующее дерево
for target_name, s_id, elem, ch_id in resolved_mapping:
    new_root.append(elem)

# Диапазон дат для программ (вчера + 3 дня вперед)
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()

def process_programmes(file_path, allowed_channel_ids):
    global programme_count
    if not os.path.exists(file_path) or not allowed_channel_ids:
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                prog_key = (ch_ref, start)
                
                if ch_ref in allowed_channel_ids and start and prog_key not in added_programmes:
                    if min_date <= start[:8] <= max_date:
                        added_programmes.add(prog_key)
                        new_root.append(elem)
                        programme_count += 1
                    else:
                        elem.clear()
                else:
                    elem.clear()

print("Сбор программ передач...")
process_programmes("raw_epg1.xml.gz", assigned_channels_per_source[1])
process_programmes("raw_epg2.xml.gz", assigned_channels_per_source[2])
process_programmes("raw_epg3.xml.gz", assigned_channels_per_source[3])

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
