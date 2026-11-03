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

def get_matched_template(ch_id, display_names):
    if ch_id and (ch_id.lower() in valid_identifiers or ch_id in valid_identifiers):
        return ch_id.lower()
    for dn in display_names:
        norm_dn = normalize(dn)
        if dn in valid_identifiers or norm_dn in valid_names_normalized:
            return norm_dn
        for vn in valid_names_normalized:
            if len(vn) >= 3 and (norm_dn.startswith(vn) or vn.startswith(norm_dn)):
                return vn
    return None

# Шаг 1: Собираем каналы из обоих источников и группируем по шаблонам из плейлиста
channel_groups = {}
src_to_final_id = {}

def collect_channels(file_path, file_src):
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                template = get_matched_template(ch_id, display_names)
                
                if template and ch_id:
                    has_icon = elem.find("icon") is not None
                    entry = {
                        'src_id': ch_id,
                        'file_src': file_src,
                        'has_icon': has_icon,
                        'elem': copy.deepcopy(elem)
                    }
                    if template not in channel_groups:
                        channel_groups[template] = []
                    channel_groups[template].append(entry)
                elem.clear()

collect_channels("raw_epg1.xml.gz", 1)
collect_channels("raw_epg2.xml.gz", 2)

new_root = ET.Element("tv")
channel_count = 0

# Выбираем для каждого канала лучшую карточку (приоритет у пикона, затем у первого источника)
for template, entries in channel_groups.items():
    best = max(entries, key=lambda x: (1 if x['has_icon'] else 0, 1 if x['file_src'] == 1 else 0))
    final_id = best['src_id']
    
    new_root.append(best['elem'])
    channel_count += 1
    
    # Все исходные ID для этого канала мапим на итоговый ID
    for entry in entries:
        src_to_final_id[entry['src_id']] = final_id

print(f"Всего отобрано уникальных каналов: {channel_count}")

# Шаг 2: Собираем программы (вчера + 3 дня вперед)
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

programme_count = 0
added_programmes = set()

def process_programmes(file_path):
    global programme_count
    if not os.path.exists(file_path):
        return
    with gzip.open(file_path, "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                src_ch_ref = elem.get("channel")
                start = elem.get("start", "")
                
                if src_ch_ref in src_to_final_id and start:
                    if min_date <= start[:8] <= max_date:
                        target_ch_id = src_to_final_id[src_ch_ref]
                        prog_key = (target_ch_id, start)
                        
                        if prog_key not in added_programmes:
                            added_programmes.add(prog_key)
                            elem.set("channel", target_ch_id)
                            new_root.append(elem)
                            programme_count += 1
                        else:
                            elem.clear()
                    else:
                        elem.clear()
                else:
                    elem.clear()

process_programmes("raw_epg1.xml.gz")
process_programmes("raw_epg2.xml.gz")

print(f"Всего добавлено передач: {programme_count}")

print("Сохранение финального EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)
print("Готово!")
