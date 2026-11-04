
import copy
import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# НАСТРОЙКИ
# ============================================================

PRIMARY_URL = "http://epg.one/epg2.xml.gz"
SECONDARY_URL = "http://iptvx.one/epg/epg.xml.gz"

OUTPUT_FILE = "epg_custom.xml.gz"

TIMEZONE = ZoneInfo("Europe/Moscow")

# Четыре календарных дня:
# позавчера, вчера, сегодня и завтра.
DAYS_BEFORE = 2
DAYS_AFTER = 1

GZIP_LEVEL = 6
DOWNLOAD_TIMEOUT = 180

OPTIONAL_TAGS_TO_REMOVE = {
    "credits",
    "rating",
    "star-rating",
    "country",
    "date",
    "episode-num",
    "url",
    "icon",
}


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def local_name(tag):
    """Возвращает имя XML-тега без namespace."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def normalize_channel_name(value):
    """
    Нормализует только регистр и пробелы.

    HD, FHD, SD, 4K, региональные обозначения
    и временные сдвиги сохраняются.
    """
    return " ".join((value or "").casefold().split())


def get_channel_names(channel):
    """Получает все названия канала из display-name."""
    names = set()

    for child in channel:
        if local_name(child.tag) == "display-name":
            name = normalize_channel_name(child.text)

            if name:
                names.add(name)

    return names


def parse_xmltv_time(value):
    """
    Разбирает время XMLTV:
    20261104120000 +0300
    20261104120000
    """
    if not value:
        return None

    value = value.strip()

    try:
        return datetime.strptime(
            value,
            "%Y%m%d%H%M%S %z"
        ).astimezone(TIMEZONE)

    except ValueError:
        pass

    try:
        result = datetime.strptime(
            value[:14],
            "%Y%m%d%H%M%S"
        )

        return result.replace(tzinfo=TIMEZONE)

    except ValueError:
        return None


def programme_is_in_window(element, window_start, window_end):
    """Проверяет попадание передачи в заданный период."""

    start = parse_xmltv_time(element.attrib.get("start"))

    if start is None:
        return False

    stop_value = element.attrib.get("stop")
    stop = parse_xmltv_time(stop_value)

    # Если окончание отсутствует, проверяем начало передачи.
    if not stop_value:
        return window_start <= start < window_end

    # Если окончание указано, но некорректно,
    # передача не проходит проверку.
    if stop is None:
        return False

    # Учитываем передачи, которые начались до окна,
    # но заканчиваются уже внутри него.
    return start < window_end and stop > window_start


def get_programme_key(element):
    """
    Ключ для поиска передач, уже присутствующих
    в основном источнике.

    Используются:
    - ID канала;
    - момент начала передачи;
    - название передачи.

    Время сравнивается с учётом часового пояса.
    """

    channel = element.attrib.get("channel", "")

    start = parse_xmltv_time(
        element.attrib.get("start")
    )

    if start is None:
        return None

    titles = []

    for child in element:
        if local_name(child.tag) == "title":
            title = " ".join(
                (child.text or "").casefold().split()
            )

            if title:
                titles.append(title)

    if not titles:
        return None

    return (
        channel,
        start.timestamp(),
        tuple(sorted(titles)),
    )


def remove_optional_tags(element):
    """Удаляет только заданные необязательные теги передачи."""

    removed = 0

    for child in list(element):
        if local_name(child.tag) in OPTIONAL_TAGS_TO_REMOVE:
            element.remove(child)
            removed += 1

    return removed


def format_size(size):
    """Форматирует размер файла."""

    units = ["B", "KB", "MB", "GB"]

    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} GB"


# ============================================================
# ЗАГРУЗКА ИСТОЧНИКОВ
# ============================================================

def download_source(url, destination):
    """Скачивает и проверяет GZIP-файл."""

    print(f"\nЗагрузка источника: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 EPG-EDEM/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=DOWNLOAD_TIMEOUT
    ) as response:

        with open(destination, "wb") as output:
            shutil.copyfileobj(response, output)

    size = os.path.getsize(destination)

    if size < 100:
        raise RuntimeError(
            f"Скачанный файл слишком маленький: {url}"
        )

    # Проверяем целостность GZIP и возможность чтения.
    with gzip.open(destination, "rb") as source:
        source.read(2)

    print(f"Размер загрузки: {format_size(size)}")

    return size


# ============================================================
# ЧТЕНИЕ КАНАЛОВ ОСНОВНОГО ИСТОЧНИКА
# ============================================================

def read_primary_channels(filename):
    """
    Читает каналы основного источника.

    Сохраняет оригинальные:
    - ID;
    - названия;
    - логотипы;
    - атрибуты;
    - остальные данные каналов.
    """

    print("\nЧтение каналов основного источника...")

    channels = {}

    root_tag = None
    root_attributes = {}

    context = ET.iterparse(
        filename,
        events=("start", "end")
    )

    root = None

    for event, element in context:

        if event == "start" and root is None:
            root = element
            root_tag = element.tag
            root_attributes = dict(element.attrib)
            continue

        if event != "end":
            continue

        tag = local_name(element.tag)

        if tag == "channel":

            channel_id = element.attrib.get("id")

            if channel_id:
                channels[channel_id] = copy.deepcopy(element)

            element.clear()

            # Удаляем уже обработанные элементы
            # из корневого дерева, чтобы не накапливать память.
            if root is not None:
                root.clear()

        elif tag == "programme":
            element.clear()

            if root is not None:
                root.clear()

    print(f"Каналов основного источника: {len(channels)}")

    return channels, root_tag, root_attributes


# ============================================================
# СОПОСТАВЛЕНИЕ КАНАЛОВ
# ============================================================

def build_primary_name_index(channels):
    """Создаёт индекс названий каналов основного источника."""

    index = {}

    for channel_id, channel in channels.items():

        for name in get_channel_names(channel):
            index.setdefault(name, set()).add(channel_id)

    return index


def read_secondary_channels(filename):
    """
    Читает названия каналов второго источника.

    Индекс позволяет проверить, что название
    встречается только у одного канала.
    """

    print("\nЧтение каналов дополнительного источника...")

    channels = {}
    name_index = {}

    context = ET.iterparse(
        filename,
        events=("start", "end")
    )

    root = None

    for event, element in context:

        if event == "start" and root is None:
            root = element
            continue

        if event != "end":
            continue

        if local_name(element.tag) != "channel":
            continue

        channel_id = element.attrib.get("id")

        if channel_id:

            names = get_channel_names(element)

            channels[channel_id] = names

            for name in names:
                name_index.setdefault(name, set()).add(channel_id)

        element.clear()

        if root is not None:
            root.clear()

    print(f"Каналов второго источника: {len(channels)}")

    return channels, name_index


def build_secondary_channel_map(
    primary_name_index,
    secondary_channels,
    secondary_name_index
):
    """
    Сопоставляет каналы второго источника с основным.

    Совпадение разрешено только тогда, когда название
    однозначно соответствует одному каналу в обоих источниках.
    """

    channel_map = {}

    matched = 0
    ambiguous = 0

    for secondary_id, names in secondary_channels.items():

        candidates = set()

        for name in names:

            primary_matches = primary_name_index.get(name, set())
            secondary_matches = secondary_name_index.get(name, set())

            # Название должно быть уникальным
            # в обоих источниках.
            if (
                len(primary_matches) == 1
                and len(secondary_matches) == 1
            ):
                candidates.update(primary_matches)

        if len(candidates) == 1:

            channel_map[secondary_id] = next(iter(candidates))
            matched += 1

        elif len(candidates) > 1:
            ambiguous += 1

    unmatched = len(secondary_channels) - matched - ambiguous

    print(f"Однозначно сопоставлено: {matched}")
    print(f"Неоднозначных соответствий: {ambiguous}")
    print(f"Не сопоставлено: {unmatched}")

    return channel_map


# ============================================================
# ЗАПИСЬ XML
# ============================================================

class CountingWriter:
    """Считает размер XML до GZIP-сжатия."""

    def __init__(self, target):
        self.target = target
        self.bytes_written = 0

    def write(self, data):
        written = self.target.write(data)
        self.bytes_written += written
        return written


def write_xml_start(output, root_tag, root_attributes):
    """Записывает XML-декларацию и открывающий корневой тег."""

    root = ET.Element(root_tag, root_attributes)

    root_xml = ET.tostring(
        root,
        encoding="utf-8",
        short_empty_elements=False
    )

    closing_position = root_xml.rfind(b"</")

    if closing_position < 0:
        raise RuntimeError(
            "Не удалось сформировать корневой XML-тег"
        )

    root_start = root_xml[:closing_position]
    root_end = root_xml[closing_position:]

    output.write(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
    )

    output.write(root_start)
    output.write(b"\n")

    return root_end


def write_element(output, element):
    """Записывает XML-элемент."""

    output.write(
        ET.tostring(
            element,
            encoding="utf-8",
            short_empty_elements=True
        )
    )

    output.write(b"\n")


# ============================================================
# ОБРАБОТКА ПЕРЕДАЧ
# ============================================================

def process_programmes(
    filename,
    output,
    window_start,
    window_end,
    allowed_channels,
    seen_keys,
    channel_map=None,
    secondary=False,
):
    """
    Обрабатывает передачи потоково.

    Основной источник:
    - сохраняет все подходящие передачи;
    - не удаляет дубли;
    - не заменяет данные.

    Дополнительный источник:
    - сопоставляет канал;
    - добавляет только отсутствующие передачи.
    """

    stats = {
        "read": 0,
        "kept": 0,
        "outside": 0,
        "invalid": 0,
        "unmapped": 0,
        "duplicates": 0,
        "optional_removed": 0,
    }

    source_name = (
        "дополнительного"
        if secondary
        else "основного"
    )

    print(f"\nОбработка передач {source_name} источника...")

    context = ET.iterparse(
        filename,
        events=("start", "end")
    )

    root = None

    for event, element in context:

        if event == "start" and root is None:
            root = element
            continue

        if event != "end":
            continue

        if local_name(element.tag) != "programme":
            continue

        stats["read"] += 1

        source_channel = element.attrib.get("channel", "")

        # Для второго источника ищем ID основного канала.
        if secondary:

            target_channel = channel_map.get(source_channel)

            if target_channel is None:
                stats["unmapped"] += 1
                element.clear()

                if root is not None:
                    root.clear()

                continue

        else:
            target_channel = source_channel

        # Не добавляем передачи, для которых
        # отсутствует соответствующий канал в основном EPG.
        if target_channel not in allowed_channels:

            stats["unmapped"] += 1
            element.clear()

            if root is not None:
                root.clear()

            continue

        start_value = element.attrib.get("start")
        stop_value = element.attrib.get("stop")

        start_time = parse_xmltv_time(start_value)
        stop_time = parse_xmltv_time(stop_value)

        if (
            start_time is None
            or (stop_value and stop_time is None)
        ):
            stats["invalid"] += 1
            element.clear()

            if root is not None:
                root.clear()

            continue

        if not programme_is_in_window(
            element,
            window_start,
            window_end
        ):
            stats["outside"] += 1
            element.clear()

            if root is not None:
                root.clear()

            continue

        # Меняем только ссылку на канал
        # в дополнительном источнике.
        if secondary:
            element.attrib["channel"] = target_channel

        key = get_programme_key(element)

        # Дедупликация применяется только ко второму источнику.
        # Передачи основного источника не удаляются.
        if secondary and key is not None and key in seen_keys:

            stats["duplicates"] += 1
            element.clear()

            if root is not None:
                root.clear()

            continue

        if key is not None:
            seen_keys.add(key)

        stats["optional_removed"] += remove_optional_tags(element)

        write_element(output, element)

        stats["kept"] += 1

        element.clear()

        if root is not None:
            root.clear()

        if stats["read"] % 100000 == 0:

            print(
                f"  Проверено: {stats['read']}; "
                f"добавлено: {stats['kept']}"
            )

    print(f"Прочитано передач: {stats['read']}")
    print(f"Добавлено передач: {stats['kept']}")
    print(f"За пределами периода: {stats['outside']}")
    print(f"Некорректное время: {stats['invalid']}")
    print(f"Не сопоставлено передач: {stats['unmapped']}")

    if secondary:
        print(f"Дубликатов пропущено: {stats['duplicates']}")

    print(
        f"Удалено необязательных тегов: "
        f"{stats['optional_removed']}"
    )

    return stats


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

def main():

    now = datetime.now(TIMEZONE)
    today = now.date()

    window_start = datetime.combine(
        today - timedelta(days=DAYS_BEFORE),
        datetime.min.time(),
        tzinfo=TIMEZONE
    )

    window_end = datetime.combine(
        today + timedelta(days=DAYS_AFTER + 1),
        datetime.min.time(),
        tzinfo=TIMEZONE
    )

    print("=" * 65)
    print("EPG-EDEM: ОБЪЕДИНЕНИЕ XMLTV")
    print("=" * 65)

    print(f"Московская дата: {today}")
    print(f"Начало периода: {window_start}")
    print(f"Конец периода:  {window_end}")
    print("Количество календарных дней: 4")

    with tempfile.TemporaryDirectory() as temp_dir:

        primary_file = os.path.join(
            temp_dir,
            "primary.xml.gz"
        )

        secondary_file = os.path.join(
            temp_dir,
            "secondary.xml.gz"
        )

        # ----------------------------------------------------
        # 1. Загрузка основного источника.
        # ----------------------------------------------------

        primary_download_size = download_source(
            PRIMARY_URL,
            primary_file
        )

        # ----------------------------------------------------
        # 2. Загрузка дополнительного источника.
        # ----------------------------------------------------

        secondary_available = True
        secondary_download_size = 0

        try:

            secondary_download_size = download_source(
                SECONDARY_URL,
                secondary_file
            )

        except Exception as error:

            secondary_available = False

            print("\nПРЕДУПРЕЖДЕНИЕ:")
            print(
                "Дополнительный источник недоступен: "
                f"{error}"
            )
            print(
                "Будет сформирован EPG только "
                "из основного источника."
            )

        # ----------------------------------------------------
        # 3. Чтение каналов основного источника.
        # ----------------------------------------------------

        channels, root_tag, root_attributes = (
            read_primary_channels(primary_file)
        )

        if not channels:
            raise RuntimeError(
                "Основной источник не содержит каналов"
            )

        primary_ids = set(channels.keys())

        # ----------------------------------------------------
        # 4. Сопоставление каналов второго источника.
        # ----------------------------------------------------

        channel_map = {}

        if secondary_available:

            primary_name_index = build_primary_name_index(
                channels
            )

            (
                secondary_channels,
                secondary_name_index
            ) = read_secondary_channels(secondary_file)

            channel_map = build_secondary_channel_map(
                primary_name_index,
                secondary_channels,
                secondary_name_index
            )

        # ----------------------------------------------------
        # 5. Формирование выходного XML.
        # ----------------------------------------------------

        print("\nСоздание итогового EPG...")

        seen_keys = set()

        secondary_stats = {
            "read": 0,
            "kept": 0,
            "outside": 0,
            "invalid": 0,
            "unmapped": 0,
            "duplicates": 0,
            "optional_removed": 0,
        }

        with gzip.open(
            OUTPUT_FILE,
            "wb",
            compresslevel=GZIP_LEVEL
        ) as gzip_output:

            output = CountingWriter(gzip_output)

            root_end = write_xml_start(
                output,
                root_tag,
                root_attributes
            )

            # Сначала записываем все каналы основного источника.
            # ID, названия и логотипы сохраняются.
            for channel in channels.values():
                write_element(output, channel)

            # Передачи основного источника.
            primary_stats = process_programmes(
                filename=primary_file,
                output=output,
                window_start=window_start,
                window_end=window_end,
                allowed_channels=primary_ids,
                seen_keys=seen_keys,
                secondary=False,
            )

            # Только недостающие передачи второго источника.
            if secondary_available:

                secondary_stats = process_programmes(
                    filename=secondary_file,
                    output=output,
                    window_start=window_start,
                    window_end=window_end,
                    allowed_channels=primary_ids,
                    channel_map=channel_map,
                    seen_keys=seen_keys,
                    secondary=True,
                )

            output.write(root_end)
            output.write(b"\n")

            # Размер распакованного XML.
            uncompressed_size = output.bytes_written

        # ----------------------------------------------------
        # 6. Размер сжатого файла.
        # ----------------------------------------------------

        compressed_size = os.path.getsize(OUTPUT_FILE)

        if uncompressed_size > 0:

            compression_percent = (
                1 - compressed_size / uncompressed_size
            ) * 100

        else:
            compression_percent = 0

        # ----------------------------------------------------
        # 7. Итоговая статистика.
        # ----------------------------------------------------

        total_programmes = (
            primary_stats["kept"]
            + secondary_stats["kept"]
        )

        total_optional_removed = (
            primary_stats["optional_removed"]
            + secondary_stats["optional_removed"]
        )

        print("\n" + "=" * 65)
        print("ИТОГОВАЯ СТАТИСТИКА CLEAN_EPG")
        print("=" * 65)

        print(f"Каналов: {len(channels)}")

        print(
            f"Передач из основного источника: "
            f"{primary_stats['kept']}"
        )

        print(
            f"Дополнительных передач: "
            f"{secondary_stats['kept']}"
        )

        print(f"Всего передач: {total_programmes}")

        print(
            f"Дубликатов второго источника: "
            f"{secondary_stats['duplicates']}"
        )

        print(
            f"Передач с несопоставленными каналами "
            f"(основной источник): "
            f"{primary_stats['unmapped']}"
        )

        print(
            f"Передач с несопоставленными каналами "
            f"(второй источник): "
            f"{secondary_stats['unmapped']}"
        )

        print(
            f"Некорректное время (основной источник): "
            f"{primary_stats['invalid']}"
        )

        print(
            f"Некорректное время (второй источник): "
            f"{secondary_stats['invalid']}"
        )

        print(
            f"Удалено необязательных XML-тегов: "
            f"{total_optional_removed}"
        )

        print("-" * 65)

        print(
            f"Размер основного источника: "
            f"{format_size(primary_download_size)}"
        )

        if secondary_available:

            print(
                f"Размер дополнительного источника: "
                f"{format_size(secondary_download_size)}"
            )

        else:

            print("Дополнительный источник: недоступен")

        print("-" * 65)

        print(
            f"Размер итогового GZIP: "
            f"{format_size(compressed_size)}"
        )

        print(
            f"Размер XML без сжатия: "
            f"{format_size(uncompressed_size)}"
        )

        print(
            f"Экономия за счёт GZIP: "
            f"{compression_percent:.1f}%"
        )

        print("-" * 65)

        print(f"Итоговый файл: {OUTPUT_FILE}")
        print("=" * 65)


if __name__ == "__main__":
    main()
