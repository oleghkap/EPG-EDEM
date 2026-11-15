#!/usr/bin/env python3

"""
EPG-EDEM lite.

Независимый скрипт (не делит код с clean_epg.py и trim_epg.py специально,
чтобы падение одного пайплайна не ломало другой).

Что делает:
  1. Скачивает ТОЛЬКО epg.one (единственный источник).
  2. Читает channels_test.txt и оставляет только те каналы источника,
     которые реально есть в вашем плейлисте. Порядок сопоставления:
       a) tvg-id плейлиста == id канала в epg.one (если в плейлисте
          есть tvg-id — скачивается отдельно по PLAYLIST_URL, не
          обязателен, без него матчинг работает как раньше);
       b) точное совпадение названия;
       c) совпадение по базовому названию без HD/FHD/UHD/4K с учётом
          страны (без слияния разных регионов/версий).
     UHD и 4K считаются одним и тем же качеством (QUALITY_EQUIVALENTS) —
     иначе каналы 4K/UHD часто теряются из-за разной маркировки
     в плейлисте и в epg.one.
  3. Для оставшихся каналов обрезает программы по датам
     (DAYS_BEFORE/DAYS_AFTER вокруг текущего дня по Москве).
  4. Удаляет второстепенные теги передач (credits, rating, country...).
  5. Обрезает длину <desc> до MAX_DESC_CHARS.
  6. Проверяет покрытие: сколько каналов из channels_test.txt вообще
     нашлись в epg.one. Список ненайденных пишется в COVERAGE_FILE —
     это нужно, чтобы заметить, когда channels_test.txt меняется
     (новый канал в плейлисте) и для него ещё нет сопоставления.

Файл НЕ зависит от epg_custom.xml.gz/epg_trimmed.xml.gz и не трогает их.
"""

import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

EPG_URL = "http://epg.one/epg2.xml.gz"
REFERENCE_FILE = "channels_test.txt"

# Плейлист используется ТОЛЬКО как источник tvg-id (необязательно).
# Список каналов и группы по-прежнему берутся из channels_test.txt.
PLAYLIST_URL_ENV = "PLAYLIST_URL"
MAX_PLAYLIST_SIZE = 50 * 1024 * 1024

OUTPUT_FILE = "epg_lite.xml.gz"
RAW_FILE = "raw_epg_lite.xml.gz"
RAW_PLAYLIST_FILE = "raw_playlist_lite.m3u"
COVERAGE_FILE = "epg_lite_coverage.txt"

DAYS_BEFORE = 2
DAYS_AFTER = 1

# Обрезка длинных описаний. None — не обрезать.
# По диагностике clean.py: desc — обычно 70-85% объёма несжатого XML,
# обрезка до 300-500 символов даёт заметную экономию при небольшой потере текста.
MAX_DESC_CHARS = 400

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
# ПОДБОР ИМЁН КАНАЛОВ
# (упрощённая версия логики clean_epg.py: без sequence-fingerprint —
# здесь достаточно решить "наш канал или нет", это не продакшен-маппинг
# с гарантией единственного правильного ID. tvg-id, если доступен,
# используется как приоритетный способ сопоставления — см. ниже)
# ============================================================

QUALITY_RE = re.compile(
    r"\b(ultra\s+hd|full\s+hd|fhd|uhd|4k|hevc|h\.?265|h\.?264|hd|sd|orig)\b",
    re.IGNORECASE,
)
COUNTRY_CODES = (
    "RU|UA|UK|GB|US|DE|IL|TR|RO|PL|FR|CA|GE|AM|BY|LT|LV|RS|AZ|KZ|"
    "IT|ES|NL|BE|CH|AT|FI|SE|NO|DK|HU|CZ|BG|GR|HR|SI|SK|PT|IE|AU|NZ|"
    "IN|JP|KR|BR|AR|MX|MD"
)
COUNTRY_PREFIX_RE = re.compile(
    rf"^(?:{COUNTRY_CODES})\s*[-–—:]\s*",
    re.IGNORECASE,
)
COUNTRY_SUFFIX_RE = re.compile(
    rf"\s+(?:{COUNTRY_CODES})\s*$",
    re.IGNORECASE,
)


def normalize_name(value):
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"\+\s*(\d+)\b", r" plus \1 ", value)
    value = value.replace("+", " plus ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_base_name(value):
    value = re.sub(
        r"\b(?:hd|fhd|uhd)\s+(?:50|60)\b",
        " ",
        value or "",
        flags=re.IGNORECASE,
    )
    return normalize_name(QUALITY_RE.sub(" ", value))


def strip_country_prefix(value):
    return COUNTRY_PREFIX_RE.sub("", value or "").strip()


def strip_country_suffix(value):
    return COUNTRY_SUFFIX_RE.sub("", value or "").strip()


def edge_country_codes(value):
    value = (value or "").strip()
    found = set()

    prefix = re.match(
        rf"^({COUNTRY_CODES})\s*[-–—:]\s*",
        value,
        re.IGNORECASE,
    )
    suffix = re.search(
        rf"\s+({COUNTRY_CODES})$",
        value,
        re.IGNORECASE,
    )

    if prefix:
        found.add(prefix.group(1).upper())
    if suffix:
        found.add(suffix.group(1).upper())

    return found


# В русскоязычных плейлистах/источниках "UHD" и "4K" обозначают одно и то
# же качество и используются непоследовательно (один и тот же канал может
# называться по-разному в плейлисте и в epg.one). Приводим их к одному
# маркеру, иначе такие каналы будут ошибочно сочтены разным качеством
# и отброшены как несовместимые.
QUALITY_EQUIVALENTS = {
    "uhd": "4k",
    "ultrahd": "4k",
}


def quality_markers(value):
    markers = set()

    for match in QUALITY_RE.finditer(value or ""):
        marker = match.group(1).casefold().replace(" ", "")
        markers.add(QUALITY_EQUIVALENTS.get(marker, marker))

    return markers


def base_names_compatible(source_name, reference_name):
    """Не объединяем явно разные маркеры качества или разные страны."""
    if quality_markers(source_name) != quality_markers(reference_name):
        return False

    source_countries = edge_country_codes(source_name)
    reference_countries = edge_country_codes(reference_name)

    if (
        source_countries
        and reference_countries
        and source_countries.isdisjoint(reference_countries)
    ):
        return False

    return True


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


# ============================================================
# ЧТЕНИЕ channels_test.txt
# ============================================================

def load_reference_channels(path):
    """
    Тот же табличный формат, что использует clean_epg.py:
    'N | CHANNEL | GRP'. Специально не импортируем clean_epg.py —
    скрипты должны оставаться независимыми друг от друга.
    """
    file_path = Path(path)

    if not file_path.is_file():
        raise RuntimeError(
            f"Не найден {path}. Этот файл обновляется в clean_epg.py "
            "(sync_reference_channels), убедитесь что он есть в репозитории "
            "до запуска epg_lite.py."
        )

    titles = []

    with file_path.open("r", encoding="utf-8-sig") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            if not line or line.startswith("---"):
                continue

            if line_number == 1 and "CHANNEL" in line.upper():
                continue

            if not re.match(r"^\s*\d+\s*\|", line):
                continue

            columns = [part.strip() for part in line.split("|")]

            if len(columns) < 2 or not columns[1]:
                continue

            titles.append(columns[1])

    if not titles:
        raise RuntimeError(f"{path} не содержит ни одной строки с каналом.")

    return titles


def build_reference_index(titles, title_to_tvg_id=None):
    by_name = {}
    by_base_name = {}
    by_tvg_id = {}

    title_to_tvg_id = title_to_tvg_id or {}

    for title in titles:
        normalized = normalize_name(title)

        if normalized:
            by_name.setdefault(normalized, []).append(title)

        countryless = strip_country_suffix(strip_country_prefix(title))
        base = normalize_base_name(countryless)

        if base:
            by_base_name.setdefault(base, []).append(title)

        tvg_id = title_to_tvg_id.get(title, "").strip()

        if tvg_id:
            by_tvg_id.setdefault(tvg_id.casefold(), []).append(title)

    return by_name, by_base_name, by_tvg_id


def match_reference_titles(channel_id, display_names, by_name, by_base_name, by_tvg_id):
    """
    Возвращает множество названий из channels_test.txt, которым
    соответствует канал источника (может быть больше одного —
    это нормально и безопасно для простого отбора "нужен / не нужен").

    Порядок проверки:
      1. tvg-id плейлиста == id канала в epg.one (точно и надёжно,
         если провайдер плейлиста использует общепринятые XMLTV ID).
      2. Точное совпадение нормализованного названия.
      3. Совпадение по базовому названию (без HD/FHD/UHD/4K) при
         совместимом качестве и стране (UHD и 4K считаются одним
         и тем же качеством, см. QUALITY_EQUIVALENTS).
    """
    if channel_id:
        tvg_matches = by_tvg_id.get(channel_id.strip().casefold())

        if tvg_matches:
            return set(tvg_matches)

    matched = set()

    for name in display_names:
        if not name:
            continue

        normalized = normalize_name(name)

        if normalized in by_name:
            matched.update(by_name[normalized])

    if matched:
        return matched

    for name in display_names:
        if not name:
            continue

        countryless = strip_country_suffix(strip_country_prefix(name))
        base = normalize_base_name(countryless)

        for candidate_title in by_base_name.get(base, []):
            if base_names_compatible(name, candidate_title):
                matched.add(candidate_title)

    return matched


# ============================================================
# TVG-ID ИЗ M3U-ПЛЕЙЛИСТА (необязательно)
# ============================================================

def find_m3u_title_separator(value):
    """Первая запятая вне кавычек атрибутов EXTINF."""
    quote = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == ",":
            return index
    return -1


def download_playlist(url, destination, max_size):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; EPG-EDEM-lite/1.0)"},
    )

    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        total = 0
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(BUFFER_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise RuntimeError("playlist download size limit exceeded")
                output.write(chunk)

    if total == 0:
        raise RuntimeError("empty playlist response")


def parse_playlist_tvg_ids(path):
    """
    Извлекает только title -> tvg-id из M3U. Группы и прочие атрибуты
    не нужны: список каналов и группы уже есть в channels_test.txt,
    здесь нас интересует только tvg-id для более надёжного сопоставления.
    """
    raw = Path(path).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1251")

    attribute_re = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
    title_to_tvg_id = {}

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF:"):
            continue

        separator = find_m3u_title_separator(line)
        if separator < 0:
            continue

        metadata = line[len("#EXTINF:"):separator]
        title = line[separator + 1:].strip().replace("|", "｜")
        if not title:
            continue

        attributes = {
            key.casefold(): value.strip()
            for key, value in attribute_re.findall(metadata)
        }
        tvg_id = attributes.get("tvg-id", "").strip()

        # При повторе имени оставляем первый непустой tvg-id.
        if tvg_id and not title_to_tvg_id.get(title):
            title_to_tvg_id[title] = tvg_id

    return title_to_tvg_id


def load_playlist_tvg_ids():
    """
    Возвращает {title: tvg_id} или пустой словарь, если PLAYLIST_URL
    не задан, либо плейлист не удалось скачать/разобрать — в этом
    случае скрипт просто продолжает работать по названиям, как раньше.
    """
    playlist_url = os.environ.get(PLAYLIST_URL_ENV, "").strip()

    if not playlist_url:
        print("PLAYLIST_URL не задан — tvg-id не используется, только названия.")
        return {}

    try:
        download_playlist(playlist_url, RAW_PLAYLIST_FILE, MAX_PLAYLIST_SIZE)
        title_to_tvg_id = parse_playlist_tvg_ids(RAW_PLAYLIST_FILE)
        print(f"tvg-id получены для {len(title_to_tvg_id)} каналов плейлиста.")
        return title_to_tvg_id

    except Exception as exc:
        print(f"Не удалось получить tvg-id из плейлиста ({exc}); "
              "продолжаю только по названиям.")
        return {}

    finally:
        if os.path.exists(RAW_PLAYLIST_FILE):
            os.remove(RAW_PLAYLIST_FILE)


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(url, destination):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; EPG-EDEM-lite/1.0)"
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
            raise RuntimeError(f"Слишком маленький файл: {file_size} байт.")

        with open(destination, "rb") as source:
            signature = source.read(2)

        if signature != b"\x1f\x8b":
            raise RuntimeError("Полученный файл не является GZIP-архивом.")

        with gzip.open(destination, "rb") as source:
            while source.read(BUFFER_SIZE):
                pass

        print(f"Скачивание завершено: {file_size / 1024 / 1024:.2f} МБ")

        return True

    except Exception as exc:
        print(f"Ошибка скачивания: {exc}")

        if os.path.exists(destination):
            os.remove(destination)

        return False


# ============================================================
# XMLTV DATETIME
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
        parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        sign = match.group(2)

        if sign:
            hours = int(match.group(3))
            minutes = int(match.group(4))

            if hours > 23 or minutes > 59:
                return None

            offset_minutes = hours * 60 + minutes

            if sign == "-":
                offset_minutes = -offset_minutes

            source_tz = timezone(timedelta(minutes=offset_minutes))

            return parsed.replace(tzinfo=source_tz).astimezone(MOSCOW)

        return parsed.replace(tzinfo=MOSCOW)

    except (ValueError, OverflowError):
        return None


def get_programme_times(element):
    start_value = element.get("start")

    if not start_value:
        return None, None

    start = parse_xmltv_datetime(start_value)

    if start is None:
        return None, None

    stop_value = element.get("stop")

    if stop_value is None:
        return start, None

    stop = parse_xmltv_datetime(stop_value)

    if stop is None:
        return None, None

    if stop <= start:
        return None, None

    return start, stop


def programme_is_in_window(start, stop, window_start, window_end):
    if start is None:
        return False

    if stop is None:
        return window_start <= start < window_end

    return start < window_end and stop > window_start


# ============================================================
# ОЧИСТКА ЭЛЕМЕНТА ПЕРЕДАЧИ
# ============================================================

def remove_unneeded_metadata(element):
    removed = 0

    for child in list(element):
        if local_name(child.tag) in REMOVED_PROGRAMME_TAGS:
            element.remove(child)
            removed += 1

    return removed


def truncate_description(element, limit):
    if limit is None:
        return 0

    for child in element:
        if local_name(child.tag) == "desc":
            text = child.text or ""

            if len(text) > limit:
                child.text = text[:limit]
                return 1

            return 0

    return 0


def get_display_names(element):
    return [
        "".join(child.itertext()).strip()
        for child in element
        if local_name(child.tag) == "display-name"
    ]


# ============================================================
# ОСНОВНАЯ ОБРАБОТКА
# ============================================================

def build_lite_epg(source_path, output_path, reference_titles, title_to_tvg_id=None):

    by_name, by_base_name, by_tvg_id = build_reference_index(
        reference_titles, title_to_tvg_id
    )

    today = datetime.now(MOSCOW).date()

    first_day = today - timedelta(days=DAYS_BEFORE)
    last_day = today + timedelta(days=DAYS_AFTER)

    window_start = datetime.combine(first_day, time.min, tzinfo=MOSCOW)
    window_end = datetime.combine(
        last_day + timedelta(days=1),
        time.min,
        tzinfo=MOSCOW,
    )

    total_window_days = DAYS_BEFORE + DAYS_AFTER + 1

    print()
    print("=" * 55)
    print("EPG-EDEM LITE: отбор по каналам + обрезка дат")
    print("=" * 55)
    print(f"Эталонных каналов (channels_test.txt): {len(reference_titles)}")
    print(f"Из них с tvg-id из плейлиста: {len(by_tvg_id)}")
    print(f"Сегодня по Москве: {today}")
    print(f"Начало периода:    {first_day}")
    print(f"Конец периода:     {last_day}")
    print(f"Продолжительность: {total_window_days} календарных дня(дней)")
    print(f"Обрезка desc: {MAX_DESC_CHARS if MAX_DESC_CHARS else 'выключена'}")

    source_compressed_size = os.path.getsize(source_path)

    xml_channels_seen = 0
    channels_kept = 0
    channels_dropped = 0

    total_programmes = 0
    kept_programmes = 0
    removed_by_window = 0
    removed_unmatched_channel = 0
    invalid_dates = 0
    removed_metadata = 0
    truncated_desc_count = 0

    kept_channel_ids = set()
    matched_reference_titles = set()

    root_element = None
    closing_tag = None
    depth = 0

    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)

    fd, temporary_output = tempfile.mkstemp(
        prefix="epg_lite_",
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

            output.write(b'<?xml version="1.0" encoding="utf-8"?>\n')

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
                        opening_tag, closing_part = serialized_root.rsplit(
                            b"</", 1
                        )
                        output.write(opening_tag)
                        closing_tag = b"</" + closing_part

                    continue

                if depth == 2:

                    tag = local_name(element.tag)
                    keep_element = True

                    if tag == "channel":

                        xml_channels_seen += 1
                        channel_id = element.get("id", "").strip()
                        display_names = get_display_names(element)

                        matched_titles = match_reference_titles(
                            channel_id, display_names, by_name, by_base_name, by_tvg_id
                        )

                        if matched_titles and channel_id:
                            keep_element = True
                            kept_channel_ids.add(channel_id)
                            matched_reference_titles.update(matched_titles)
                            channels_kept += 1
                        else:
                            keep_element = False
                            channels_dropped += 1

                    elif tag == "programme":

                        total_programmes += 1
                        channel_id = element.get("channel", "").strip()

                        if channel_id not in kept_channel_ids:
                            keep_element = False
                            removed_unmatched_channel += 1

                        else:
                            start, stop = get_programme_times(element)

                            if start is None:
                                invalid_dates += 1
                                keep_element = False
                            else:
                                keep_element = programme_is_in_window(
                                    start, stop, window_start, window_end
                                )

                                if not keep_element:
                                    removed_by_window += 1

                            if keep_element:
                                removed_metadata += remove_unneeded_metadata(
                                    element
                                )
                                truncated_desc_count += truncate_description(
                                    element, MAX_DESC_CHARS
                                )
                                kept_programmes += 1

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
                raise ValueError("Не найден корневой элемент XMLTV.")

            output.write(closing_tag)

        # ====================================================
        # ВАЛИДАЦИЯ
        # ====================================================

        print()
        print("Проверка итогового GZIP и XML...")

        checked_elements = 0

        with gzip.open(temporary_output, "rb") as check_file:
            for _, _ in ET.iterparse(check_file, events=("end",)):
                checked_elements += 1

        if checked_elements == 0:
            raise ValueError("Итоговый XML пуст.")

        if channels_kept == 0:
            raise ValueError(
                "Ни один канал источника не сопоставлен с channels_test.txt. "
                "Возможно, изменился формат channels_test.txt или источник недоступен."
            )

        if kept_programmes == 0:
            raise ValueError(
                "После фильтрации по каналам и датам не осталось ни одной "
                "передачи — проверьте окно дат и содержимое исходного EPG."
            )

        os.replace(temporary_output, output_path)

    except Exception:
        if os.path.exists(temporary_output):
            os.remove(temporary_output)
        raise

    # ========================================================
    # ПОКРЫТИЕ КАНАЛОВ (channels_test.txt может меняться)
    # ========================================================

    unmatched_reference_titles = sorted(
        set(reference_titles) - matched_reference_titles
    )
    coverage_ratio = (
        len(matched_reference_titles) / len(reference_titles)
        if reference_titles
        else 0
    )

    coverage_path = os.path.join(
        output_dir or ".", COVERAGE_FILE
    )
    with open(coverage_path, "w", encoding="utf-8") as coverage_file:
        coverage_file.write(
            f"Эталонных каналов: {len(reference_titles)}\n"
            f"Найдено в epg.one: {len(matched_reference_titles)}\n"
            f"Не найдено:        {len(unmatched_reference_titles)}\n"
            f"Покрытие:          {coverage_ratio:.1%}\n\n"
            "КАНАЛЫ ИЗ channels_test.txt БЕЗ СОВПАДЕНИЯ В epg.one:\n"
        )
        for title in unmatched_reference_titles:
            coverage_file.write(f"  {title}\n")

    # ========================================================
    # СТАТИСТИКА
    # ========================================================

    output_compressed_size = os.path.getsize(output_path)

    with gzip.open(output_path, "rb") as output:
        output_uncompressed_size = sum(
            len(chunk)
            for chunk in iter(lambda: output.read(BUFFER_SIZE), b"")
        )

    print()
    print("=" * 55)
    print("РЕЗУЛЬТАТ")
    print("=" * 55)

    print(f"Каналов в источнике:            {xml_channels_seen}")
    print(f"Каналов оставлено:              {channels_kept}")
    print(f"Каналов отброшено (не наши):    {channels_dropped}")
    print()
    print(f"Покрытие channels_test.txt:     {coverage_ratio:.1%} "
          f"({len(matched_reference_titles)}/{len(reference_titles)})")
    print(f"Отчёт по непокрытым каналам:    {coverage_path}")
    print()
    print(f"Всего передач в источнике:      {total_programmes}")
    print(f"Передач сохранено:              {kept_programmes}")
    print(f"Удалено (канал не наш):         {removed_unmatched_channel}")
    print(f"Удалено (вне окна дат):         {removed_by_window}")
    print(f"Передач с некорректной датой:   {invalid_dates}")
    print(f"Удалено доп. тегов:             {removed_metadata}")
    print(f"Обрезано длинных desc:          {truncated_desc_count}")

    print()
    print("РАЗМЕРЫ")
    print(f"Исходный GZIP:  {source_compressed_size / 1024 / 1024:.2f} МБ")
    print(f"Итоговый GZIP:  {output_compressed_size / 1024 / 1024:.2f} МБ")
    print(f"Итоговый XML:   {output_uncompressed_size / 1024 / 1024:.2f} МБ")

    if source_compressed_size:
        reduction = (
            1 - output_compressed_size / source_compressed_size
        ) * 100
        print(f"Изменение размера относительно исходного GZIP: {reduction:.1f}%")

    print()
    print(f"Готово: {output_path}")
    print("=" * 55)


# ============================================================
# MAIN
# ============================================================

def main():
    reference_titles = load_reference_channels(REFERENCE_FILE)
    title_to_tvg_id = load_playlist_tvg_ids()

    print(f"Скачивание EPG: {EPG_URL}")

    if not download_file(EPG_URL, RAW_FILE):
        raise RuntimeError(
            "Не удалось скачать или проверить исходный EPG. "
            "Итоговый файл не изменён."
        )

    try:
        build_lite_epg(RAW_FILE, OUTPUT_FILE, reference_titles, title_to_tvg_id)
    finally:
        if os.path.exists(RAW_FILE):
            os.remove(RAW_FILE)


if __name__ == "__main__":
    main()
