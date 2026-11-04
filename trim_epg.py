import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


EPG_URL = "http://epg.one/epg2.xml.gz"

OUTPUT_FILE = "epg_trimmed.xml.gz"
RAW_FILE = "raw_epg.xml.gz"

DAYS_BEFORE = 2
DAYS_AFTER = 1

MOSCOW = ZoneInfo("Europe/Moscow")

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


def local_name(tag):
    """Return XML tag name without namespace."""
    return tag.rsplit("}", 1)[-1]


def download_file(url, destination):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; EPG-EDEM/1.0)"
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            with open(destination, "wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)

        return os.path.getsize(destination) > 0

    except Exception as exc:
        print(f"Ошибка скачивания: {exc}")
        return False


def parse_xmltv_datetime(value):
    if not value:
        return None

    match = re.match(
        r"^\s*(\d{14})(?:\s*([+-])(\d{2}):?(\d{2}))?\s*$",
        value,
    )

    if not match:
        return None

    try:
        parsed = datetime.strptime(
            match.group(1),
            "%Y%m%d%H%M%S",
        )

        sign = match.group(2)

        if sign:
            offset_minutes = (
                int(match.group(3)) * 60
                + int(match.group(4))
            )

            if sign == "-":
                offset_minutes = -offset_minutes

            source_tz = timezone(
                timedelta(minutes=offset_minutes)
            )

            return parsed.replace(
                tzinfo=source_tz
            ).astimezone(MOSCOW)

        return parsed.replace(tzinfo=MOSCOW)

    except (ValueError, OverflowError):
        return None


def programme_is_in_window(element, window_start, window_end):
    start = parse_xmltv_datetime(
        element.get("start", "")
    )

    if start is None:
        return False

    stop = parse_xmltv_datetime(
        element.get("stop", "")
    )

    if stop is None:
        return window_start <= start < window_end

    return start < window_end and stop > window_start


def remove_unneeded_metadata(element):
    removed = 0

    for child in list(element):
        if local_name(child.tag) in REMOVED_PROGRAMME_TAGS:
            element.remove(child)
            removed += 1

    return removed


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

    root_element = None
    closing_tag = None
    depth = 0

    output_dir = os.path.dirname(
        os.path.abspath(output_path)
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
                 compresslevel=6,
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

                        closing_tag = b"</" + closing_part

                    continue

                if depth == 2:

                    tag = local_name(element.tag)

                    keep_element = True

                    if tag == "programme":

                        total_programmes += 1

                        start_value = element.get("start", "")
                        parsed_start = parse_xmltv_datetime(
                            start_value
                        )

                        if parsed_start is None:
                            invalid_dates += 1

                        keep_element = programme_is_in_window(
                            element,
                            window_start,
                            window_end,
                        )

                        if keep_element:

                            removed_metadata += (
                                remove_unneeded_metadata(element)
                            )

                            kept_programmes += 1

                        else:
                            removed_programmes += 1

                    elif tag == "channel":

                        channels += 1

                    if keep_element:

                        output.write(
                            ET.tostring(
                                element,
                                encoding="utf-8",
                                short_empty_elements=True,
                            )
                        )

                    if root_element is not None:
                        root_element.remove(element)

                    element.clear()

                depth -= 1

            if root_element is None or closing_tag is None:
                raise ValueError(
                    "Не найден корневой элемент XMLTV."
                )

            output.write(closing_tag)

        # Проверяем весь итоговый gzip и XML, а не только начало файла.
        with gzip.open(temporary_output, "rb") as check_file:
            for _, _ in ET.iterparse(
                check_file,
                events=("end",),
            ):
                pass

        os.replace(
            temporary_output,
            output_path,
        )

    except Exception:

        if os.path.exists(temporary_output):
            os.remove(temporary_output)

        raise

    output_compressed_size = os.path.getsize(output_path)

    with gzip.open(output_path, "rb") as output:
        output_uncompressed_size = sum(
            len(chunk)
            for chunk in iter(
                lambda: output.read(1024 * 1024),
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


def main():

    print(f"Скачивание EPG: {EPG_URL}")

    if not download_file(EPG_URL, RAW_FILE):
        raise RuntimeError(
            "Не удалось скачать исходный EPG. "
            "Итоговый файл не изменён."
        )

    trim_epg(
        RAW_FILE,
        OUTPUT_FILE,
    )


if __name__ == "__main__":
    main()
