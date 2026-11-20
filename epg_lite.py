#!/usr/bin/env python3

"""
EPG-EDEM lite.

Полностью независимый скрипт: ничего не читает и не пишет из/в файлы
clean_epg.py и trim_epg.py (ни channels_test.txt, ни epg_custom.xml.gz,
ни epg_trimmed.xml.gz), и наоборот — ни один из них не трогает файлы
этого скрипта. Каждый скрипт может быть удалён независимо от других.

Что делает:
  1. Сам скачивает ваш M3U-плейлист (PLAYLIST_URL) и строит из него
     СОБСТВЕННЫЙ список каналов — channels_lite.txt, формат:
         № | CHANNEL | TVG-ID | GRP
     При первом запуске файл создаётся. При каждом следующем запуске
     плейлист скачивается заново и сравнивается с уже сохранённым
     channels_lite.txt: если ничего не изменилось — файл не
     перезаписывается; если состав/tvg-id/группы изменились —
     перезаписывается целиком.
  2. Скачивает ТОЛЬКО epg.one (единственный источник EPG).
  3. Оставляет только те каналы источника, которые реально есть в
     channels_lite.txt. Порядок сопоставления:
       a) tvg-id из channels_lite.txt == id канала в epg.one (вы сами
          прописываете в плейлисте tvg-id именно от epg.one — самый
          надёжный способ, используется первым, без эвристик);
       b) точное совпадение названия;
       c) совпадение по базовому названию без HD/FHD/UHD/4K с учётом
          страны (без слияния разных регионов/версий).
     UHD и 4K считаются одним и тем же качеством (QUALITY_EQUIVALENTS) —
     иначе каналы 4K/UHD часто теряются из-за разной маркировки
     в плейлисте и в epg.one.
  4. Для оставшихся каналов обрезает программы по датам
     (DAYS_BEFORE/DAYS_AFTER вокруг текущего дня по Москве — сейчас
     3 дня: вчера/сегодня/завтра).
  5. Удаляет второстепенные теги передач (credits, rating, country...)
     и логотип канала (<icon> внутри <channel> — свои логотипы и так
     подтягиваются из плейлиста).
  6. Обрезает длину <desc> до MAX_DESC_CHARS (сейчас 350 символов).
  7. Заменяет внутренний id канала epg.one (длинный, повторяется в
     каждой передаче) на короткий синтетический ("c1", "c2"...) — это
     не ваш tvg-id и не влияет на матчинг, чисто экономия места.
  8. Каналы без единой передачи в итоговом окне дат ОСТАЮТСЯ в выводе
     (как есть сейчас у вас с epg.one) — просто отдельно считаются и
     показываются в статистике и в COVERAGE_FILE, чтобы было видно
     масштаб проблемы, без выбрасывания таких каналов.
"""

import gzip
import os
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

EPG_URL = "http://epg.one/epg2.xml.gz"

PLAYLIST_URL_ENV = "PLAYLIST_URL"
MAX_PLAYLIST_SIZE = 50 * 1024 * 1024
RAW_PLAYLIST_FILE = "raw_playlist_lite.m3u"

REFERENCE_FILE = "channels_lite.txt"

OUTPUT_FILE = "epg_lite.xml.gz"
RAW_FILE = "raw_epg_lite.xml.gz"
COVERAGE_FILE = "epg_lite_coverage.txt"

DAYS_BEFORE = 1
DAYS_AFTER = 1

# Обрезка длинных описаний. None — не обрезать.
# По диагностике clean.py: desc — обычно 70-85% объёма несжатого XML,
# обрезка до 300-500 символов даёт заметную экономию при небольшой потере текста.
MAX_DESC_CHARS = 350

# Теги channel-level логотипа (не путать с tvg-id!) — свои логотипы и так
# подтягиваются из плейлиста, поэтому логотип из epg.one не нужен.
REMOVED_CHANNEL_TAGS = {
    "icon",
}

# Короткие синтетические channel-id вместо длинных внутренних id epg.one.
# Экономия: оригинальный id повторяется в КАЖДОЙ передаче каждого канала
# (десятки раз за 3 дня), короткий id "c1", "c2"... заметно меньше.
# На матчинг по вашему tvg-id это не влияет — подмена чисто техническая,
# происходит только при записи итогового файла.
SHORT_CHANNEL_ID_PREFIX = "c"

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
# с гарантией единственного правильного ID. tvg-id из channels_lite.txt
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
# СОБСТВЕННЫЙ СПИСОК КАНАЛОВ: channels_lite.txt
# (№ | CHANNEL | TVG-ID | GRP) — строится и обновляется этим же
# скриптом из вашего M3U-плейлиста, без обращения к channels_test.txt
# или к любому другому файлу clean_epg.py/trim_epg.py.
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


def parse_m3u_playlist(path):
    """
    Разбирает #EXTINF-записи M3U: название, tvg-id, группа (group-title,
    либо предшествующий #EXTGRP). Независимая реализация — не делит код
    с clean_epg.py, чтобы скрипты не зависели друг от друга.
    """
    raw = Path(path).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1251")

    lines = content.splitlines()
    attribute_re = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

    channels = []
    current_group = ""
    pending_index = None

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTGRP:"):
            group = line.partition(":")[2].strip()
            if pending_index is not None and not channels[pending_index]["group_from_attribute"]:
                channels[pending_index]["group"] = group
            current_group = group
            continue

        if line.startswith("#EXTINF:"):
            separator = find_m3u_title_separator(line)
            if separator < 0:
                continue

            metadata = line[len("#EXTINF:"):separator]
            title = line[separator + 1:].strip()
            if not title:
                continue

            attributes = {
                key.casefold(): value.strip()
                for key, value in attribute_re.findall(metadata)
            }
            group_title = attributes.get("group-title", "")
            group = group_title or current_group
            tvg_id = attributes.get("tvg-id", "").strip()

            channels.append({
                "title": title.replace("|", "｜"),
                "tvg_id": tvg_id.replace("|", "｜"),
                "group": group.replace("|", "｜"),
                "group_from_attribute": bool(group_title),
            })
            pending_index = len(channels) - 1
            continue

        if not line.startswith("#"):
            pending_index = None

    if not channels:
        raise RuntimeError("playlist contains no #EXTINF channel records")

    return channels


def render_reference_channels(channels):
    """Текстовая таблица channels_lite.txt: № | CHANNEL | TVG-ID | GRP."""
    lines = [
        "№    | CHANNEL                                 | TVG-ID                           | GRP            ",
        "-----+-----------------------------------------+----------------------------------+----------------",
    ]
    for number, channel in enumerate(channels, start=1):
        title = channel["title"].strip()
        tvg_id = (channel.get("tvg_id") or "").strip()
        group = (channel.get("group") or "").strip() or "Без категории"
        lines.append(f"{number:<5}| {title:<39} | {tvg_id:<32} | {group:<14}")
    return "\n".join(lines) + "\n"


def sync_reference_channels():
    """
    Создаёт channels_lite.txt при первом запуске. При следующих запусках
    скачивает плейлист заново и перезаписывает файл только если состав
    каналов/tvg-id/групп реально изменился — иначе оставляет файл как есть
    (не плодим лишние изменения в git).
    """
    path = Path(REFERENCE_FILE)
    playlist_url = os.environ.get(PLAYLIST_URL_ENV, "").strip()

    if not playlist_url:
        if path.is_file():
            print(f"{PLAYLIST_URL_ENV} не задан — использую существующий {REFERENCE_FILE} без обновления.")
            return
        raise RuntimeError(
            f"{PLAYLIST_URL_ENV} не задан, а {REFERENCE_FILE} ещё не существует — "
            "для первого запуска нужен доступ к плейлисту."
        )

    try:
        download_playlist(playlist_url, RAW_PLAYLIST_FILE, MAX_PLAYLIST_SIZE)
        channels = parse_m3u_playlist(RAW_PLAYLIST_FILE)
    except Exception as exc:
        if path.is_file():
            print(f"Не удалось обновить {REFERENCE_FILE} из плейлиста ({exc}); использую прежний список.")
            return
        raise RuntimeError(f"Не удалось получить список каналов из плейлиста: {exc}") from exc
    finally:
        if os.path.exists(RAW_PLAYLIST_FILE):
            os.remove(RAW_PLAYLIST_FILE)

    updated_content = render_reference_channels(channels)
    previous_content = path.read_text(encoding="utf-8-sig") if path.exists() else None

    if previous_content == updated_content:
        print(f"Плейлист: {len(channels)} каналов; {REFERENCE_FILE} уже актуален, не перезаписан.")
        return

    temporary_path = path.with_name(path.name + ".pending")
    try:
        temporary_path.write_text(updated_content, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    print(f"Плейлист: {len(channels)} каналов; {REFERENCE_FILE} обновлён (были изменения).")


def load_reference_channels(path):
    """Читает channels_lite.txt обратно: [{title, tvg_id, group}, ...]."""
    file_path = Path(path)

    if not file_path.is_file():
        raise RuntimeError(f"Не найден {path} — sync_reference_channels должен был его создать.")

    channels = []

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

            channels.append({
                "title": columns[1],
                "tvg_id": columns[2] if len(columns) > 2 else "",
                "group": columns[3] if len(columns) > 3 else "",
            })

    if not channels:
        raise RuntimeError(f"{path} не содержит ни одной строки с каналом.")

    return channels


def build_reference_index(channels):
    by_name = {}
    by_base_name = {}
    by_tvg_id = {}

    for channel in channels:
        title = channel["title"]

        normalized = normalize_name(title)
        if normalized:
            by_name.setdefault(normalized, []).append(title)

        countryless = strip_country_suffix(strip_country_prefix(title))
        base = normalize_base_name(countryless)
        if base:
            by_base_name.setdefault(base, []).append(title)

        tvg_id = (channel.get("tvg_id") or "").strip()
        if tvg_id:
            by_tvg_id.setdefault(tvg_id.casefold(), []).append(title)

    return by_name, by_base_name, by_tvg_id


def match_reference_titles(channel_id, display_names, by_name, by_base_name, by_tvg_id):
    """
    Возвращает множество названий из channels_lite.txt, которым
    соответствует канал источника (может быть больше одного —
    это нормально и безопасно для простого отбора "нужен / не нужен").

    Порядок проверки:
      1. tvg-id из channels_lite.txt == id канала в epg.one (вы сами
         прописываете в плейлисте tvg-id именно от epg.one — самый
         надёжный способ, без эвристик).
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
# DOWNLOAD EPG
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


def remove_unneeded_channel_metadata(element):
    removed = 0

    for child in list(element):
        if local_name(child.tag) in REMOVED_CHANNEL_TAGS:
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

def build_lite_epg(source_path, output_path, reference_channels):

    reference_titles = [channel["title"] for channel in reference_channels]
    by_name, by_base_name, by_tvg_id = build_reference_index(reference_channels)

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
    print(f"Эталонных каналов ({REFERENCE_FILE}): {len(reference_titles)}")
    print(f"Из них с tvg-id: {len(by_tvg_id)}")
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
    channel_id_to_titles = {}
    channels_with_kept_programme = set()
    short_channel_ids = {}
    removed_channel_logos = 0

    # Диагностика: какие теги реально остаются в итоговых передачах и
    # сколько байт они занимают — чтобы решить, что ещё можно обрезать,
    # опираясь на реальные цифры, а не на предположения.
    programme_tag_stats = defaultdict(lambda: {"count": 0, "bytes": 0})

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
                            channel_id_to_titles[channel_id] = matched_titles
                            matched_reference_titles.update(matched_titles)
                            channels_kept += 1

                            short_id = f"{SHORT_CHANNEL_ID_PREFIX}{len(short_channel_ids) + 1}"
                            short_channel_ids[channel_id] = short_id
                            element.set("id", short_id)
                            removed_channel_logos += remove_unneeded_channel_metadata(element)
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
                                channels_with_kept_programme.add(channel_id)
                                element.set("channel", short_channel_ids[channel_id])

                                for child in element:
                                    child_tag = local_name(child.tag)
                                    child_bytes = len(
                                        ET.tostring(child, encoding="utf-8")
                                    )
                                    programme_tag_stats[child_tag]["count"] += 1
                                    programme_tag_stats[child_tag]["bytes"] += child_bytes

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
                f"Ни один канал источника не сопоставлен с {REFERENCE_FILE}. "
                "Возможно, изменился формат файла или источник недоступен."
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
    # ПОКРЫТИЕ КАНАЛОВ + КАНАЛЫ БЕЗ ПЕРЕДАЧ
    # ========================================================

    unmatched_reference_titles = sorted(
        set(reference_titles) - matched_reference_titles
    )
    coverage_ratio = (
        len(matched_reference_titles) / len(reference_titles)
        if reference_titles
        else 0
    )

    channel_ids_without_programmes = kept_channel_ids - channels_with_kept_programme
    titles_without_programmes = sorted({
        title
        for channel_id in channel_ids_without_programmes
        for title in channel_id_to_titles.get(channel_id, ())
    })

    coverage_path = os.path.join(
        output_dir or ".", COVERAGE_FILE
    )
    with open(coverage_path, "w", encoding="utf-8") as coverage_file:
        coverage_file.write(
            f"Эталонных каналов: {len(reference_titles)}\n"
            f"Найдено в epg.one: {len(matched_reference_titles)}\n"
            f"Не найдено:        {len(unmatched_reference_titles)}\n"
            f"Покрытие:          {coverage_ratio:.1%}\n\n"
            f"Каналов перенесено:        {channels_kept}\n"
            f"  из них без передач:      {len(channel_ids_without_programmes)}\n\n"
            f"КАНАЛЫ ИЗ {REFERENCE_FILE} БЕЗ СОВПАДЕНИЯ В epg.one:\n"
        )
        for title in unmatched_reference_titles:
            coverage_file.write(f"  {title}\n")

        coverage_file.write(
            "\nПЕРЕНЕСЁННЫЕ КАНАЛЫ БЕЗ ЕДИНОЙ ПЕРЕДАЧИ В ЭТОМ ОКНЕ ДАТ:\n"
        )
        for title in titles_without_programmes:
            coverage_file.write(f"  {title}\n")

        total_tag_bytes = sum(
            stats["bytes"] for stats in programme_tag_stats.values()
        ) or 1

        coverage_file.write(
            "\nТЕГИ ВНУТРИ ИТОГОВЫХ ПЕРЕДАЧ (что ещё можно резать) — "
            "уже ПОСЛЕ удаления credits/rating/country/date/episode-num/url/icon "
            "и обрезки desc до {limit} символов:\n".format(
                limit=MAX_DESC_CHARS if MAX_DESC_CHARS else "∞"
            )
        )
        coverage_file.write(
            f"{'тег':<16}{'встреч':>10}{'байт всего':>14}{'байт/шт':>12}{'% объёма':>11}\n"
        )
        for tag_name, stats in sorted(
            programme_tag_stats.items(), key=lambda item: item[1]["bytes"], reverse=True
        ):
            average_bytes = stats["bytes"] / stats["count"] if stats["count"] else 0
            share = stats["bytes"] / total_tag_bytes
            coverage_file.write(
                f"{tag_name:<16}{stats['count']:>10}{stats['bytes']:>14}"
                f"{average_bytes:>12.1f}{share:>10.1%}\n"
            )

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
    print(f"  из них без передач:           {len(channel_ids_without_programmes)}")
    print(f"Каналов отброшено (не наши):    {channels_dropped}")
    print(f"Убрано логотипов каналов:       {removed_channel_logos}")
    print()
    print(f"Покрытие {REFERENCE_FILE}:     {coverage_ratio:.1%} "
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
    sync_reference_channels()
    reference_channels = load_reference_channels(REFERENCE_FILE)

    print(f"Скачивание EPG: {EPG_URL}")

    if not download_file(EPG_URL, RAW_FILE):
        raise RuntimeError(
            "Не удалось скачать или проверить исходный EPG. "
            "Итоговый файл не изменён."
        )

    try:
        build_lite_epg(RAW_FILE, OUTPUT_FILE, reference_channels)
    finally:
        if os.path.exists(RAW_FILE):
            os.remove(RAW_FILE)


if __name__ == "__main__":
    main()
