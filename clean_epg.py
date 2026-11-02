import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import copy
from datetime import datetime, timedelta

EPG_URL_1 = "https://epg.one/edem.xml.gz"
EPG_URL_2 = "https://epg.one/epg2.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'[\(\)\[\]\|\:\,\-\+\s\.]', '', t)
    return t

def shift_xmltv_time(time_str, hours_offset):
    # Сдвигает время в формате XMLTV (например, 20261102153000 +0300) на заданное число часов
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

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

# Автоматическое создание channels.txt (один раз)
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

# Читаем список каналов
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
    print(f"Загружено шаблонов для фильтрации: {len(valid_names_normalized)}")
else:
    print("Ошибка: файл channels.txt пуст или отсутствует!")

print("Скачивание EPG источников...")
download_file(EPG_URL_1, "raw_epg1.xml.gz")
download_file(EPG_URL_2, "raw_epg2.xml.gz")

new_root = ET.Element("tv")
kept_channel_ids = set()
channel_id_to_name = {} # Карта: ID канала в EPG -> его название
channel_count = 0

def process_channels(file_path):
    global channel_count
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if ch_id in kept_channel_ids:
                    elem.clear()
                    continue
                    
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                keep = False
                matched_name = ""
                
                if ch_id and (ch_id.lower() in valid_identifiers or ch_id in valid_identifiers):
                    keep = True
                    matched_name = ch_id
                else:
                    for dn in display_names:
                        norm_dn = normalize(dn)
                        if dn in valid_identifiers or norm_dn in valid_names_normalized:
                            keep = True
                            matched_name = dn
                            break
                        
                        matched = False
                        for vn in valid_names_normalized:
                            if len(vn) >= 3 and (norm_dn.startswith(vn) or vn.startswith(norm_dn)):
                                keep = True
                                matched_name = dn
                                matched = True
                                break
                        if matched:
                            break
                
                if keep and ch_id:
                    kept_channel_ids.add(ch_id)
                    if display_names:
                        channel_id_to_name[ch_id] = display_names[0]
                    new_root.append(elem)
                    channel_count += 1
                else:
                    elem.clear()

process_channels("raw_epg1.xml.gz")
process_channels("raw_epg2.xml.gz")

print(f"Всего отобрано каналов: {channel_count}")

# Собираем программы и запоминаем, у каких каналов они есть
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()
channels_with_programmes = set()
raw_programmes_by_channel = {} # Для последующего клонирования

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
                        channels_with_programmes.add(ch_ref)
                        
                        # Сохраняем для возможного клонирования пустых каналов
                        if ch_ref not in raw_programmes_by_channel:
                            raw_programmes_by_channel[ch_ref] = []
                        raw_programmes_by_channel[ch_ref].append(copy.deepcopy(elem))
                        
                        new_root.append(elem)
                        programme_count += 1
                    else:
                        elem.clear()
                else:
                    elem.clear()

process_programmes("raw_epg1.xml.gz")
process_programmes("raw_epg2.xml.gz")

print(f"Всего добавлено реальных передач: {programme_count}")

# АВТОМАТИЧЕСКОЕ КЛОНИРОВАНИЕ ДЛЯ КАНАЛОВ БЕЗ ПРОГРАММ (HD, FHD, сдвиги времени)
print("Запуск клонирования программы для каналов без расписания...")
cloned_count = 0

# Создаем индекс доступных базовых каналов по их нормализованному имени
base_channel_map = {} # norm_name -> ch_id
for ch_id in channels_with_programmes:
    name = channel_id_to_name.get(ch_id, ch_id)
    base_channel_map[normalize(name)] = ch_id

for ch_id in kept_channel_ids:
    if ch_id not in channels_with_programmes:
        ch_name = channel_id_to_name.get(ch_id, ch_id)
        norm_name = normalize(ch_name)
        
        # Определяем базовое имя и сдвиг времени (+2, +4 и т.д.)
        offset = 0
        base_norm = norm_name
        
        # Проверяем наличие суффиксов сдвига в имени
        match_shift = re.search(r'[\+\s](\d+)$', ch_name.lower())
        if match_shift:
            offset = int(match_shift.group(1))
            # Убираем сдвиг из имени для поиска базового канала (например, "первыйканал+2" -> "первыйканал")
            base_norm = re.sub(r'[\+\s]\d+$', '', norm_name)
        
        # Также убираем hd/fhd для поиска базы
        base_norm = base_norm.replace('hd', '').replace('fhd', '')
        
        # Ищем подходящий канал-донор с программой
        donor_ch_id = None
        if base_norm in base_channel_map:
            donor_ch_id = base_channel_map[base_norm]
        else:
            # Ищем частичное совпадение среди доноров
            for b_norm, d_id in base_channel_map.items():
                if len(b_norm) >= 3 and (base_norm.startswith(b_norm) or b_norm.startswith(base_norm)):
                    donor_ch_id = d_id
                    break
        
        # Если донор найден — клонируем его программы с учетом сдвига
        if donor_ch_id and donor_ch_id in raw_programmes_by_channel:
            for prog_elem in raw_programmes_by_channel[donor_ch_id]:
                new_prog = copy.deepcopy(prog_elem)
                new_prog.set("channel", ch_id)
                
                if offset != 0:
                    start_str = new_prog.get("start", "")
                    stop_str = new_prog.get("stop", "")
                    if start_str:
                        new_prog.set("start", shift_xmltv_time(start_str, offset))
                    if stop_str:
                        new_prog.set("stop", shift_xmltv_time(stop_str, offset))
                
                new_root.append(new_prog)
                cloned_count += 1

print(f"Успешно клонировано передач для пустых каналов: {cloned_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
