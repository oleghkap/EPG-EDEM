import gzip
import urllib.request
import re

EPG_URL = "https://epg.one/edem.xml.gz"

# Ключевые слова стран, которые нужно полностью вырезать
EXCLUDE_KEYWORDS = [
    "US |", "USA |", "США", "US:", "USA:",
    "UA |", "UKR |", "Украина", "Україна", "UA:",
    "BY |", "Беларусь", "Белоруссия", "BY:"
]

print("Скачивание полного EPG...")
urllib.request.urlretrieve(EPG_URL, "raw_epg.xml.gz")

print("Фильтрация EPG построчно...")
current_channel_id = None
skip_current_block = False
saved_lines_count = 0

with gzip.open("raw_epg.xml.gz", "rt", encoding="utf-8", errors="ignore") as f_in, \
     gzip.open("epg_custom.xml.gz", "wt", encoding="utf-8") as f_out:
    
    for line in f_in:
        # Проверяем начало блока канала
        if "<channel" in line:
            current_channel_id = line
            skip_current_block = any(kw.lower() in line.lower() for kw in EXCLUDE_KEYWORDS)
            if not skip_current_block:
                f_out.write(line)
                saved_lines_count += 1
            continue
            
        # Если мы внутри тега </channel>, закрываем блок
        if "</channel>" in line:
            if not skip_current_block:
                f_out.write(line)
                saved_lines_count += 1
            current_channel_id = None
            skip_current_block = False
            continue
            
        # Если мы внутри запрещенного канала, пропускаем его строки (включая его расписание/описания)
        if current_channel_id and skip_current_block:
            continue
            
        # Для передач (<programme>): проверяем, ссылается ли передача на удаленный канал
        if "<programme" in line:
            # Ищем id канала в строке программы
            match = re.search(r'channel="([^"]+)"', line)
            if match:
                ch_ref = match.group(1)
                # Если этот канал есть в списке запрещенных — пропускаем программу
                if any(kw.lower() in ch_ref.lower() for kw in EXCLUDE_KEYWORDS):
                    # Пропускаем всю программу до тега </programme>
                    continue
                    
        # Записываем все остальные валидные строки (включая описания <desc>)
        f_out.write(line)
        saved_lines_count += 1

print(f"Готово! Обработано и сохранено строк: {saved_lines_count}")
