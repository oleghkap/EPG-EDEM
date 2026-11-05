#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EPG-EDEM: ручная проверка покрытия персонального M3U
Результаты:
  channels_test.txt
  coverage_report.csv
  channel_coverage.csv
  epg_test_combined.xml.gz

PLAYLIST_URL берётся только из GitHub Secrets.
Персональный URL плейлиста не записывается в результаты.
"""

import csv
import gzip
import os
import re
import sys
import time
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# НАСТРОЙКИ
# ============================================================

TZ = ZoneInfo("Europe/Moscow")

DAYS_BEFORE = 1
DAYS_AFTER = 7

MAX_PLAYLIST_SIZE = 100 * 1024 * 1024
MAX_SOURCE_SIZE = 600 * 1024 * 1024

CONNECT_TIMEOUT = 25
READ_TIMEOUT = 90

USER_AGENT = "VLC/3.0.16"

OUTPUT_DIR = Path(".")

SOURCES = [
    ("EPG.ONE epg2", "http://epg.one/epg2.xml.gz"),
    ("EPG.ONE epg", "http://epg.one/epg.xml.gz"),
    ("EPG.ONE ru2", "http://epg.one/ru2.xml.gz"),
    ("EPG.ONE EDEM", "http://epg.one/edem.xml.gz"),

    ("IPTVX full", "https://iptvx.one/epg/epg.xml.gz"),
    ("IPTVX lite", "https://iptvx.one/epg/epg_lite.xml.gz"),

    ("TV.TEAM full", "http://epg.team/tvteam.xml.3.1"),
    ("TV.TEAM lite", "http://epg.team/tvteam.xml"),

    ("Sharavoz", "http://iptv-content.webhop.net/guide.xml"),

    ("TeleGuide", "http://download.teleguide.su/download/new3/xmltv.xml.gz"),
    ("ProgramTV", "http://programtv.ru/xmltv.xml.gz"),
    ("OTTClub", "https://ottepg.ru/ottepg.xml.gz"),

    ("Shara TV", "http://stb.shara-tv.org/epg/epgtv.xml.gz"),
    ("Kineskop", "http://st.kineskop.tv/epg.xml.gz"),
    ("TopIPTV", "http://topiptv.info/download/topiptv.xml.gz"),

    ("Great IPTV", "https://epg.greatiptv.cc/iptv.xml.gz"),
    ("IPTVXTV", "http://epg.iptvx.tv/xmltv.xml.gz"),

    ("M3U Edit ALL", "https://m3u-edit.com/epg-source.php?file=ALL_SOURCES1.xml.gz"),
]


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def log(message=""):
    print(message, flush=True)


def normalize_name(value):
    """Нормализация имени без удаления региональных суффиксов +2 и т.п."""
    value = (value or "").lower().strip()
    value = value.replace("ё", "е")
    value = re.sub(r"\b(ultra hd|full hd|fhd|uhd|4k|hevc|h\.?265|h\.?264|hd|sd)\b", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def quality_normalize(value):
    """Дополнительная нормализация для сопоставления HD/SD-вариантов."""
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(
        r"\b(ultra hd|full hd|fhd|uhd|4k|hevc|h\.?265|h\.?264|hd|sd)\b",
        " ",
        value,
    )
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def local_period():
    now = datetime.now(TZ)
    today = now.date()

    start = datetime.combine(
        today - timedelta(days=DAYS_BEFORE),
        datetime.min.time(),
        tzinfo=TZ,
    )

    end = datetime.combine(
        today + timedelta(days=DAYS_AFTER + 1),
        datetime.min.time(),
        tzinfo=TZ,
    )

    return start, end


def parse_xmltv_datetime(value):
    """Разбор XMLTV datetime с часовым поясом."""
    if not value:
        return None

    value = value.strip()

    match = re.match(
        r"^(\d{14})(?:\s*([+-]\d{4}))?$",
        value,
    )

    if not match:
        return None

    try:
        dt = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        offset = match.group(2)

        if offset:
            sign = 1 if offset[0] == "+" else -1
            hours = int(offset[1:3])
            minutes = int(offset[3:5])

            if hours > 23 or minutes > 59:
                return None

            from datetime import timezone

            delta = timedelta(hours=hours, minutes=minutes) * sign
            dt = dt.replace(tzinfo=timezone(delta))
            return dt.astimezone(TZ)

        return dt.replace(tzinfo=TZ)

    except (ValueError, OverflowError):
        return None


def download_to_file(url, destination, max_size):
    """Скачивание с ограничением размера и без отключения TLS-проверки."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )

    total = 0

    with urllib.request.urlopen(
        request,
        timeout=CONNECT_TIMEOUT,
    ) as response:
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)

                if not chunk:
                    break

                total += len(chunk)

                if total > max_size:
                    raise RuntimeError("download size limit exceeded")

                output.write(chunk)

    if total == 0:
        raise RuntimeError("empty response")

    return total


def open_xml_file(path):
    """Определение gzip по сигнатуре, а не по расширению."""
    with open(path, "rb") as file:
        magic = file.read(2)

    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")

    return open(path, "rb")


# ============================================================
# РАЗБОР M3U
# ============================================================

def split_extinf(line):
    """Разделяет EXTINF по первой запятой вне кавычек."""
    quoted = False

    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return line[:index], line[index + 1:].strip()

    return line, ""


def parse_attributes(value):
    result = {}

    pattern = r'([\w-]+)\s*=\s*"([^"]*)"|([\w-]+)\s*=\s*([^\s,]+)'

    for match in re.finditer(pattern, value):
        if match.group(1):
            result[match.group(1).lower()] = match.group(2)
        elif match.group(3):
            result[match.group(3).lower()] = match.group(4)

    return result


def parse_m3u(data):
    text = data.decode("utf-8-sig", errors="replace")

    channels = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF:"):
            header, title = split_extinf(line[8:])
            attrs = parse_attributes(header)

            name = title.strip()

            if not name:
                name = attrs.get("tvg-name", "").strip()

            if not name:
                continue

            current = {
                "title": name,
                "tvg_id": attrs.get("tvg-id", "").strip(),
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "group": attrs.get("group-title", "").strip(),
            }

        elif line and not line.startswith("#") and current is not None:
            # URL потока намеренно не сохраняется.
            channels.append(current)
            current = None

    # Убираем только полные повторы title + tvg-id.
    unique = []
    seen = set()

    for channel in channels:
        key = (
            channel["title"].casefold(),
            channel["tvg_id"].casefold(),
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(channel)

    return unique


def load_playlist():
    playlist_url = os.environ.get("PLAYLIST_URL", "").strip()

    if not playlist_url:
        raise RuntimeError("PLAYLIST_URL secret is missing")

    log("Загрузка персонального M3U...")

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name

    try:
        size = download_to_file(
            playlist_url,
            temp_path,
            MAX_PLAYLIST_SIZE,
        )

        with open(temp_path, "rb") as file:
            data = file.read()

        channels = parse_m3u(data)

        if not channels:
            raise RuntimeError("no channels found in playlist")

        log(f"Каналов в M3U после удаления полных повторов: {len(channels)}")
        return channels

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


# ============================================================
# СОПОСТАВЛЕНИЕ КАНАЛОВ
# ============================================================

def build_playlist_indexes(channels):
    by_id = defaultdict(set)
    by_name = defaultdict(set)
    by_quality_name = defaultdict(set)

    for index, channel in enumerate(channels):
        channel["key"] = f"playlist_{index + 1}"

        if channel["tvg_id"]:
            by_id[channel["tvg_id"].casefold()].add(index)

        names = {
            channel["title"],
            channel["tvg_name"],
        }

        for name in names:
            if not name:
                continue

            normalized = normalize_name(name)
            quality = quality_normalize(name)

            if normalized:
                by_name[normalized].add(index)

            if quality:
                by_quality_name[quality].add(index)

    return by_id, by_name, by_quality_name


def map_source_channel(
    channel_id,
    display_names,
    indexes,
):
    by_id, by_name, by_quality_name = indexes

    if channel_id:
        matches = by_id.get(channel_id.casefold(), set())

        if len(matches) == 1:
            return next(iter(matches)), "tvg-id"

        if len(matches) > 1:
            return None, "ambiguous"

    for name in display_names:
        normalized = normalize_name(name)

        if normalized:
            matches = by_name.get(normalized, set())

            if len(matches) == 1:
                return next(iter(matches)), "exact name"

            if len(matches) > 1:
                return None, "ambiguous"

    for name in display_names:
        quality = quality_normalize(name)

        if quality:
            matches = by_quality_name.get(quality, set())

            if len(matches) == 1:
                return next(iter(matches)), "quality fallback"

            if len(matches) > 1:
                return None, "ambiguous"

    return None, "unmatched"


# ============================================================
# ОБРАБОТКА XMLTV
# ============================================================

def element_text(parent, tag):
    element = parent.find(tag)

    if element is None:
        return ""

    return "".join(element.itertext()).strip()


def process_source(
    source_name,
    source_url,
    playlist_channels,
    indexes,
    period_start,
    period_end,
    global_programmes,
):
    stats = {
        "source": source_name,
        "status": "error",
        "xml_channels": 0,
        "matched_playlist_channels": 0,
        "channels_with_programmes": 0,
        "programmes_read": 0,
        "programmes_in_period": 0,
        "unique_programmes": 0,
        "duplicates_skipped": 0,
        "ambiguous_channels": 0,
        "unmatched_channels": 0,
        "first_programme": "",
        "last_programme": "",
    }

    mapped = {}
    channels_with_programmes = set()
    source_programmes = set()

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name

    try:
        log(f"  Скачивание: {source_name}")

        size = download_to_file(
            source_url,
            temp_path,
            MAX_SOURCE_SIZE,
        )

        log(f"  Получено: {size / 1024 / 1024:.1f} MB")

        with open_xml_file(temp_path) as xml_file:
            context = ET.iterparse(xml_file, events=("start", "end"))
            root = None

            for event, elem in context:
                tag = elem.tag.split("}")[-1]

                if event == "start":
                    if root is None:
                        root = elem
                    continue

                if tag == "channel":
                    stats["xml_channels"] += 1

                    channel_id = elem.attrib.get("id", "").strip()

                    display_names = [
                        "".join(item.itertext()).strip()
                        for item in elem.findall("display-name")
                    ]

                    index, method = map_source_channel(
                        channel_id,
                        display_names,
                        indexes,
                    )

                    if index is not None:
                        mapped[channel_id] = index
                    elif method == "ambiguous":
                        stats["ambiguous_channels"] += 1
                    else:
                        stats["unmatched_channels"] += 1

                elif tag == "programme":
                    stats["programmes_read"] += 1

                    channel_id = elem.attrib.get("channel", "").strip()
                    index = mapped.get(channel_id)

                    if index is not None:
                        start_dt = parse_xmltv_datetime(
                            elem.attrib.get("start", "")
                        )

                        stop_dt = parse_xmltv_datetime(
                            elem.attrib.get("stop", "")
                        )

                        if start_dt is not None:
                            if period_start <= start_dt < period_end:
                                title = element_text(elem, "title")

                                if title:
                                    description = element_text(elem, "desc")

                                    category = [
                                        "".join(item.itertext()).strip()
                                        for item in elem.findall("category")
                                    ]

                                    stop_text = (
                                        stop_dt.isoformat()
                                        if stop_dt is not None
                                        else ""
                                    )

                                    identity = (
                                        index,
                                        start_dt.isoformat(),
                                        stop_text,
                                        normalize_name(title),
                                    )

                                    stats["programmes_in_period"] += 1
                                    channels_with_programmes.add(index)

                                    if identity in global_programmes:
                                        stats["duplicates_skipped"] += 1
                                    else:
                                        global_programmes[identity] = {
                                            "channel_index": index,
                                            "start": start_dt,
                                            "stop": stop_dt,
                                            "title": title,
                                            "description": description,
                                            "categories": category,
                                            "source": source_name,
                                        }

                                        stats["unique_programmes"] += 1
                                        source_programmes.add(identity)

                                    current_date = start_dt.strftime(
                                        "%Y-%m-%d %H:%M"
                                    )

                                    if not stats["first_programme"]:
                                        stats["first_programme"] = current_date

                                    stats["last_programme"] = current_date

                if tag in ("channel", "programme"):
                    elem.clear()

                if root is not None:
                    root.clear()
                  
        stats["matched_playlist_channels"] = len(set(mapped.values()))
        stats["channels_with_programmes"] = len(channels_with_programmes)
        stats["status"] = "OK"

        log(
            f"  Результат: каналов с программами "
            f"{stats['channels_with_programmes']}, "
            f"уникальных передач {stats['unique_programmes']}, "
            f"дубликатов {stats['duplicates_skipped']}"
        )

    except Exception as error:
        # Не выводим текст исключения: в нём потенциально могут быть URL.
        stats["status"] = "ERROR: download or XML parsing failed"
        log("  Ошибка загрузки или разбора XMLTV.")

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return stats


# ============================================================
# СОХРАНЕНИЕ РЕЗУЛЬТАТОВ
# ============================================================

def write_channels(channels):
    path = OUTPUT_DIR / "channels_test.txt"

    with open(path, "w", encoding="utf-8") as file:
        for channel in channels:
            file.write(channel["title"] + "\n")


def write_coverage_report(stats_list):
    path = OUTPUT_DIR / "coverage_report.csv"

    fields = [
        "source",
        "status",
        "xml_channels",
        "matched_playlist_channels",
        "channels_with_programmes",
        "programmes_read",
        "programmes_in_period",
        "unique_programmes",
        "duplicates_skipped",
        "ambiguous_channels",
        "unmatched_channels",
        "first_programme",
        "last_programme",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(stats_list)


def write_channel_report(channels, global_programmes):
    path = OUTPUT_DIR / "channel_coverage.csv"

    by_channel = defaultdict(lambda: {
        "programme_count": 0,
        "sources": set(),
        "first": None,
        "last": None,
    })

    for programme in global_programmes.values():
        index = programme["channel_index"]
        item = by_channel[index]

        item["programme_count"] += 1
        item["sources"].add(programme["source"])

        start = programme["start"]

        if item["first"] is None or start < item["first"]:
            item["first"] = start

        if item["last"] is None or start > item["last"]:
            item["last"] = start

    fields = [
        "channel",
        "tvg_id",
        "group",
        "programme_count",
        "sources",
        "first_programme",
        "last_programme",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for index, channel in enumerate(channels):
            item = by_channel[index]

            writer.writerow({
                "channel": channel["title"],
                "tvg_id": channel["tvg_id"],
                "group": channel["group"],
                "programme_count": item["programme_count"],
                "sources": "; ".join(sorted(item["sources"])),
                "first_programme": (
                    item["first"].strftime("%Y-%m-%d %H:%M")
                    if item["first"] else ""
                ),
                "last_programme": (
                    item["last"].strftime("%Y-%m-%d %H:%M")
                    if item["last"] else ""
                ),
            })


def write_combined_xml(channels, global_programmes):
    path = OUTPUT_DIR / "epg_test_combined.xml.gz"

    root = ET.Element("tv", {
        "generator-info-name": "EPG-EDEM Coverage Test",
    })

    for index, channel in enumerate(channels):
        channel_element = ET.SubElement(
            root,
            "channel",
            {"id": f"playlist_{index + 1}"},
        )

        ET.SubElement(
            channel_element,
            "display-name",
        ).text = channel["title"]

    ordered = sorted(
        global_programmes.values(),
        key=lambda item: (
            item["channel_index"],
            item["start"],
            item["title"].casefold(),
        ),
    )

    for programme in ordered:
        element = ET.SubElement(
            root,
            "programme",
            {
                "start": programme["start"].strftime("%Y%m%d%H%M%S %z"),
                "channel": f"playlist_{programme['channel_index'] + 1}",
            },
        )

        if programme["stop"] is not None:
            element.set(
                "stop",
                programme["stop"].strftime("%Y%m%d%H%M%S %z"),
            )

        ET.SubElement(element, "title").text = programme["title"]

        if programme["description"]:
            ET.SubElement(
                element,
                "desc",
            ).text = programme["description"]

        for category in programme["categories"]:
            if category:
                ET.SubElement(
                    element,
                    "category",
                ).text = category

    xml_data = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

    with gzip.open(path, "wb", compresslevel=6) as file:
        file.write(xml_data)


# ============================================================
# MAIN
# ============================================================

def main():
    started = time.time()

    log("=" * 65)
    log("EPG-EDEM: ПРОВЕРКА ПОКРЫТИЯ XMLTV ИСТОЧНИКОВ")
    log("=" * 65)

    period_start, period_end = local_period()

    log(f"Московское начало периода: {period_start}")
    log(f"Московский конец периода:  {period_end}")
    log(f"Количество источников: {len(SOURCES)}")
    log()

    channels = load_playlist()
    write_channels(channels)

    indexes = build_playlist_indexes(channels)

    global_programmes = {}
    all_stats = []

    for number, (source_name, source_url) in enumerate(SOURCES, start=1):
        log()
        log("-" * 65)
        log(f"[{number}/{len(SOURCES)}] {source_name}")
        log("-" * 65)

        stats = process_source(
            source_name,
            source_url,
            channels,
            indexes,
            period_start,
            period_end,
            global_programmes,
        )

        all_stats.append(stats)

    write_coverage_report(all_stats)
    write_channel_report(channels, global_programmes)
    write_combined_xml(channels, global_programmes)

    covered_channels = {
        programme["channel_index"]
        for programme in global_programmes.values()
    }

    log()
    log("=" * 65)
    log("ИТОГОВЫЕ РЕЗУЛЬТАТЫ")
    log("=" * 65)

    log(f"Каналов в плейлисте: {len(channels)}")
    log(f"Каналов с программами хотя бы из одного источника: {len(covered_channels)}")
    log(f"Каналов без программ: {len(channels) - len(covered_channels)}")
    log(f"Уникальных передач в объединённом EPG: {len(global_programmes)}")

    log()
    log("Созданы файлы:")
    log("  channels_test.txt")
    log("  coverage_report.csv")
    log("  channel_coverage.csv")
    log("  epg_test_combined.xml.gz")

    log()
    log(f"Время выполнения: {(time.time() - started) / 60:.1f} мин.")

    if not any(item["status"] == "OK" for item in all_stats):
        log("ВНИМАНИЕ: ни один источник не обработан успешно.")
        sys.exit(1)


if __name__ == "__main__":
    main()
