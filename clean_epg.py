import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
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

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Автоматическое создание channels.txt если отсутствует
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

# Читаем шаблоны каналов
valid_identifiers = set()
valid_names_normalized = set()

if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            valid_identifiers.add(line.lower())
            valid_identifiers.add(line)
            valid_names_normalized.add(normalize(line))

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")
download_file(EPG_URL_3, "raw_epg3.xml.gz")

new_root = ET.Element("tv")
covered_target_channels = set()  # Успешно заполненные каналы из наших шаблонов
kept_channel_ids_source1 = set()
kept_channel_ids_source2 = set()
kept_channel_ids_source3 = set()
channel_count = 0

def match_channel(ch_id, display_names):
    if ch_id and (ch_id.lower() in valid_identifiers or ch_id in valid_identifiers):
        return True
    for dn in display_names:
        norm_dn = normalize(dn)
        if not norm_dn:
            continue
        if dn in valid_identifiers or norm_dn in valid_names_normalized:
            return True
        for vn in valid_names_normalized:
            if len(vn) >= 3 and (norm_dn.startswith(vn) or vn.startswith(norm_dn)):
                return True
    return False

def get_matched_target_key(ch_id, display_names):
    if ch_id and ch_id.lower() in valid_identifiers:
        return ch_id.lower()
    for dn in display_names:
        norm_dn = normalize(dn)
        if norm_dn in valid_names_normalized:
            return norm_dn
    return None

# --- ЭТАП 1: ПЕРВЫЙ источник (Основной) ---
print("Обработка источника №1 (epg2)...")
if os.path.exists("raw_epg1.xml.gz"):
    with gzip.open("raw_epg1.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                
                if match_channel(ch_id, display_names):
                    target_key = get_matched_target_key(ch_id, display_names)
                    if target_key and target_key not in covered_target_channels:
                        covered_target_channels.add(target_key)
                        kept_channel_ids_source1.add(ch_id)
                        new_root.append(elem)
                        channel_count += 1
                        continue
                elem.clear()

# --- ЭТАП 2: ВТОРОЙ источник (Дополнение) ---
print("Обработка источника №2 (iptvx) для добора недостающего...")
if os.path.exists("raw_epg2.xml.gz"):
    with gzip.open("raw_epg2.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                
                if match_channel(ch_id, display_names):
                    target_key = get_matched_target_key(ch_id, display_names)
                    if target_key and target_key not in covered_target_channels:
                        covered_target_channels.add(target_key)
                        kept_channel_ids_source2.add(ch_id)
                        new_root.append(elem)
                        channel_count += 1
                        continue
                elem.clear()

# --- ЭТАП 3: ТРЕТИЙ источник (Финальный резерв) ---
print("Обработка источника №3 (pp) для финального добора...")
if os.path.exists("raw_epg3.xml.gz"):
    with gzip.open("raw_epg3.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                
                if match_channel(ch_id, display_names):
                    target_key = get_matched_target_key(ch_id, display_names)
                    if target_key and target_key not in covered_target_channels:
                        covered_target_channels.add(target_key)
                        kept_channel_ids_source3.add(ch_id)
                        new_root.append(elem)
                        channel_count += 1
                        continue
                elem.clear()

print(f"Всего отобрано уникальных каналов (1 + 2 + 3): {channel_count}")

# Диапазон дат: вчера + 3 дня вперед
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()

# --- ЭТАП 4: Сбор программ для каналов из всех источников ---
print("Сбор программ передач...")
def process_programmes(file_path, allowed_channel_ids):
    global programme_count
    if not os.path.exists(file_path):
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

process_programmes("raw_epg1.xml.gz", kept_channel_ids_source1)
process_programmes("raw_epg2.xml.gz", kept_channel_ids_source2)
process_programmes("raw_epg3.xml.gz", kept_channel_ids_source3)

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
