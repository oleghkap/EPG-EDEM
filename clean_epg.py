import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import ssl
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/epg2.xml.gz"  # Основной источник
EPG_URL_2 = "https://epg.one/pp.xml.gz"    # Вспомогательный источник
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
    # Извлекаем смещение времени (+2, +4 и т.д.), чтобы регионы не путались
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

# Читаем целевые каналы из channels.txt (только по названию)
target_channels_sigs = {}
if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                sig = get_channel_signature(line)
                if sig[0]:
                    target_channels_sigs[sig] = line

print(f"Загружено целевых каналов из channels.txt: {len(target_channels_sigs)}")

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")

def get_source_channels_map(file_path):
    channels_map = {} # sig -> list of (elem, ch_id)
    if not os.path.exists(file_path):
        return channels_map
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                for name in display_names + ([ch_id] if ch_id else []):
                    sig = get_channel_signature(name)
                    if sig[0] and sig in target_channels_sigs:
                        if sig not in channels_map:
                            channels_map[sig] = []
                        channels_map[sig].append((elem, ch_id))
    return channels_map

print("Индексация каналов из источников...")
s1_channels = get_source_channels_map("raw_epg1.xml.gz")
s2_channels = get_source_channels_map("raw_epg2.xml.gz")

new_root = ET.Element("tv")
added_sigs = set()
sig_to_output_ch_id = {}
source1_mapping = {}
source2_mapping = {}
channel_count = 0

# 1. Обрабатываем Основной источник (1)
for sig, items in s1_channels.items():
    if sig in target_channels_sigs and sig not in added_sigs:
        added_sigs.add(sig)
        elem, ch_id = items[0]
        sig_to_output_ch_id[sig] = ch_id
        new_root.append(elem)
        channel_count += 1
        
    for _, src_ch_id in items:
        if sig in sig_to_output_ch_id:
            source1_mapping[src_ch_id] = sig_to_output_ch_id[sig]

# 2. Обрабатываем Вспомогательный источник (2) для каналов, которых не было в первом
for sig, items in s2_channels.items():
    if sig in target_channels_sigs and sig not in added_sigs:
        added_sigs.add(sig)
        elem, ch_id = items[0]
        sig_to_output_ch_id[sig] = ch_id
        new_root.append(elem)
        channel_count += 1
        
    for _, src_ch_id in items:
        if sig in sig_to_output_ch_id:
            source2_mapping[src_ch_id] = sig_to_output_ch_id[sig]

print(f"Успешно сопоставлено уникальных каналов: {channel_count}")

# Расчет дат по Московскому времени (UTC+3): 2 дня до и 2 дня после
now_msk = datetime.utcnow() + timedelta(hours=3)
min_date = (now_msk - timedelta(days=2)).strftime("%Y%m%d")
max_date = (now_msk + timedelta(days=2)).strftime("%Y%m%d")

programme_count = 0
added_programme_slots = set() # Защита от дублей: (target_ch_id, start_time)

def process_programmes(file_path, mapping_dict):
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

print("Сбор программ из основного источника...")
process_programmes("raw_epg1.xml.gz", source1_mapping)

print("Сбор недостающих программ из вспомогательного источника...")
process_programmes("raw_epg2.xml.gz", source2_mapping)

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
