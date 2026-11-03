import gzip
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime, timedelta

EPG_URL = "https://ottg.tv/epg.xml.gz"

def download_file(url, dest):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VLC/3.0.16'})
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

print("Скачивание EPG источника...")
if download_file(EPG_URL, "raw_epg.xml.gz"):
    new_root = ET.Element("tv")
    
    print("Копирование каналов...")
    channel_count = 0
    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "channel":
                new_root.append(elem)
                channel_count += 1
            else:
                elem.clear()
                
    print(f"Всего каналов сохранено: {channel_count}")

    # Диапазон дат: вчера + 3 дня вперед
    now = datetime.utcnow()
    min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    max_date = (now + timedelta(days=3)).strftime("%Y%m%d")

    print("Фильтрация программ по датам (вчера + 3 дня)...")
    programme_count = 0
    with gzip.open("raw_epg.xml.gz", "rb") as f_in:
        for event, elem in ET.iterparse(f_in, events=("end",)):
            if elem.tag == "programme":
                start = elem.get("start", "")
                if start and min_date <= start[:8] <= max_date:
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
    print("Готово! Файл сохранен как epg_trimmed.xml.gz")
else:
    print("Не удалось скачать исходный файл.")
