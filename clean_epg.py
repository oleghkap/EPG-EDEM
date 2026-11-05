
import copy
from bisect import bisect_left
import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ============================================================
# НАСТРОЙКИ
# ============================================================

PRIMARY_URL = "http://epg.one/epg2.xml.gz"
SECONDARY_URL = "http://iptvx.one/epg/epg.xml.gz"

OUTPUT_FILE = "epg_custom.xml.gz"

TIMEZONE = ZoneInfo("Europe/Moscow")

# Позавчера, вчера, сегодня и завтра.
DAYS_BEFORE = 2
DAYS_AFTER = 1

GZIP_LEVEL = 9
DOWNLOAD_TIMEOUT = 180
BUFFER_SIZE = 1024 * 1024

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

XMLTV_DATETIME_PATTERN = re.compile(
    r"^\s*(\d{14})(?:\s*([+-])(\d{2}):?(\d{2}))?\s*$"
)


# ============================================================
# XML И ВРЕМЯ
# ============================================================

def local_name(tag):
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def normalize_channel_name(value):
    return " ".join((value or "").casefold().split())


def get_channel_names(channel):
    names = set()

    for child in channel:
        if local_name(child.tag) == "display-name":
            name = normalize_channel_name(child.text)
            if name:
                names.add(name)

    return names


def parse_xmltv_time(value):
    """
    Поддерживаемые форматы:
        20261104120000
        20261104120000 +0300
        20261104120000 +03:00

    Время без часового пояса считается московским.
    Время с часовым поясом переводится в Europe/Moscow.
    Некорректный суффикс не игнорируется.
    """

    if not value:
        return None

    match = XMLTV_DATETIME_PATTERN.fullmatch(value)

    if not match:
        return None

    try:
        parsed = datetime.strptime(
            match.group(1),
            "%Y%m%d%H%M%S",
        )

        sign = match.group(2)

        if sign:
            hours = int(match.group(3))
            minutes = int(match.group(4))

            if hours > 23 or minutes > 59:
                return None

            offset_minutes = hours * 60 + minutes

            if sign == "-":
                offset_minutes = -offset_minutes

            source_tz = timezone(
                timedelta(minutes=offset_minutes)
            )

            return parsed.replace(
                tzinfo=source_tz
            ).astimezone(TIMEZONE)

        return parsed.replace(tzinfo=TIMEZONE)

    except (ValueError, OverflowError):
        return None


def get_programme_times(element):
    """
    Возвращает (start, stop).

    Отсутствующий stop допустим.
    Указанный, но некорректный stop недопустим.
    stop должен быть строго больше start.
    """

    start_value = element.attrib.get("start")

    if not start_value:
        return None, None

    start = parse_xmltv_time(start_value)

    if start is None:
        return None, None

    stop_value = element.attrib.get("stop")

    if stop_value is None:
        return start, None

    stop = parse_xmltv_time(stop_value)

    if stop is None or stop <= start:
        return None, None

    return start, stop


def programme_is_in_window(
    start,
    stop,
    window_start,
    window_end,
):
    if start is None:
        return False

    if stop is None:
        return window_start <= start < window_end

    return (
        start < window_end
        and stop > window_start
    )


def get_programme_key(element):
    """
    Ключ для сравнения передач:
    ID канала, время начала, названия.

    Используется для дедупликации только вторичного источника.
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
    removed = 0

    for child in list(element):
        if local_name(child.tag) in OPTIONAL_TAGS_TO_REMOVE:
            element.remove(child)
            removed += 1

    return removed


def release_element(root, element):
    if root is not None:
        try:
            root.remove(element)
        except ValueError:
            pass

    element.clear()


def format_size(size):
    units = ["B", "KB", "MB", "GB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} GB"


def iterparse_gzip(filename, events):
    with gzip.open(filename, "rb") as source:
        yield from ET.iterparse(source, events=events)


# ============================================================
# ЗАГРУЗКА ИСТОЧНИКОВ
# ============================================================

def download_source(url, destination):
    print(f"\nЗагрузка источника: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 EPG-EDEM/1.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=DOWNLOAD_TIMEOUT,
    ) as response:

        with open(destination, "wb") as output:
            shutil.copyfileobj(
                response,
                output,
                BUFFER_SIZE,
            )

    size = os.path.getsize(destination)

    if size < 100:
        raise RuntimeError(
            f"Скачанный файл слишком маленький: {url}"
        )

    with open(destination, "rb") as source:
        if source.read(2) != b"\x1f\x8b":
            raise RuntimeError(
                f"Файл не является GZIP-архивом: {url}"
            )

    # Проверяем архив полностью, включая контрольную сумму.
    with gzip.open(destination, "rb") as source:
        while source.read(BUFFER_SIZE):
            pass

    print(f"Размер загрузки: {format_size(size)}")

    return size


# ============================================================
# ЧТЕНИЕ КАНАЛОВ PRIMARY
# ============================================================

def read_primary_channels(filename):
    print("\nЧтение каналов основного источника...")

    channels = {}

    root_tag = None
    root_attributes = {}

    context = iterparse_gzip(
        filename,
        events=("start", "end"),
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

            release_element(root, element)

        elif tag == "programme":
            release_element(root, element)

    print(f"Каналов основного источника: {len(channels)}")

    return channels, root_tag, root_attributes


# ============================================================
# ЧТЕНИЕ КАНАЛОВ SECONDARY
# ============================================================

def build_primary_name_index(channels):
    index = {}

    for channel_id, channel in channels.items():

        for name in get_channel_names(channel):
            index.setdefault(name, set()).add(channel_id)

    return index


def read_secondary_channels(filename):
    """
    Читает каналы вторичного источника.

    Важно: освобождает не только channel, но и programme.
    Это предотвращает накопление всего XML в памяти.
    """

    print("\nЧтение каналов дополнительного источника...")

    channels = {}
    name_index = {}

    context = iterparse_gzip(
        filename,
        events=("start", "end"),
    )

    root = None
    depth = 0

    for event, element in context:

        if event == "start":

            depth += 1

            if root is None:
                root = element

            continue

        if depth == 2:

            tag = local_name(element.tag)

            if tag == "channel":

                channel_id = element.attrib.get("id")

                if channel_id:

                    names = get_channel_names(element)
                    channels[channel_id] = names

                    for name in names:
                        name_index.setdefault(
                            name,
                            set(),
                        ).add(channel_id)

            # Освобождаем все обработанные элементы верхнего
            # уровня, включая programme и неизвестные элементы.
            release_element(root, element)

        depth -= 1

    print(f"Каналов второго источника: {len(channels)}")

    return channels, name_index


def build_secondary_channel_map(
    primary_name_index,
    secondary_channels,
    secondary_name_index,
):
    """
    Сопоставляет только точные нормализованные display-name.

    Одно название может соответствовать нескольким channel ID основного
    источника (например, базовая версия, HD/FHD и orig). В таком случае
    расписание второго источника назначается всем этим ID. Никакого
    нечёткого поиска, удаления суффиксов или сопоставления с плейлистом нет.
    """

    channel_map = {}
    mapped_pairs = 0
    ambiguous = 0

    for secondary_id, names in secondary_channels.items():
        candidates = set()

        for name in names:
            candidates.update(primary_name_index.get(name, set()))

        if candidates:
            channel_map[secondary_id] = candidates
            mapped_pairs += len(candidates)
            if len(candidates) > 1:
                ambiguous += 1

    unmatched = len(secondary_channels) - len(channel_map)

    print(f"Сопоставлено каналов второго источника: {len(channel_map)}")
    print(f"Всего точных соответствий channel ID: {mapped_pairs}")
    print(f"Названий с несколькими каналами primary: {ambiguous}")
    print(f"Не сопоставлено: {unmatched}")

    return channel_map


def interval_overlaps(intervals, start, stop):
    """Проверяет пересечение [start, stop) с уже объединёнными интервалами."""
    if stop is None:
        return False

    index = bisect_left(intervals, (start, datetime.min.replace(tzinfo=TIMEZONE)))

    if index > 0 and intervals[index - 1][1] > start:
        return True

    return index < len(intervals) and intervals[index][0] < stop


def add_interval(intervals, start, stop):
    """Добавляет интервал и объединяет пересекающиеся интервалы."""
    if stop is None:
        return

    index = bisect_left(intervals, (start, datetime.min.replace(tzinfo=TIMEZONE)))
    merged_start = start
    merged_stop = stop

    if index > 0 and intervals[index - 1][1] >= merged_start:
        index -= 1
        merged_start = min(merged_start, intervals[index][0])
        merged_stop = max(merged_stop, intervals[index][1])

    while index < len(intervals) and intervals[index][0] < merged_stop:
        merged_stop = max(merged_stop, intervals[index][1])
        intervals.pop(index)

    intervals.insert(index, (merged_start, merged_stop))


# ============================================================
# ЗАПИСЬ XML
# ============================================================

class CountingWriter:
    def __init__(self, target):
        self.target = target
        self.bytes_written = 0

    def write(self, data):
        written = self.target.write(data)
        self.bytes_written += written
        return written


def write_xml_start(output, root_tag, root_attributes):
    root = ET.Element(root_tag, root_attributes)

    root_xml = ET.tostring(
        root,
        encoding="utf-8",
        short_empty_elements=False,
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
    output.write(
        ET.tostring(
            element,
            encoding="utf-8",
            short_empty_elements=True,
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
    channel_intervals,
    seen_starts,
    channel_map=None,
    secondary=False,
):
    stats = {
        "read": 0,
        "kept": 0,
        "outside": 0,
        "invalid": 0,
        "unmapped": 0,
        "duplicates": 0,
        "optional_removed": 0,
        "overlaps_skipped": 0,
    }

    source_name = "дополнительного" if secondary else "основного"
    print(f"\nОбработка передач {source_name} источника...")

    context = iterparse_gzip(filename, events=("start", "end"))
    root = None

    for event, element in context:
        if event == "start" and root is None:
            root = element
            continue
        if event != "end" or local_name(element.tag) != "programme":
            continue

        stats["read"] += 1
        source_channel = element.attrib.get("channel", "")

        if secondary:
            target_channels = (channel_map or {}).get(source_channel, set())
            if not target_channels:
                stats["unmapped"] += 1
                release_element(root, element)
                continue
        else:
            target_channels = {source_channel}

        start_time, stop_time = get_programme_times(element)
        if start_time is None:
            stats["invalid"] += 1
            release_element(root, element)
            continue

        if not programme_is_in_window(start_time, stop_time, window_start, window_end):
            stats["outside"] += 1
            release_element(root, element)
            continue

        wrote_any = False
        for target_channel in target_channels:
            if target_channel not in allowed_channels:
                continue

            starts = seen_starts.setdefault(target_channel, set())
            if start_time in starts:
                stats["duplicates"] += 1
                continue

            intervals = channel_intervals.setdefault(target_channel, [])
            if secondary and interval_overlaps(intervals, start_time, stop_time):
                stats["overlaps_skipped"] += 1
                continue

            output_element = copy.deepcopy(element) if secondary else element
            if secondary:
                output_element.attrib["channel"] = target_channel

            stats["optional_removed"] += remove_optional_tags(output_element)
            write_element(output, output_element)
            starts.add(start_time)

            if stop_time is not None:
                add_interval(intervals, start_time, stop_time)

            stats["kept"] += 1
            wrote_any = True

        if not wrote_any and not secondary:
            stats["unmapped"] += 1

        release_element(root, element)

        if stats["read"] % 100000 == 0:
            print(f"  Проверено: {stats['read']}; добавлено: {stats['kept']}")

    print(f"Прочитано передач: {stats['read']}")
    print(f"Добавлено передач: {stats['kept']}")
    print(f"За пределами периода: {stats['outside']}")
    print(f"Некорректное время: {stats['invalid']}")
    print(f"Не сопоставлено передач: {stats['unmapped']}")
    if secondary:
        print(f"Дубликатов по времени начала: {stats['duplicates']}")
        print(f"Пересечений с уже добавленными передачами пропущено: {stats['overlaps_skipped']}")
    print(f"Удалено необязательных тегов: {stats['optional_removed']}")

    return stats


# ============================================================
# ПРОВЕРКА ИТОГОВОГО XML
# ============================================================

def validate_output(filename):
    checked_elements = 0
    channels = 0

    with gzip.open(filename, "rb") as source:

        for event, element in ET.iterparse(
            source,
            events=("end",),
        ):

            checked_elements += 1

            if local_name(element.tag) == "channel":
                channels += 1

    if checked_elements == 0:
        raise RuntimeError("Итоговый XML пуст.")

    if channels == 0:
        raise RuntimeError(
            "В итоговом XML отсутствуют каналы."
        )

    return channels


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

def main():

    now = datetime.now(TIMEZONE)
    today = now.date()

    window_start = datetime.combine(
        today - timedelta(days=DAYS_BEFORE),
        datetime.min.time(),
        tzinfo=TIMEZONE,
    )

    window_end = datetime.combine(
        today + timedelta(days=DAYS_AFTER + 1),
        datetime.min.time(),
        tzinfo=TIMEZONE,
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
            "primary.xml.gz",
        )

        secondary_file = os.path.join(
            temp_dir,
            "secondary.xml.gz",
        )

        temporary_output = os.path.join(
            temp_dir,
            "epg_custom.tmp.xml.gz",
        )

        # 1. Загружаем основной источник.
        primary_download_size = download_source(
            PRIMARY_URL,
            primary_file,
        )

        # 2. Загружаем дополнительный источник.
        secondary_available = True
        secondary_download_size = 0

        try:

            secondary_download_size = download_source(
                SECONDARY_URL,
                secondary_file,
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

        # 3. Читаем каналы primary.
        channels, root_tag, root_attributes = (
            read_primary_channels(primary_file)
        )

        if not channels:
            raise RuntimeError(
                "Основной источник не содержит каналов."
            )

        primary_ids = set(channels.keys())

        # 4. Сопоставляем каналы secondary.
        channel_map = {}

        if secondary_available:

            primary_name_index = build_primary_name_index(
                channels
            )

            (
                secondary_channels,
                secondary_name_index,
            ) = read_secondary_channels(secondary_file)

            channel_map = build_secondary_channel_map(
                primary_name_index,
                secondary_channels,
                secondary_name_index,
            )

        # 5. Формируем временный выходной файл.
        print("\nСоздание итогового EPG...")

        channel_intervals = {}
        seen_starts = {}

        secondary_stats = {
            "read": 0,
            "kept": 0,
            "outside": 0,
            "invalid": 0,
            "unmapped": 0,
            "duplicates": 0,
            "optional_removed": 0,
            "overlaps_skipped": 0,
        }

        with gzip.open(
            temporary_output,
            "wb",
            compresslevel=GZIP_LEVEL,
        ) as gzip_output:

            output = CountingWriter(gzip_output)

            root_end = write_xml_start(
                output,
                root_tag,
                root_attributes,
            )

            # Сначала все каналы primary без подмены иконок.
            for channel in channels.values():
                write_element(output, channel)

            # Затем передачи primary.
            primary_stats = process_programmes(
                filename=primary_file,
                output=output,
                window_start=window_start,
                window_end=window_end,
                allowed_channels=primary_ids,
                channel_intervals=channel_intervals,
                seen_starts=seen_starts,
                secondary=False,
            )

            # Затем только недостающие передачи secondary.
            if secondary_available:

                secondary_stats = process_programmes(
                    filename=secondary_file,
                    output=output,
                    window_start=window_start,
                    window_end=window_end,
                    allowed_channels=primary_ids,
                    channel_map=channel_map,
                    channel_intervals=channel_intervals,
                    seen_starts=seen_starts,
                    secondary=True,
                )

            output.write(root_end)
            output.write(b"\n")

            uncompressed_size = output.bytes_written

        # 6. Проверяем временный файл до замены результата.
        print("\nПроверка итогового GZIP и XML...")

        output_channels = validate_output(temporary_output)

        # Заменяем прежний результат только после проверки.
        os.replace(
            temporary_output,
            OUTPUT_FILE,
        )

        compressed_size = os.path.getsize(OUTPUT_FILE)

        compression_percent = (
            (1 - compressed_size / uncompressed_size) * 100
            if uncompressed_size
            else 0
        )

        # 7. Итоговая статистика.
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

        print(f"Каналов в выходном файле: {output_channels}")

        print(
            "Передач из основного источника: "
            f"{primary_stats['kept']}"
        )

        print(
            "Дополнительных передач: "
            f"{secondary_stats['kept']}"
        )

        print(f"Всего передач: {total_programmes}")

        print(
            "Дубликатов второго источника по времени начала: "
            f"{secondary_stats['duplicates']}"
        )
        print(
            "Пересечений передач второго источника пропущено: "
            f"{secondary_stats['overlaps_skipped']}"
        )

        print(
            "Несопоставленных передач primary: "
            f"{primary_stats['unmapped']}"
        )

        print(
            "Несопоставленных передач secondary: "
            f"{secondary_stats['unmapped']}"
        )

        print(
            "Некорректное время primary: "
            f"{primary_stats['invalid']}"
        )

        print(
            "Некорректное время secondary: "
            f"{secondary_stats['invalid']}"
        )

        print(
            "Удалено необязательных XML-тегов: "
            f"{total_optional_removed}"
        )

        print("-" * 65)

        print(
            "Размер основного источника: "
            f"{format_size(primary_download_size)}"
        )

        if secondary_available:
            print(
                "Размер дополнительного источника: "
                f"{format_size(secondary_download_size)}"
            )
        else:
            print("Дополнительный источник: недоступен")

        print("-" * 65)

        print(
            f"Размер итогового GZIP: {format_size(compressed_size)}"
        )

        print(
            f"Размер XML без сжатия: {format_size(uncompressed_size)}"
        )

        print(
            f"Экономия за счёт GZIP: {compression_percent:.1f}%"
        )

        print("-" * 65)
        print(f"Итоговый файл: {OUTPUT_FILE}")
        print("=" * 65)


if __name__ == "__main__":
    main()
