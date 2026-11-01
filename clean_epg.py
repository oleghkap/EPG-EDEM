import gzip
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime, timedelta

EPG_URL = "https://epg.one/edem.xml.gz"

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

print("Распаковка и оптимизация расписания...")
with gzip.open("raw_epg.xml.gz", "rb") as f:
    tree = ET.parse(f)

root = tree.getroot()

# Ограничиваем расписание только актуальными днями (сегодня + 2 дня вперед), 
# чтобы файл гарантированно влез в память телевизора, но описания (<desc>) сохраняются полностью!
now = datetime.utcnow()
min_date = (now - timedelta(days=1)).strftime("%Y%m%d")
max_date = (now + timedelta(days=2)).strftime("%Y%m%d")

programme_count = 0
removed_count = 0

for programme in list(root.findall("programme")):
    start = programme.get("start", "")
    if start:
        date_str = start[:8]
        # Удаляем программы, которые слишком далеко в будущем или прошлом
        if date_str < min_date or date_str > max_date:
            root.remove(programme)
            removed_count += 1
        else:
            programme_count += 1
    else:
        root.remove(programme)

print(f"Оставлено актуальных передач с описаниями: {programme_count}")
print(f"Удалено устаревших/дальних передач: {removed_count}")

print("Сохранение легкого EPG для телевизора...")
with gzip.open("epg_custom.xml.gz", "wb") as f:
    tree.write(f, encoding="utf-8", xml_declaration=True)

print("Готово!")
