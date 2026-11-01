import gzip
import xml.etree.ElementTree as ET
import urllib.request
import os
import re

EPG_URL = "https://epg.one/edem.xml.gz"
# Ссылка берется из секретов GitHub Actions, либо используется заглушка
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "")

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

valid_ids = set()
valid_names = set()

if PLAYLIST_URL:
    print("Загрузка плейлиста по защищенной ссылке...")
    try:
        req = urllib.request.Request(PLAYLIST_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            playlist_text = response.read().decode('utf-8', errors='ignore')
            
        for line in playlist_text.splitlines():
            m_id = re.search(r'tvg-id="([^"]+)"', line)
            if m_id:
                valid_ids.add(m_id.group(1).strip().lower())
            m_name = re.search(r'tvg-name="([^"]+)"', line)
            if m_name:
                valid_names.add(m_name.group(1).strip().lower())
            if "," in line and not line.startswith("#"):
                parts = line.strip().split(",")
                if len(parts) > 1:
                    valid_names.add(parts[-1].strip().lower())
        print(f"Успешно обработано каналов из плейлиста: {max(len(valid_ids), len(valid_names))}")
    except Exception as e:
        print(f"Ошибка при скачивании плейлиста: {e}")
else:
    print("Внимание: PLAYLIST_URL не задан, фильтрация по плейлисту пропущена.")

print("Распаковка и фильтрация XML...")
with gzip.open("raw_epg.xml.gz", "rb") as f:
    tree = ET.parse(f)

root = tree.getroot()
removed_channels = set()

# Если плейлист загрузился, оставляем только нужные каналы
if valid_ids or valid_names:
    for channel in root.findall("channel"):
        ch_id = channel.get("id", "").strip().lower()
        display_names = [dn.text.strip().lower() for dn in channel.findall("display-name") if dn.text]
        
        keep = False
        if ch_id in valid_ids:
            keep = True
        for name in display_names:
            if name in valid_names or any(vn in name or name in vn for vn in valid_names):
                keep = True
                break
                
        if not keep:
            removed_channels.add(channel.get("id"))
            root.remove(channel)
    print(f"Удалено неиспользуемых каналов из EPG: {len(removed_channels)}")

# Удаляем программы для удаленных каналов
programme_count = 0
for programme in root.findall("programme"):
    if programme.get("channel") in removed_channels:
        root.remove(programme)
        programme_count += 1

print(f"Удалено лишних блоков передач: {programme_count}")

print("Сохранение ультра-компактного EPG...")
with gzip.open("epg_custom.xml.gz", "wb") as f:
    tree.write(f, encoding="utf-8", xml_declaration=True)

print("Готово!")
