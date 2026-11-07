import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

EPG_INPUT_FILE = "epg_custom.xml.gz"
OUTPUT_FILE = "epg_trimmed.xml.gz"

DAYS_BEFORE = 2
DAYS_AFTER = 1

MOSCOW = ZoneInfo("Europe/Moscow")

DOWNLOAD_TIMEOUT = 90
GZIP_LEVEL = 6
BUFFER_SIZE = 1024 * 1024

REMOVED_PROGRAMME_TAGS = {
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
# XML UTILITIES
# ============================================================

def local_name(tag):
    """Return XML tag name without namespace."""
    return tag.rsplit("}", 1)[-1]


# ============================================================
# DOWNLOAD AND VALIDATION
# ============================================================

def download_file(url, destination):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; EPG-EDEM/1.0)"
        },
    )

    try:
        print(f"Скачивание: {url}")

        if os.path.exists(destination):
            os.remove(destination)

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

        if not os.path.exists(destination):
            raise RuntimeError("Файл не был создан.")

        file_size = os.path.getsize(destination)

        if file_size < 100:
            raise RuntimeError(
                f"Слишком маленький файл: {file_size} байт."
            )

        # Проверяем сигнатуру GZIP.
        with open(destination, "rb") as source:
            signature = source.read(2)

        if signature != b"\x1f\x8b":
            raise RuntimeError(
                "Полученный файл не является GZIP-архивом."
            )

        # Полностью распаковываем архив для проверки
        # целостности данных и контрольной суммы GZIP.
        with gzip.open(destination, "rb") as source:
            while source.read(BUFFER_SIZE):
                pass

        print(
            f"Скачивание завершено: "
            f"{file_size / 1024 / 1024:.2f} МБ"
        )

        return True

    except Exception as exc:
        print(f"Ошибка скачивания: {exc}")

        if os.path.exists(destination):
            os.remove(destination)

        return False


# ============================================================
# XMLTV DATETIME PARSER
# ============================================================

XMLTV_DATETIME_PATTERN = re.compile(
    r"^\s*(\d{14})(?:\s*([+-])(\d{2}):?(\d{2}))?\s*$"
)


def parse_xmltv_datetime(value):
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
            ).astimezone(MOSCOW)

        return parsed.replace(
            tzinfo=MOSCOW
        )

    except (ValueError, OverflowError):
        return None


# ============================================================
# PROGRAMME TIME VALIDATION
# ============================================================

def get_programme_times(element):
    """
    Returns:
        (start, stop) for a valid programme.
        (None, None) for invalid dates.

    Missing stop is allowed.
    An explicitly supplied but invalid stop is rejected.
    """

    start_value = element.get("start")

    if not start_value:
        return None, None

    start = parse_xmltv_datetime(start_value)

    if start is None:
        return None, None

    stop_value = element.get("stop")

    # Отсутствующий stop допустим.
    if stop_value is None:
        return start, None

    # Если stop указан, он обязан быть корректным.
    stop = parse_xmltv_datetime(stop_value)

    if stop is None:
        return None, None

    # Продолжительность передачи должна быть положительной.
    if stop <= start:
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

    # Если stop отсутствует, проверяем время начала.
    if stop is None:
        return window_start <= start < window_end

    # Сохраняем передачи, пересекающие временное окно.
    return (
        start < window_end
        and stop > window_start
    )


# ============================================================
# METADATA CLEANUP
# ============================================================

def remove_unneeded_metadata(element):
    removed = 0

    for child in list(element):
        if local_name(child.tag) in REMOVED_PROGRAMME_TAGS:
            element.remove(child)
            removed += 1

    return removed


# ============================================================
# EPG PROCESSING
# ============================================================

def trim_epg(source_path, output_path):

    today = datetime.now(MOSCOW).date()

    first_day = today - timedelta(days=DAYS_BEFORE)
    last_day = today + timedelta(days=DAYS_AFTER)

    window_start = datetime.combine(
        first_day,
        time.min,
        tzinfo=MOSCOW,
    )

    window_end = datetime.combine(
        last_day + timedelta(days=1),
        time.min,
        tzinfo=MOSCOW,
    )

    print()
    print("=" * 55)
    print("ОБРАБОТКА EPG")
    print("=" * 55)

    print(f"Сегодня по Москве: {today}")
    print(f"Начало периода:    {first_day}")
    print(f"Конец периода:     {last_day}")
    print("Продолжительность: 4 календарных дня")

    source_compressed_size = os.path.getsize(source_path)

    channels = 0
    total_programmes = 0
    kept_programmes = 0
    removed_programmes = 0
    invalid_dates = 0
    removed_metadata = 0
    overlaps_detected = 0
    last_stop_by_channel = {}

    root_element = None
    closing_tag = None
    depth = 0

    output_dir = os.path.dirname(
        os.path.abspath(output_path)
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    fd, temporary_output = tempfile.mkstemp(
        prefix="epg_trimmed_",
        suffix=".xml.gz",
        dir=output_dir,
    )

    os.close(fd)

    try:

        with gzip.open(source_path, "rb") as source, \
             gzip.open(
                 temporary_output,
                 "wb",
                 compresslevel=GZIP_LEVEL,
             ) as output:

            output.write(
                b'<?xml version="1.0" encoding="utf-8"?>\n'
            )

            for event, element in ET.iterparse(
                source,
                events=("start", "end"),
            ):

                if event == "start":

                    depth += 1

                    if depth == 1:

                        root_element = element

                        root_shell = ET.Element(
                            root_element.tag,
                            root_element.attrib,
                        )

                        serialized_root = ET.tostring(
                            root_shell,
                            encoding="utf-8",
                            short_empty_elements=False,
                        )

                        opening_tag, closing_part = (
                            serialized_root.rsplit(b"</", 1)
                        )

                        output.write(opening_tag)

                        # Сохраняем фактическое имя закрывающего
                        # тега, включая namespace prefix.
                        closing_tag = b"</" + closing_part

                    continue

                if depth == 2:

                    tag = local_name(element.tag)

                    keep_element = True

                    if tag == "programme":

                        total_programmes += 1

                        start, stop = get_programme_times(
                            element
                        )

                        if start is None:
                            invalid_dates += 1

                            keep_element = False

                        else:
                            keep_element = programme_is_in_window(
                                start,
                                stop,
                                window_start,
                                window_end,
                            )

                        if keep_element:

                            channel_id = element.attrib.get("channel", "")
                            previous_stop = last_stop_by_channel.get(channel_id)
                            if stop is not None:
                                if previous_stop is not None and start < previous_stop:
                                    overlaps_detected += 1
                                    raise ValueError(
                                        "Пересечение в объединённом EPG: "
                                        f"channel={channel_id}, start={start}, "
                                        f"previous_stop={previous_stop}"
                                    )
                                last_stop_by_channel[channel_id] = stop

                            removed_metadata += (
                                remove_unneeded_metadata(element)
                            )

                            kept_programmes += 1

                        else:
                            removed_programmes += 1

                    elif tag == "channel":

                        channels += 1

                    # Сохраняем все каналы и прочие элементы
                    # верхнего уровня. Для programme применяем фильтр.
                    if keep_element:

                        output.write(
                            ET.tostring(
                                element,
                                encoding="utf-8",
                                short_empty_elements=True,
                            )
                        )

                    # Удаляем только обработанный элемент.
                    # Не используем root_element.clear():
                    # он мог бы удалить ещё не обработанные данные.
                    if root_element is not None:
                        root_element.remove(element)

                    element.clear()

                depth -= 1

            if root_element is None or closing_tag is None:
                raise ValueError(
                    "Не найден корневой элемент XMLTV."
                )

            output.write(closing_tag)

        # ====================================================
        # VALIDATE GENERATED FILE
        # ====================================================

        print()
        print("Проверка итогового GZIP и XML...")

        checked_elements = 0

        with gzip.open(temporary_output, "rb") as check_file:

            for _, _ in ET.iterparse(
                check_file,
                events=("end",),
            ):
                checked_elements += 1

        if checked_elements == 0:
            raise ValueError(
                "Итоговый XML пуст."
            )

        if channels == 0:
            raise ValueError(
                "В исходном EPG не найдено ни одного канала."
            )

        if overlaps_detected != 0:
            raise ValueError(
                f"В итоговом EPG осталось пересечений: {overlaps_detected}"
            )

        # Заменяем итоговый файл только после успешной проверки.
        os.replace(
            temporary_output,
            output_path,
        )

    except Exception:

        if os.path.exists(temporary_output):
            os.remove(temporary_output)

        raise

    # ========================================================
    # STATISTICS
    # ========================================================

    output_compressed_size = os.path.getsize(output_path)

    with gzip.open(output_path, "rb") as output:
        output_uncompressed_size = sum(
            len(chunk)
            for chunk in iter(
                lambda: output.read(BUFFER_SIZE),
                b"",
            )
        )

    print()
    print("=" * 55)
    print("РЕЗУЛЬТАТ")
    print("=" * 55)

    print(f"Каналов сохранено:             {channels}")
    print(f"Всего передач в источнике:     {total_programmes}")
    print(f"Передач сохранено:             {kept_programmes}")
    print(f"Передач удалено:               {removed_programmes}")
    print(f"Передач с некорректной датой:  {invalid_dates}")
    print(f"Удалено дополнительных тегов:  {removed_metadata}")
    print(f"Пересечений после проверки:     {overlaps_detected}")

    print()
    print("РАЗМЕРЫ")

    print(
        f"Исходный GZIP:  "
        f"{source_compressed_size / 1024 / 1024:.2f} МБ"
    )

    print(
        f"Итоговый GZIP:  "
        f"{output_compressed_size / 1024 / 1024:.2f} МБ"
    )

    print(
        f"Итоговый XML:   "
        f"{output_uncompressed_size / 1024 / 1024:.2f} МБ"
    )

    if source_compressed_size:

        reduction = (
            1 - output_compressed_size / source_compressed_size
        ) * 100

        print(
            f"Изменение размера относительно исходного GZIP: "
            f"{reduction:.1f}%"
        )

    print()
    print(f"Готово: {output_path}")
    print("=" * 55)


# ============================================================
# MAIN
# ============================================================

def main():

    if not os.path.isfile(EPG_INPUT_FILE):
        raise RuntimeError(
            f"Не найден объединённый EPG: {EPG_INPUT_FILE}. "
            "Сначала выполните clean_epg.py."
        )

    print(f"Обработка объединённого EPG: {EPG_INPUT_FILE}")
    trim_epg(EPG_INPUT_FILE, OUTPUT_FILE)


if __name__ == "__main__":
    main()
