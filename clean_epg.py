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
    t = re.sub(r'[\(\)\[\]\|\:\,\s\.]', '', t)
    return t

def get_channel_signature(text):
    if not text:
        return "", ""
    t = text.lower()
    # Извлекаем смещение времени (+2, +4, +6 и т.д.), чтобы регионы не путались с базовыми каналами
    shift_match = re.search(r'\+(\d+)', t)
    shift = shift_match.group(0) if shift_match else ""
    
    # Убираем технические приставки качества
    clean = re.sub(r'\b(hd|fhd|sd|4k|hevc|uhd)\b', '', t)
    norm = normalize(clean)
    return norm, shift

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

def parse_source(file_path):
    channels_map = {} # (norm_name, shift) -> (elem, ch_id)
    if not os.path.exists(file_path):
        return channels_map
    
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                
                names_to_check = display_names + ([ch_id] if ch_id else [])
                for name in names_to_check:
                    norm, shift = get_channel_signature(name)
                    if norm:
                        key = (norm, shift)
                        if key not in channels_map:
                            channels_map[key] = (elem, ch_id)
    return channels_map

print("Интексация каналов из источников...")
sources_channels = {
    1: parse_source("raw_epg1.xml.gz"),
    2: parse_source("raw_epg2.xml.gz"),
    3: parse_source("raw_epg3.xml.gz")
}

new_root = ET.Element("tv")
added_output_channel_ids = set()
source_program_mappings = {1: {}, 2: {}, 3: {}}
channel_count = 0

for target in target_channels:
    t_norm, t_shift = get_channel_signature(target)
    
    chosen_elem = None
    chosen_ch_id = None
    
    # 1. Ищем лучший канал (иконку/метаданные) с приоритетом 1 -> 2 -> 3
    for s_id in [1, 2, 3]:
        s_map = sources_channels[s_id]
        key = (t_norm, t_shift)
        if key in s_map:
            chosen_elem, chosen_ch_id = s_map[key]
            break
            
    if chosen_elem is not None and chosen_ch_id not in added_output_channel_ids:
        added_output_channel_ids.add(chosen_ch_id)
        new_root.append(chosen_elem)
        channel_count += 1
        
        # 2. Регистрируем маппинг программ для ВСЕХ источников, у которых есть этот канал
        for s_id in [1, 2, 3]:
            s_map = sources_channels[s_id]
            key = (t_norm, t_shift)
            if key in s_map:
                _, src_ch_id = s_map[key]
                source_program_mappings[s_id][src_ch_id] = chosen_ch_id

print(f"Успешно сопоставлено уникальных каналов: {channel_count}")

# Расчет дат по Московскому времени (UTC+3) во избежание сдвигов на сервере GitHub
now_msk = datetime.utcnow() + timedelta(hours=3)
min_date = (now_msk - timedelta(days=2)).strftime("%Y%m%d")
max_date = (now_msk + timedelta(days=5)).strftime("%Y%m%d")

programme_count = 0
added_programme_slots = set() # Защита от дублей: (target_ch_id, start_time)

def process_programmes_for_source(file_path, mapping_dict):
    global programme_count
    if not os.path.exists(file_path) or not mapping_dict:
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                if ch_ref in mapping_dict:
                    target_ch_id = mapping_dict[ch_ref]
                    start = elem.get("start", "")
                    slot_key = (target_ch_id, start)
                    
                    if start and slot_key not in added_programme_slots:
                        if min_date <= start[:8] <= max_date:
                            elem.set("channel", target_ch_id)
                            added_programme_slots.add(slot_key)
                            new_root.append(elem)
                            programme_count += 1
                        else:
                            elem.clear()
                    else:
                        elem.clear()
                else:
                    elem.clear()

print("Сбор и объединение программ со всех источников...")
# Обрабатываем источники по порядку: 1 -> 2 -> 3
for s_id in [1, 2, 3]:
    process_programmes_for_source(f"raw_epg{s_id}.xml.gz", source_program_mappings[s_id])

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
