import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
import copy
from datetime import datetime, timedelta, timezone

EPG_URL_1 = "https://epg.one/epg2.xml.gz"
EPG_URL_2 = "https://epg.one/ru2.xml.gz"
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    for tag in ['fhd', 'hd', 'uhd', 'фхд', 'хд', 'ухд']:
        t = re.sub(rf'\b{tag}\b', '', t)
    
    lat_to_cyr = str.maketrans({
        'a': 'а', 'b': 'б', 'c': 'с', 'e': 'е', 'h': 'н', 
        'k': 'к', 'm': 'м', 'o': 'о', 'p': 'р', 't': 'т', 'x': 'х', 'y': 'у'
    })
    t = t.translate(lat_to_cyr)
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

# Читаем список каналов для фильтрации EPG
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
channel_id_to_name = {}
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
                
                if ch_id and (ch_id.lower() in valid_identifiers or ch_id in valid_identifiers):
                    keep = True
                else:
                    for dn in display_names:
                        norm_dn = normalize(dn)
                        if dn in valid_identifiers or norm_dn in valid_names_normalized:
                            keep = True
                            break
                        
                        matched = False
                        for vn in valid_names_normalized:
                            if len(vn) >= 3 and (norm_dn.startswith(vn) or vn.startswith(norm_dn)):
                                keep = True
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

# Собираем программы (вчера + 3 дня вперед)
now = datetime.now(timezone.utc)
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()
raw_programmes_by_channel = {}

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

print(f"Всего добавлено родных передач: {programme_count}")

# Карты для поиска доноров
base_channel_map = {}
for ch_id, progs in raw_programmes_by_channel.items():
    if len(progs) > 0:
        name = channel_id_to_name.get(ch_id, ch_id)
        base_channel_map[normalize(name)] = ch_id

print("Проверка и принудительное дублирование программ для пустых HD/FHD/UHD каналов...")
cloned_hd_count = 0

for ch_id in kept_channel_ids:
    # ГЛАВНОЕ ИЗМЕНЕНИЕ: проверяем количество реальных программ у этого конкретного канала
    channel_progs = raw_programmes_by_channel.get(ch_id, [])
    
    # Если программ ровно 0, значит канал пустой и его нужно заполнить
    if len(channel_progs) == 0:
        ch_name = channel_id_to_name.get(ch_id, ch_id)
        lower_name = ch_name.lower()
        
        is_hd_variant = any(tag in lower_name for tag in ['hd', 'fhd', 'uhd', 'хд', 'фхд', 'ухд'])
        has_time_shift = bool(re.search(r'\+\s*\d+', lower_name))
        
        if is_hd_variant and not has_time_shift:
            base_name_norm = normalize(ch_name)
            
            donor_ch_id = None
            if base_name_norm in base_channel_map:
                donor_ch_id = base_channel_map[base_name_norm]
            else:
                for b_norm, d_id in base_channel_map.items():
                    if len(b_norm) >= 3 and (base_name_norm.startswith(b_norm) or b_norm.startswith(base_norm)):
                        donor_ch_id = d_id
                        break
            
            if donor_ch_id and donor_ch_id in raw_programmes_by_channel:
                for prog_elem in raw_programmes_by_channel[donor_ch_id]:
                    new_prog = copy.deepcopy(prog_elem)
                    new_prog.set("channel", ch_id)
                    new_root.append(new_prog)
                    cloned_hd_count += 1

print(f"Успешно продублировано передач для пустых HD/FHD/UHD каналов: {cloned_hd_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
