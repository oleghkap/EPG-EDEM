import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os

# Ссылка на полный EPG провайдера
EPG_URL = "https://epg.one/edem.xml.gz"

# Исключаемые слова/префиксы в названиях каналов или их категорий
EXCLUDE_KEYWORDS = [
    "US |", "USA |", "США", "US:", "USA:",
    "UA |", "UKR |", "Украина", "Україна", "UA:",
    "BY |", "Беларусь", "Белоруссия", "BY:"
]

print("Скачивание EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

print("Распаковка и парсинг XML...")
with gzip.open("raw_epg.xml.gz", "rb") as f:
    tree = ET.parse(f)

root = tree.getroot()
removed_channels = set()

# 1. Фильтрация каналов <channel>
for channel in root.findall("channel"):
    channel_id = channel.get("id")
    display_name = channel.find("display-name")
    name_text = display_name.text if display_name is not None and display_name.text else ""

    # Проверяем, содержит ли канал исключаемые ключевые слова
    if any(keyword.lower() in name_text.lower() for keyword in EXCLUDE_KEYWORDS):
        removed_channels.add(channel_id)
        root.remove(channel)

print(f"Удалено каналов: {len(removed_channels)}")

# 2. Фильтрация программ передач <programme> для удаленных каналов
programme_count = 0
for programme in root.findall("programme"):
    if programme.get("channel") in removed_channels:
        root.remove(programme)
        programme_count += 1

print(f"Удалено блоков передач: {programme_count}")

# 3. Сохранение и сжатие оптимизированного XML
print("Сохранение оптимизированного EPG...")
output_file = "epg_custom.xml.gz"
with gzip.open(output_file, "wb") as f:
    tree.write(f, encoding="utf-8", xml_declaration=True)

print("Готово! Файл epg_custom.xml.gz сформирован.")
