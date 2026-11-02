import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import copy
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/epg2.xml.gz"
EPG_URL_2 = "https://epg.one/ru2.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s\.]', '', t)
    return t

def shift_xmltv_time(time_str, hours_offset):
    match = re.match(r'(\d{14})(\s*[+\-]\d{4})?', time_str.strip())
    if not match:
        return time_str
    dt_part = match.group(1)
    tz_part = match.group(2) or ""
    try:
        dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S")
        dt_shifted = dt + timedelta(hours=hours_offset)
        return dt_shifted.strftime("%Y%m%d%H%M%S") + tz_part
    except Exception:
        return time_str

def parse_channel_info(ch_name):
    """Аккуратно извлекает сдвиг и очищает базовое имя, сохраняя цифры в названиях (например, '1' в Россия 1)"""
    lower_name = ch_name.lower()
    offset = 0
    
    match_shift = re.search(r'[\+\(]+\s*(\d+)\s*\)?$', lower_name)
    if not match_shift:
        match_shift = re.search(r'\+\s*(\d+)', lower_name)
    
    if match_shift:
        offset = int(match_shift.group(1))
        
    # Удаляем только хвостики со сдвигом и качество, не трогая полезные цифры в названии
    base_name = re.sub(r'[\+\(]?\s*\d+\s*\)?$', '', ch_name)
    base_name = re.sub(r'\+\s*\d+', '', base_name)
    base_name = re.sub(r'\b(hd|fhd|uhd)\b', '', base_name, flags=re.IGNORECASE)
    
    norm_base = normalize(base_name)
    return offset, norm_base

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Автоматическое создание channels.txt (один раз, если файла нет)
if not os.path.exists("channels.txt") or os.path.getsize("channels.txt") == 0:
    print(f"Проверка PLAYLIST_URL: {'Задана' if PLAYLIST_URL else 'ПУСТАЯ!'}")
    if PLAYLIST_URL:
        try:
            req = urllib.request.Request(PLAYLIST_URL, headers={'User-Agent': 'VLC/3.0.16'})
            with urllib.request.urlopen(req) as response:
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
                
                if line.startswith("#EXTINF"):
                    if "," in line:
                        parts = line.split(",")
                        ch_name = parts[-1].strip()
                        if ch_name:
                            extracted_channels.append(ch_name)
                        
            if len(extracted_channels) > 0:
                unique_channels = []
                seen = set()
                for ch in extracted_channels:
                    if ch not in seen:
                        seen.add(ch)
                        unique_channels.append(ch)

                with open("channels.txt", "w", encoding="utf-8") as f:
                    for ch in unique_channels:
                        f.write(ch + "\n")
                print(f"Успешно записано уникальных строк в channels.txt: {len(unique_channels)}")
                
        except Exception as e:
            print(f"Критическая ошибка при скачивании плейлиста: {e}")

print("Скачивание EPG источников (epg2 и ru2)...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")

all_channels_raw = {}
all_programmes_raw = {}

now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

def parse_raw_epg(file_path):
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if ch_id and ch_id not in all_channels_raw:
                    all_channels_raw[ch_id] = copy.deepcopy(elem)
                elem.clear()
            elif elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                if ch_ref and start and min_date <= start[:8] <= max_date:
                    if ch_ref not in all_programmes_raw:
                        all_programmes_raw[ch_ref] = []
                    all_programmes_raw[ch_ref].append(copy.deepcopy(elem))
                elem.clear()

parse_raw_epg("raw_epg1.xml.gz")
parse_raw_epg("raw_epg2.xml.gz")

print(f"Загружено уникальных каналов из источников: {len(all_channels_raw)}")
print(f"Загружено каналов с программами: {len(all_programmes_raw)}")

new_root = ET.Element("tv")
channel_names_cache = {}

for ch_id, elem in all_channels_raw.items():
    display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
    ch_name_first = display_names[0] if display_names else ch_id
    channel_names_cache[ch_id] = ch_name_first

playlist_channels = []
if os.path.exists("channels.txt"):
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and not name.startswith("#") and name not in playlist_channels:
                playlist_channels.append(name)

donor_map = {}
for ch_id, progs in all_programmes_raw.items():
    if progs:
        ch_name = channel_names_cache.get(ch_id, ch_id)
        donor_map[normalize(ch_name)] = ch_id

final_channels_added = set()
cloned_count = 0
native_count = 0

for ch_name in playlist_channels:
    offset, norm_base = parse_channel_info(ch_name)
    norm_name = normalize(ch_name)
    
    matched_ch_id = None
    for ch_id, raw_name in channel_names_cache.items():
        if normalize(raw_name) == norm_name:
            matched_ch_id = ch_id
            break
            
    has_native_progs = matched_ch_id and matched_ch_id in all_programmes_raw and len(all_programmes_raw[matched_ch_id]) > 0
    
    target_ch_id = matched_ch_id if matched_ch_id else f"gen_{norm_name}"
    if target_ch_id not in final_channels_added:
        final_channels_added.add(target_ch_id)
        if target_ch_id in all_channels_raw:
            new_root.append(copy.deepcopy(all_channels_raw[target_ch_id]))
        else:
            ch_elem = ET.Element("channel", id=target_ch_id)
            dn_elem = ET.SubElement(ch_elem, "display-name")
            dn_elem.text = ch_name
            new_root.append(ch_elem)

    progs_to_add = []
    
    # 1. Приоритет: если у канала есть своя родная программа — берем её
    if has_native_progs:
        for prog in all_programmes_raw[matched_ch_id]:
            new_prog = copy.deepcopy(prog)
            new_prog.set("channel", target_ch_id)
            progs_to_add.append(new_prog)
            native_count += 1
    else:
        # 2. Родной программы нет — ищем донора для сдвига или HD версии
        donor_id = None
        if norm_base in donor_map:
            donor_id = donor_map[norm_base]
        else:
            for b_norm, d_id in donor_map.items():
                if len(b_norm) >= 3 and (norm_base.startswith(b_norm) or b_norm.startswith(norm_base)):
                    donor_id = d_id
                    break
        
        if donor_id and donor_id in all_programmes_raw:
            for prog in all_programmes_raw[donor_id]:
                new_prog = copy.deepcopy(prog)
                new_prog.set("channel", target_ch_id)
                
                if offset > 0:
                    start_str = new_prog.get("start", "")
                    stop_str = new_prog.get("stop", "")
                    if start_str:
                        new_prog.set("start", shift_xmltv_time(start_str, offset))
                    if stop_str:
                        new_prog.set("stop", shift_xmltv_time(stop_str, offset))
                        
                progs_to_add.append(new_prog)
                cloned_count += 1

    for p in progs_to_add:
        new_root.append(p)

print(f"Добавлено родных программ: {native_count}")
print(f"Добавлено клонированных/сдвинутых программ: {cloned_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
