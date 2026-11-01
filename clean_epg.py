import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os

EPG_URL = "https://epg.one/edem.xml.gz"

# Жесткие стоп-слова для исключения ненужных стран и регионов
EXCLUDE_KEYWORDS = [
    "US |", "USA |", "США", "US:", "USA:",
    "UA |", "UKR |", "Украина", "Україна", "UA:",
    "BY |", "Беларусь", "Белоруссия", "BY:"
]

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

print("Распаковка и фильтрация XML...")
with gzip.open("raw_epg.xml.gz", "rb") as f:
    tree = ET.parse(f)

root = tree.getroot()
kept_channels = set()
removed_channels = set()

# Проходим по всем каналам и вырезаем только то, что попадает под стоп-слова стран
for channel in root.findall("channel"):
    ch_id = channel.get("id", "").strip().lower()
    display_names = [dn.text.strip() for dn in channel.findall("display-name") if dn.text]
    
    full_str = f"{ch_id} " + " ".join(display_names)
    
    # Если канал принадлежит запрещенной стране — удаляем его
    if any(kw.lower() in full_str.lower() for kw in EXCLUDE_KEYWORDS):
        removed_channels.add(channel.get("id"))
        root.remove(channel)
    else:
        kept_channels.add(channel.get("id"))

print(f"Оставлено каналов после фильтрации стран: {len(kept_channels)}")

# Очищаем передачи только для удаленных каналов, сохраняя описания для всех остальных
programme_count = 0
for programme in root.findall("programme"):
    if programme.get("channel") in removed_channels:
        root.remove(programme)
    else:
        programme_count += 1

print(f"Оставлено блоков передач с описаниями: {programme_count}")

print("Сохранение оптимизированного EPG...")
with gzip.open("epg_custom.xml.gz", "wb") as f:
    tree.write(f, encoding="utf-8", xml_declaration=True)

print("Готово!")
