import gzip
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime, timedelta

EPG_URL = "https://epg.one/edem.xml.gz"

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=2)).strftime("%Y%m%d")

print("Быстрая потоковая фильтрация EPG...")

new_root = ET.Element("tv")
programme_count = 0
removed_count = 0
channel_count = 0

# Открываем архив на чтение через gzip, чтобы парсер получил чистый XML
with gzip.open("raw_epg.xml.gz", "rb") as f_in:
    for event, elem in ET.iterparse(f_in, events=("end",)):
        if elem.tag == "channel":
            new_root.append(elem)
            channel_count += 1
        elif elem.tag == "programme":
            start = elem.get("start", "")
            if start and min_date <= start[:8] <= max_date:
                new_root.append(elem)
                programme_count += 1
            else:
                elem.clear()
                removed_count += 1

print(f"Обработано каналов: {channel_count}")
print(f"Оставлено актуальных передач с описаниями: {programme_count}")
print(f"Удалено устаревших передач: {removed_count}")

print("Сохранение оптимизированного EPG...")
tree = ET.ElementTree(new_root)
with gzip.open("epg_custom.xml.gz", "wb") as f_out:
    tree.write(f_out, encoding="utf-8", xml_declaration=True)

print("Готово!")
