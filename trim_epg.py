import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re
from datetime import datetime, timedelta

EPG_URL = "https://filmax-tv.ru/epg.xml.gz"
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

# Читаем список ваших каналов для гибкого сопоставления
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

print("Скачивание EPG источника ottg.tv...")
if download_file(EPG_URL, "raw_epg.xml.gz"):
    new_root = ET.Element("tv")
    kept_channel_ids = set()
    channel_count = 0

    print("Гибкий поиск и фильтрация каналов...")
    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                ch_id = elem.get("id", "").strip()
                if ch_id in kept_channel_ids:
                    elem.clear()
                    continue
                    
                display_names = [dn.text for dn in elem.findall("display-name") if dn.text]
                keep = False
                
                # Проверяем ID или отображаемые имена по расширенным правилам
                if ch_id and (ch_id.lower() in valid_identifiers or ch_id in valid_identifiers):
                    keep = True
                else:
                    for dn in display_names:
                        norm_dn = normalize(dn)
                        if not norm_dn:
                            continue
                        # Точное совпадение или частичное вхождение (учитываем даже короткие совпадения)
                        if dn in valid_identifiers or norm_dn in valid_names_normalized:
                            keep = True
                            break
                        matched = False
                        for vn in valid_names_normalized:
                            if len(vn) >= 2 and (norm_dn in vn or vn in norm_dn):
                                keep = True
                                matched = True
                                break
                        if matched:
                            break
                
                if keep and ch_id:
                    kept_channel_ids.add(ch_id)
                    new_root.append(elem)
                    channel_count += 1
                else:
                    elem.clear()

    print(f"Отобрано каналов (максимально): {channel_count}")

    # Диапазон дат: вчера + 3 дня вперед
    now = datetime.utcnow()
    min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

    print("Фильтрация программ по датам...")
    programme_count = 0
    added_programmes = set()

    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                ch_ref = elem.get("channel")
                start = elem.get("start", "")
                prog_key = (ch_ref, start)
                
                if ch_ref in kept_channel_ids and start and prog_key not in added_programmes:
                    if min_date <= start[:8] <= max_date:
                        added_programmes.add(prog_key)
                        new_root.append(elem)
                        programme_count += 1
                    else:
                        elem.clear()
                else:
                    elem.clear()

    print(f"Всего передач сохранено: {programme_count}")

    print("Сохранение обрезанного EPG...")
    tree = ET.ElementTree(new_root)
    with gzip.open("epg_trimmed.xml.gz", "wb") as f_out:
        tree.write(f_out, encoding="utf-8", xml_declaration=True)
    print("Готово!")
else:
    print("Не удалось скачать исходный файл.")
