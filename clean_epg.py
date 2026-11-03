import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import copy
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/epg2.xml.gz"
EPG_URL_2 = "https://iptvx.one/epg/epg.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s\.]', '', t)
    return t

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Автоматическое создание channels.txt если файл отсутствует
if not os.path.exists("channels.txt") or os.path.getsize("channels.txt") == 0:
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

playlist_channels = []
if os.path.exists("channels.txt"):
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and not name.startswith("#") and name not in playlist_channels:
                playlist_channels.append(name)

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")

# Шаг 1: Собираем все каналы из обоих источников в память
source_channels = {}

def parse_source_channels(file_path):
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if ch_id and ch_id not in source_channels:
                    display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                    has_icon = elem.find("icon") is not None
                    norm_names = [normalize(dn) for dn in display_names if dn]
                    source_channels[ch_id] = {
                        'elem': copy.deepcopy(elem),
                        'norm_names': norm_names,
                        'has_icon': has_icon
                    }
                elem.clear()

parse_source_channels("raw_epg1.xml.gz")
parse_source_channels("raw_epg2.xml.gz")

print(f"Всего найдено каналов в источниках: {len(source_channels)}")

new_root = ET.Element("tv")
mapped_source_to_target = {}
processed_targets = set()
channel_count = 0

# Шаг 2: Сопоставляем каналы плейлиста с источниками, отдавая приоритет тем, у которых есть пиконы
for ch_name in playlist_channels:
    norm_target = normalize(ch_name)
    target_ch_id = f"gen_{norm_target}"
    
    matching_source_ids = []
    best_source_elem = None
    has_icon_found = False
    
    for src_id, data in source_channels.items():
        matched = False
        for n_name in data['norm_names']:
            if n_name == norm_target or (len(n_name) >= 3 and (norm_target.startswith(n_name) or n_name.startswith(norm_target))):
                matched = True
                break
        if matched:
            matching_source_ids.append(src_id)
            # Выбираем карточку с иконкой в приоритете
            if data['has_icon'] and not has_icon_found:
                best_source_elem = data['elem']
                has_icon_found = True
            elif not best_source_elem:
                best_source_elem = data['elem']

    if target_ch_id not in processed_targets:
        processed_targets.add(target_ch_id)
        if best_source_elem is not None:
            final_ch_elem = copy.deepcopy(best_source_elem)
            final_ch_elem.set("id", target_ch_id)
            new_root.append(final_ch_elem)
        else:
            ch_elem = ET.Element("channel", id=target_ch_id)
            dn_elem = ET.SubElement(ch_elem, "display-name")
            dn_elem.text = ch_name
            new_root.append(ch_elem)
        channel_count += 1

    for src_id in matching_source_ids:
        mapped_source_to_target[src_id] = target_ch_id

print(f"Отобрано уникальных каналов для плейлиста: {channel_count}")

# Шаг 3: Собираем программы из обоих источников и привязываем к единому ID
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()

def parse_source_programmes(file_path):
    global programme_count
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                src_ch_ref = elem.get("channel")
                start = elem.get("start", "")
                
                if src_ch_ref in mapped_source_to_target and start:
                    if min_date <= start[:8] <= max_date:
                        target_ch_id = mapped_source_to_target[src_ch_ref]
                        prog_key = (target_ch_id, start)
                        
                        if prog_key not in added_programmes:
                            added_programmes.add(prog_key)
                            new_prog = copy.deepcopy(elem)
                            new_prog.set("channel", target_ch_id)
                            new_root.append(new_prog)
                            programme_count += 1
                    elem.clear()
                else:
                    elem.clear()

parse_source_programmes("raw_epg1.xml.gz")
parse_source_programmes("raw_epg2.xml.gz")

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
