import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import ssl
from datetime import datetime, timedelta

EPG_URL = "https://epg.one/epg2.xml.gz"
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
    # Учитываем региональный сдвиг (+2, +4 и т.д.), чтобы регионы не смешивались
    shift_match = re.search(r'\+(\d+)', t)
    shift = shift_match.group(0) if shift_match else ""
    # Убираем технические приставки качества для группировки SD/HD/FHD
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

# Читаем целевые каналы из channels.txt и группируем по сигнатурам (для поддержки HD/FHD)
target_channels_map = {} # sig -> list of target channel names from channels.txt
if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                sig = get_channel_signature(line)
                if sig[0]:
                    if sig not in target_channels_map:
                        target_channels_map[sig] = []
                    target_channels_map[sig].append(line)

print(f"Загружено целевых каналов из channels.txt: {sum(len(v) for v in target_channels_map.values())}")

print("Скачивание EPG источника...")
if download_file(EPG_URL, "raw_epg.xml.gz"):
    new_root = ET.Element("tv")
    
    # Шаг 1: Индексируем каналы источника по сигнатурам
    source_channels = {}  # sig -> list of (elem, ch_id)
    source_ch_to_sig = {} # src_ch_id -> sig
    
    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if not ch_id:
                    elem.clear()
                    continue
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                for name in display_names + [ch_id]:
                    sig = get_channel_signature(name)
                    if sig[0] and sig in target_channels_map:
                        if sig not in source_channels:
                            source_channels[sig] = []
                        source_channels[sig].append((elem, ch_id))
                        source_ch_to_sig[ch_id] = sig
                        break
                else:
                    elem.clear()

    # Шаг 2: Создаем каналы в выходном XML для всех вариантов из channels.txt (включая HD/FHD)
    channel_count = 0
    sig_to_target_ids = {} # sig -> list of target channel names to generate programs for
    
    for sig, target_names in target_channels_map.items():
        if sig in source_channels:
            sample_elem, _ = source_channels[sig][0]
            for t_name in target_names:
                if sig not in sig_to_target_ids:
                    sig_to_target_ids[sig] = []
                sig_to_target_ids[sig].append(t_name)
                
                # Создаем элемент канала для каждого варианта (включая HD/FHD)
                ch_elem = ET.Element("channel", {"id": t_name})
                dn_elem = ET.SubElement(ch_elem, "display-name")
                dn_elem.text = t_name
                
                # Переносим иконку из источника
                for child in sample_elem:
                    if child.tag == "icon":
                        ch_elem.append(child)
                        
                new_root.append(ch_elem)
                channel_count += 1

    print(f"Отобрано и сгенерировано каналов (с учетом дублирования HD/FHD): {channel_count}")

    # Шаг 3: Маппинг исходных ID каналов на списки целевых имен каналов для дублирования программ
    source_ch_to_targets = {}
    for src_ch_id, sig in source_ch_to_sig.items():
        if sig in sig_to_target_ids:
            source_ch_to_targets[src_ch_id] = sig_to_target_ids[sig]

    # Диапазон дат по Московскому времени (UTC+3): 2 дня до и 2 дня после
    now_msk = datetime.utcnow() + timedelta(hours=3)
    min_date = (now_msk - timedelta(days=2)).strftime("%Y%m%d")
    max_date = (now_msk + timedelta(days=2)).strftime("%Y%m%d")

    print("Фильтрация программ и дублирование на HD/FHD...")
    programme_count = 0
    added_programmes = set()

    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                
                target_ids = source_ch_to_targets.get(ch_ref)
                if target_ids and start:
                    if min_date <= start[:8] <= max_date:
                        # Дублируем программу на все варианты канала (обычный, HD, FHD)
                        for t_id in target_ids:
                            prog_key = (t_id, start)
                            if prog_key not in added_programmes:
                                added_programmes.add(prog_key)
                                
                                prog_elem = ET.Element(elem.tag, elem.attrib)
                                prog_elem.set("channel", t_id)
                                for child in elem:
                                    prog_elem.append(child)
                                    
                                new_root.append(prog_elem)
                                programme_count += 1
                        elem.clear()
                    else:
                        elem.clear()
                else:
                    elem.clear()

    print(f"Всего передач сохранено (с учетом дублирования): {programme_count}")

    print("Сохранение обрезанного EPG...")
    tree = ET.ElementTree(new_root)
    with gzip.open("epg_trimmed.xml.gz", "wb") as f_out:
        tree.write(f_out, encoding="utf-8", xml_declaration=True)
    print("Готово!")
else:
    print("Не удалось скачать исходный файл.")
