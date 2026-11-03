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
    # Учитываем региональный сдвиг (+2, +4 и т.д.), чтобы регионы не смешивались
    shift_match = re.search(r'\+(\d+)', t)
    shift = shift_match.group(0) if shift_match else ""
    # Убираем приставки качества для объединения SD, HD и FHD в одну группу
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

# Читаем целевые каналы
target_channels = set()
if os.path.exists("channels.txt") and os.path.getsize("channels.txt") > 0:
    with open("channels.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                target_channels.add(line.lower())

print(f"Загружено целевых каналов для триммера: {len(target_channels)}")

print("Скачивание EPG источника...")
if download_file(EPG_URL, "raw_epg.xml.gz"):
    new_root = ET.Element("tv")
    kept_channel_ids = set()
    source_ch_to_group = {}
    group_to_kept_ids = {}
    channel_count = 0

    print("Фильтрация каналов и группировка версий (SD/HD/FHD)...")
    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                
                # Вычисляем группу для канала источника
                names_to_check = display_names + ([ch_id] if ch_id else [])
                g_key = ("", "")
                for name in names_to_check:
                    candidate_key = get_channel_group_key(name)
                    if candidate_key != ("", ""):
                        g_key = candidate_key
                        break
                
                if ch_id:
                    source_ch_to_group[ch_id] = g_key

                if ch_id in kept_channel_ids:
                    elem.clear()
                    continue
                
                matched = False
                if ch_id and ch_id.lower() in target_channels:
                    matched = True
                else:
                    for dn in display_names:
                        if dn and dn.lower() in target_channels:
                            matched = True
                            break
                
                if matched and ch_id:
                    kept_channel_ids.add(ch_id)
                    new_root.append(elem)
                    channel_count += 1
                    
                    if g_key != ("", ""):
                        if g_key not in group_to_kept_ids:
                            group_to_kept_ids[g_key] = set()
                        group_to_kept_ids[g_key].add(ch_id)
                else:
                    elem.clear()

    print(f"Отобрано каналов: {channel_count}")

    # Диапазон дат по Московскому времени (UTC+3): 2 дня до и 2 дня после
    now_msk = datetime.utcnow() + timedelta(hours=3)
    min_date = (now_msk - timedelta(days=2)).strftime("%Y%m%d")
    max_date = (now_msk + timedelta(days=2)).strftime("%Y%m%d")

    print("Фильтрация программ, дублирование на HD/FHD и сохранение...")
    programme_count = 0
    added_programmes = set()

    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                
                group_key = source_ch_to_group.get(ch_ref)
                target_ids = group_to_kept_ids.get(group_key) if group_key else None
                
                if target_ids and start:
                    if min_date <= start[:8] <= max_date:
                        # Дублируем программу на все каналы группы (SD, HD, FHD)
                        for target_ch_id in target_ids:
                            prog_key = (target_ch_id, start)
                            if prog_key not in added_programmes:
                                added_programmes.add(prog_key)
                                
                                prog_elem = ET.Element(elem.tag, elem.attrib)
                                prog_elem.set("channel", target_ch_id)
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
