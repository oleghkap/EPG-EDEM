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
DAYS_AFTER = 2

MOSCOW = ZoneInfo("Europe/Moscow")


def download_file(url: str, destination: str) -> bool:
    """Download source EPG with TLS certificate validation."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; EPG-EDEM/1.0)"
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            with open(destination, "wb") as output:
                shutil.copyfileobj(
                    response,
                    output,
                    length=1024 * 1024
                )

        return os.path.getsize(destination) > 0

    except Exception as exc:
        print(f"Ошибка скачивания {url}: {exc}")
        return False


def parse_xmltv_datetime(value: str):
    """Parse XMLTV date and convert it to Moscow time."""

    if not value:
        return None

    match = re.match(
        r"^\s*(\d{14})(?:\s*([+-])(\d{2}):?(\d{2}))?\s*$",
        value
    )

    if not match:
        return None

    try:
        parsed = datetime.strptime(
            match.group(1),
            "%Y%m%d%H%M%S"
        )

        sign = match.group(2)
        hours = match.group(3)
        minutes = match.group(4)

        if sign:
            offset_minutes = int(hours) * 60 + int(minutes)

            if sign == "-":
                offset_minutes = -offset_minutes

            source_tz = timezone(
                timedelta(minutes=offset_minutes)
            )

            return parsed.replace(
                tzinfo=source_tz
            ).astimezone(MOSCOW)

        # If timezone is absent, interpret the time as Moscow time.
        return parsed.replace(tzinfo=MOSCOW)

    except (ValueError, OverflowError):
        return None


def programme_is_in_window(
    element: ET.Element,
    window_start: datetime,
    window_end: datetime
) -> bool:

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

    # Keep programmes that intersect the requested period.
    return start < window_end and stop > window_start


def trim_epg(source_path: str, output_path: str):

    today_moscow = datetime.now(MOSCOW).date()

    first_day = today_moscow - timedelta(
        days=DAYS_BEFORE
    )

    after_last_day = today_moscow + timedelta(
        days=DAYS_AFTER + 1
    )

    window_start = datetime.combine(
        first_day,
        time.min,
        tzinfo=MOSCOW
    )

    window_end = datetime.combine(
        after_last_day,
        time.min,
        tzinfo=MOSCOW
    )

    print(
        f"Московский диапазон: {first_day.isoformat()} — "
        f"{(after_last_day - timedelta(days=1)).isoformat()}"
    )

    print(
        "Обработка XMLTV без сопоставления, "
        "переименования и дублирования каналов..."
    )

    channel_count = 0
    programme_count = 0
    skipped_programmes = 0

    root_element = None
    depth = 0

    output_dir = os.path.dirname(
        os.path.abspath(output_path)
    )

    fd, temporary_output = tempfile.mkstemp(
        prefix="epg_trimmed_",
        suffix=".xml.gz",
        dir=output_dir
    )

    os.close(fd)

    try:

        with gzip.open(source_path, "rb") as source, \
             gzip.open(
                 temporary_output,
                 "wb",
                 compresslevel=6
             ) as output:

            output.write(
                b'<?xml version="1.0" encoding="utf-8"?>\n'
            )

            for event, element in ET.iterparse(
                source,
                events=("start", "end")
            ):

                if event == "start":

                    depth += 1

                    if depth == 1:

                        root_element = element

                        # Preserve attributes of the original <tv> element.
                        root_shell = ET.Element(
                            root_element.tag,
                            root_element.attrib
                        )

                        serialized_root = ET.tostring(
                            root_shell,
                            encoding="utf-8",
                            short_empty_elements=False
                        )

                        opening_tag = serialized_root.rsplit(
                            b"</",
                            1
                        )[0]

                        output.write(opening_tag)

                    continue

                if depth == 2:

                    keep_element = True

                    if element.tag == "programme":

                        keep_element = programme_is_in_window(
                            element,
                            window_start,
                            window_end
                        )

                        if keep_element:
                            programme_count += 1
                        else:
                            skipped_programmes += 1

                    elif element.tag == "channel":

                        channel_count += 1

                    if keep_element:

                        output.write(
                            ET.tostring(
                                element,
                                encoding="utf-8",
                                short_empty_elements=True
                            )
                        )

                    # Remove processed elements to limit RAM usage.
                    if root_element is not None:
                        root_element.remove(element)

                    element.clear()

                depth -= 1

            if root_element is None:
                raise ValueError(
                    "В исходном файле не найден корневой элемент <tv>"
                )

            output.write(b"</tv>")

        # Validate the generated gzip/XML before replacing the result.
        with gzip.open(temporary_output, "rb") as check_file:

            for _, _ in ET.iterparse(
                check_file,
                events=("start",)
            ):
                break

        os.replace(
            temporary_output,
            output_path
        )

    except Exception:

        if os.path.exists(temporary_output):
            os.remove(temporary_output)

        raise

    print(f"Сохранено каналов: {channel_count}")
    print(f"Сохранено передач: {programme_count}")

    print(
        "Исключено передач вне диапазона или "
        f"с некорректным start: {skipped_programmes}"
    )

    print(
        f"Готово: {output_path} "
        f"({os.path.getsize(output_path):,} байт)"
    )


def main():

    print(f"Скачивание EPG: {EPG_URL}")

    if not download_file(EPG_URL, RAW_FILE):

        raise RuntimeError(
            "Не удалось скачать исходный EPG. "
            "Итоговый файл не изменён."
        )

    trim_epg(
        RAW_FILE,
        OUTPUT_FILE
    )


if __name__ == "__main__":
    main()
