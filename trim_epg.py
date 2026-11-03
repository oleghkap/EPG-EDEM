import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import ssl
from datetime import datetime, timedelta

EPG_URL = "https://epg.one/epg2.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

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

def get_channel_group_key(text):
    if not text:
        return ("", "")
    t = text.lower()
    shift_match = re.search(r'\+(\d+)', t)
    shift = shift_match.group(0) if shift_match else ""
    # Очищаем от приставок качества для объединения в одну группу
    clean = re.sub(r'\b(hd|fhd|sd|4k|hevc|uhd)\b', '', t)
    norm = re.sub(r'[\(\)\[\]\|\:\,\-\+\s\.]', '', clean)
    return (norm, shift)

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

# Читаем целевые каналы из channels.txt
target_channels_raw = []
if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                target_channels_raw.append(line)

print(f"Загружено целевых каналов из channels.txt: {len(target_channels_raw)}")

print("Скачивание EPG источника...")
if download_file(EPG_URL, "raw_epg.xml.gz"):
    new_root = ET.Element("tv")
    
    # Шаг 1: Собираем каналы из первоисточника
    source_channels = {}       # ch_id -> element
    source_ch_group = {}       # ch_id -> group_key
    group_to_source_ids = {}   # group_key -> set of source ch_ids

    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if not ch_id:
                    elem.clear()
                    continue
                
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                names_to_check = display_names + [ch_id]
                
                g_key = ("", "")
                for name in names_to_check:
                    candidate_key = get_channel_group_key(name)
                    if candidate_key != ("", ""):
                        g_key = candidate_key
                        break
                
                source_channels[ch_id] = elem
                source_ch_group[ch_id] = g_key
                
                if g_key != ("", ""):
                    if g_key not in group_to_source_ids:
                        group_to_source_ids[g_key] = set()
                    group_to_source_ids[g_key].add(ch_id)

    # Шаг 2: Связываем каналы из channels.txt с группами
    group_to_output_ids = {}   # group_key -> set of output channel IDs/names to create
    
    for target in target_channels_raw:
        g_key = get_channel_group_key(target)
        if g_key != ("", "") and g_key in group_to_source_ids:
            if g_key not in group_to_output_ids:
                group_to_output_ids[g_key] = set()
            group_to_output_ids[g_key].add(target)

    # Шаг 3: Генерируем каналы для вывода (включая все HD/FHD варианты из плейлиста)
    channel_count = 0
    added_output_channels = set()

    for g_key, target_ids in group_to_output_ids.items():
        source_sample_id = list(group_to_source_ids[g_key])[0]
        sample_elem = source_channels[source_sample_id]
        
        for t_id in target_ids:
            if t_id in added_output_channels:
                continue
            added_output_channels.add(t_id)
            
            # Создаем тег канала для каждого варианта (HD, FHD и т.д.)
            ch_elem = ET.Element("channel", {"id": t_id})
            dn_elem = ET.SubElement(ch_elem, "display-name")
            dn_elem.text = t_id
            
            # Переносим иконку из источника
            for child in sample_elem:
                if child.tag == "icon":
                    ch_elem.append(child)
            
            new_root.append(ch_elem)
            channel_count += 1

    print(f"Отобрано и сгенерировано каналов (с учетом HD/FHD): {channel_count}")

    # Диапазон дат по Московскому времени (UTC+3): 2 дня до и 2 дня после
    now_msk = datetime.utcnow() + timedelta(hours=3)
    min_date = (now_msk - timedelta(days=2)).strftime("%Y%m%d")
    max_date = (now_msk + timedelta(days=2)).strftime("%Y%m%d")

    print("Фильтрация программ и дублирование на все варианты (HD/FHD)...")
    programme_count = 0
    added_programmes = set()

    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                
                g_key = source_ch_group.get(ch_ref)
                output_ids = group_to_output_ids.get(g_key) if g_key else None
                
                if output_ids and start:
                    if min_date <= start[:8] <= max_date:
                        # Дублируем программу на ВСЕ варианты канала (SD, HD, FHD) из группы
                        for out_ch_id in output_ids:
                            prog_key = (out_ch_id, start)
                            if prog_key not in added_programmes:
                                added_programmes.add(prog_key)
                                
                                prog_elem = ET.Element(elem.tag, elem.attrib)
                                prog_elem.set("channel", out_ch_id)
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
