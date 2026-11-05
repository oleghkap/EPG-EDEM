#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EPG-EDEM: ручная проверка покрытия персонального M3U

Результаты:
  channels_test.txt
  playlist_duplicates.csv
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
import socket
import sys
import time
import shutil
import tempfile
import urllib.request
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# НАСТРОЙКИ
# ============================================================

TZ = ZoneInfo("Europe/Moscow")

DAYS_BEFORE = 2
DAYS_AFTER = 1

MAX_PLAYLIST_SIZE = 100 * 1024 * 1024
MAX_SOURCE_SIZE = 600 * 1024 * 1024

CONNECT_TIMEOUT = 25
READ_TIMEOUT = 90
DOWNLOAD_RETRIES = 3

USER_AGENT = "VLC/3.0.16"

OUTPUT_DIR = Path(".")

SOURCES = [
    ("EPG.ONE epg2", "http://epg.one/epg2.xml.gz"),
    ("IPTVX full", "http://iptvx.one/epg/epg.xml.gz"),
    ("TV.TEAM full", "http://epg.team/tvteam.xml.3.1"),
    ("ProgramTV", "http://programtv.ru/xmltv.xml.gz"),
    ("TopIPTV", "http://topiptv.info/download/topiptv.xml.gz"),
    ("BCU Media", "https://epg.bcumedia.pro/epg.xml.gz"),
    ("FOX TV", "https://epg.fox-tv.fun/1.xmltv"),
    ("Satfor", "https://list.satfor.pro/f/epg.xml.gz"),
    ("M3U Edit ALL", "http://m3u-edit.com/epg-source.php?file=ALL_SOURCES1.xml.gz"),
]


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def log(message=""):
    print(message, flush=True)


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
    """Нормализация без удаления HD/FHD/SD/4K и суффиксов +2/+4."""
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"\+\s*(\d+)\b", r" plus \1 ", value)
    value = value.replace("+", " plus ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_base_name(value):
    """Базовое имя канала без маркеров качества и варианта orig."""
    value = re.sub(
        r"\b(?:hd|fhd|uhd)\s+(?:50|60)\b",
        " ",
        value or "",
        flags=re.IGNORECASE,
    )
    return normalize_name(QUALITY_RE.sub(" ", value))


def has_quality_marker(value):
    return bool(QUALITY_RE.search(value or ""))


def strip_country_prefix(value):
    return COUNTRY_PREFIX_RE.sub("", value or "").strip()


def strip_country_suffix(value):
    return COUNTRY_SUFFIX_RE.sub("", value or "").strip()

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


def safe_error_message(error):
    """Диагностика без вывода URL и возможных секретов."""
    if isinstance(error, HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, socket.gaierror):
            return "DNS lookup failed"
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "network timeout"
        return f"network error ({type(reason).__name__})"
    if isinstance(error, ET.ParseError):
        line, column = error.position
        return f"XML parse error at line {line}, column {column}"
    if isinstance(error, gzip.BadGzipFile):
        return "invalid gzip data"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "network timeout"
    return type(error).__name__


def download_to_file(url, destination, max_size):
    """Скачивание с отдельными connect/read timeout и повторами."""
    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            total = 0

            with urllib.request.urlopen(
                request,
                timeout=CONNECT_TIMEOUT,
            ) as response:
                raw = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock is not None:
                    sock.settimeout(READ_TIMEOUT)

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

        except HTTPError as error:
            if 400 <= error.code < 500:
                raise
            last_error = error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error

        if attempt < DOWNLOAD_RETRIES:
            log(
                f"  Временная ошибка загрузки: {safe_error_message(last_error)}. "
                f"Повтор {attempt + 1}/{DOWNLOAD_RETRIES}..."
            )
            time.sleep(2 * attempt)

    raise last_error

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
    pending_group = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            header, title = split_extinf(line[8:])
            attrs = parse_attributes(header)
            name = title.strip() or attrs.get("tvg-name", "").strip()

            if not name:
                current = None
                continue

            group = (
                attrs.get("grp", "").strip()
                or attrs.get("group-title", "").strip()
                or pending_group
            )

            current = {
                "title": name,
                "tvg_id": attrs.get("tvg-id", "").strip(),
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "group": group,
            }

        elif line.upper().startswith("#EXTGRP:"):
            group = line.split(":", 1)[1].strip()
            if current is not None:
                if not current.get("group"):
                    current["group"] = group
            else:
                pending_group = group

        elif not line.startswith("#") and current is not None:
            # URL нужен только как признак завершённой записи; не сохраняем его.
            channels.append(current)
            current = None
            pending_group = ""

    return channels

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

        log(f"Записей каналов в M3U без удаления повторов: {len(channels)}")
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
    by_base_name = defaultdict(set)

    for index, channel in enumerate(channels):
        channel["key"] = f"playlist_{index + 1}"
        if channel["tvg_id"]:
            by_id[channel["tvg_id"].casefold()].add(index)

        for name in {channel["title"], channel["tvg_name"]}:
            if not name:
                continue

            variants = {name, strip_country_suffix(name)}
            for variant in variants:
                normalized = normalize_name(variant)
                if normalized:
                    by_name[normalized].add(index)

                base = normalize_base_name(variant)
                if base:
                    by_base_name[base].add(index)

    return by_id, by_name, by_base_name


def map_source_channels(channel_id, display_names, indexes):
    """
    Возвращает ВСЕ записи M3U, соответствующие одному XMLTV ID.

    Одно расписание может быть назначено основному каналу и его отдельным
    вариантам HD/FHD/UHD/orig. Эти варианты остаются самостоятельными
    записями плейлиста. Числовые суффиксы (+2/+4 и т.п.) сохраняются.
    """
    by_id, by_name, by_base_name = indexes

    id_matches = (
        set(by_id.get(channel_id.casefold(), set()))
        if channel_id else set()
    )

    exact_matches = set()
    base_matches = set()
    exact_method = "exact name"
    country_method = False

    for name in display_names:
        source_name = strip_country_prefix(name)
        variants = (source_name, strip_country_suffix(source_name))

        for variant in dict.fromkeys(variants):
            normalized = normalize_name(variant)
            if normalized:
                matches = by_name.get(normalized, set())
                if matches:
                    exact_matches.update(matches)
                    if variant != name:
                        country_method = True

            base = normalize_base_name(variant)
            if base:
                # Все отдельные варианты одного базового канала получают
                # программу, а не только единственный найденный вариант.
                base_matches.update(by_base_name.get(base, set()))

    all_matches = id_matches | exact_matches | base_matches

    if all_matches:
        if id_matches:
            return all_matches, "tvg-id"
        if exact_matches:
            return all_matches, (
                "country-normalized name" if country_method else exact_method
            )
        return all_matches, "variant base name"

    return set(), "unmatched"
# ============================================================
# ОБРАБОТКА XMLTV
# ============================================================

def local_name(tag):
    return tag.split("}", 1)[-1]


def element_text(parent, tag):
    """Текст дочернего XMLTV-элемента, в том числе с namespace."""
    for element in parent:
        if local_name(element.tag) == tag:
            return "".join(element.itertext()).strip()
    return ""

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
        "status": "ERROR",
        "error": "",
        "xml_channels": 0,
        "xml_channels_with_programmes": 0,
        "matched_playlist_channels": 0,
        "channels_with_programmes": 0,
        "programmes_read": 0,
        "programmes_in_period": 0,
        "unique_programmes": 0,
        "duplicates_skipped": 0,
        "matched_by_tvg_id": 0,
        "matched_by_exact_name": 0,
        "matched_by_country_name": 0,
        "matched_by_base_name": 0,
        "ambiguous_channels": 0,
        "unmatched_channels": 0,
        "first_programme": "",
        "last_programme": "",
    }

    # Один XMLTV ID может соответствовать нескольким записям плейлиста.
    mapped = {}
    source_channels_with_programmes = set()
    channels_with_programmes = set()
    source_programmes = {}

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name

    try:
        log(f"  Скачивание: {source_name}")
        size = download_to_file(source_url, temp_path, MAX_SOURCE_SIZE)
        log(f"  Получено: {size / 1024 / 1024:.1f} MB")

        with open_xml_file(temp_path) as xml_file:
            context = ET.iterparse(xml_file, events=("start", "end"))
            root = None

            for event, elem in context:
                tag = local_name(elem.tag)

                if event == "start":
                    if root is None:
                        root = elem
                    continue

                if tag == "channel":
                    stats["xml_channels"] += 1
                    channel_id = elem.attrib.get("id", "").strip()
                    display_names = [
                        "".join(item.itertext()).strip()
                        for item in elem
                        if local_name(item.tag) == "display-name"
                    ]

                    matched_indexes, method = map_source_channels(
                        channel_id,
                        display_names,
                        indexes,
                    )

                    if matched_indexes and channel_id:
                        mapped.setdefault(channel_id, set()).update(
                            matched_indexes
                        )

                        if method == "tvg-id":
                            stats["matched_by_tvg_id"] += 1
                        elif method == "exact name":
                            stats["matched_by_exact_name"] += 1
                        elif method == "country-normalized name":
                            stats["matched_by_country_name"] += 1
                        elif method in ("unique base name", "variant base name"):
                            stats["matched_by_base_name"] += 1
                    elif method == "ambiguous":
                        stats["ambiguous_channels"] += 1
                    else:
                        stats["unmatched_channels"] += 1

                elif tag == "programme":
                    stats["programmes_read"] += 1
                    channel_id = elem.attrib.get("channel", "").strip()
                    matched_indexes = mapped.get(channel_id, set())

                    start_value = elem.attrib.get("start", "")
                    start_dt = parse_xmltv_datetime(start_value)

                    stop_value = elem.attrib.get("stop")
                    stop_dt = None

                    # Как в trim_epg.py и clean_epg.py:
                    # отсутствующий stop допустим, но явно указанный
                    # некорректный stop или stop <= start делает запись невалидной.
                    if stop_value is not None:
                        stop_dt = parse_xmltv_datetime(stop_value)
                        if stop_dt is None or start_dt is None or stop_dt <= start_dt:
                            start_dt = None

                    # Сохраняем все передачи, пересекающие окно,
                    # а не только те, которые начинаются внутри него.
                    in_period = (
                        start_dt is not None
                        and (
                            period_start <= start_dt < period_end
                            if stop_dt is None
                            else start_dt < period_end and stop_dt > period_start
                        )
                    )

                    if in_period:
                        title = element_text(elem, "title")
                        if title:
                            stats["programmes_in_period"] += 1
                            source_channels_with_programmes.add(channel_id)

                            if matched_indexes:
                                description = element_text(elem, "desc")
                                categories = [
                                    "".join(item.itertext()).strip()
                                    for item in elem
                                    if local_name(item.tag) == "category"
                                ]

                                stop_text = (
                                    stop_dt.isoformat()
                                    if stop_dt is not None
                                    else ""
                                )

                                for index in matched_indexes:
                                    identity = (
                                        index,
                                        start_dt.isoformat(),
                                        stop_text,
                                        normalize_name(title),
                                    )

                                    channels_with_programmes.add(index)

                                    if (
                                        identity in global_programmes
                                        or identity in source_programmes
                                    ):
                                        stats["duplicates_skipped"] += 1
                                    else:
                                        source_programmes[identity] = {
                                            "channel_index": index,
                                            "start": start_dt,
                                            "stop": stop_dt,
                                            "title": title,
                                            "description": description,
                                            "categories": categories,
                                            "source": source_name,
                                        }

                            current_date = start_dt.strftime("%Y-%m-%d %H:%M")
                            if not stats["first_programme"]:
                                stats["first_programme"] = current_date
                            stats["last_programme"] = current_date

                # Чистим только завершённые channel/programme, не их дочерние
                # display-name/title/desc/category до обработки родителя.
                if tag in ("channel", "programme"):
                    elem.clear()
                    if root is not None:
                        root.clear()

        all_matched_indexes = set()
        for matched_set in mapped.values():
            all_matched_indexes.update(matched_set)

        stats["matched_playlist_channels"] = len(all_matched_indexes)
        stats["xml_channels_with_programmes"] = len(
            source_channels_with_programmes
        )
        stats["channels_with_programmes"] = len(channels_with_programmes)
        stats["unique_programmes"] = len(source_programmes)
        stats["status"] = "OK"

        # Сливаем данные только после полного успешного разбора источника.
        global_programmes.update(source_programmes)

        log(
            f"  Результат: XMLTV-каналов с программами "
            f"{stats['xml_channels_with_programmes']}; "
            f"сопоставлено с плейлистом {stats['matched_playlist_channels']}; "
            f"каналов плейлиста с программами "
            f"{stats['channels_with_programmes']}; "
            f"новых передач {stats['unique_programmes']}; "
            f"дубликатов {stats['duplicates_skipped']}"
        )

    except Exception as error:
        stats["status"] = "ERROR"
        stats["error"] = safe_error_message(error)
        log(f"  Ошибка источника: {stats['error']}")

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
    """Таблица каналов: порядковый номер, название и группа. Без URL и TVG-REC."""
    path = OUTPUT_DIR / "channels_test.txt"
    headers = ["№", "CHANNEL", "GRP"]

    def clean(value):
        return (
            str(value or "")
            .replace("\t", " ")
            .replace("\r", " ")
            .replace("\n", " ")
            .strip()
        )

    rows = [
        [
            str(index),
            clean(channel.get("title")),
            clean(channel.get("group")),
        ]
        for index, channel in enumerate(channels, start=1)
    ]

    widths = [
        max(
            len(headers[index]),
            max((len(row[index]) for row in rows), default=0),
        )
        for index in range(len(headers))
    ]

    def format_row(values):
        return " | ".join(
            str(value).ljust(widths[index])
            for index, value in enumerate(values)
        )

    separator = "-+-".join("-" * width for width in widths)

    with open(path, "w", encoding="utf-8-sig") as file:
        file.write(format_row(headers) + "\n")
        file.write(separator + "\n")
        for row in rows:
            file.write(format_row(row) + "\n")

def write_duplicate_report(channels):
    """Записывает повторы по названию и tvg-id без URL потоков."""
    path = OUTPUT_DIR / "playlist_duplicates.csv"
    groups = defaultdict(list)

    for index, channel in enumerate(channels, start=1):
        title_key = " ".join(channel["title"].casefold().split())
        tvg_id_key = channel["tvg_id"].casefold().strip()
        if title_key:
            groups[("same_title", title_key)].append(index)
        if tvg_id_key:
            groups[("same_tvg_id", tvg_id_key)].append(index)

    fields = [
        "duplicate_by",
        "duplicate_group",
        "playlist_entry",
        "channel",
        "group",
    ]

    duplicate_group_count = 0
    duplicate_entries = set()

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for (duplicate_by, duplicate_key), indexes in sorted(groups.items()):
            if len(indexes) < 2:
                continue

            duplicate_group_count += 1
            group_name = f"D{duplicate_group_count:04d}"

            for index in indexes:
                channel = channels[index - 1]
                duplicate_entries.add(index)
                writer.writerow({
                    "duplicate_by": duplicate_by,
                    "duplicate_group": group_name,
                    "playlist_entry": index,
                    "channel": channel["title"],
                    "group": channel["group"],
                })

    return duplicate_group_count, len(duplicate_entries)

def write_coverage_report(stats_list):
    path = OUTPUT_DIR / "coverage_report.csv"
    fields = [
        "source",
        "status",
        "error",
        "xml_channels",
        "xml_channels_with_programmes",
        "matched_playlist_channels",
        "channels_with_programmes",
        "programmes_read",
        "programmes_in_period",
        "unique_programmes",
        "duplicates_skipped",
        "matched_by_tvg_id",
        "matched_by_exact_name",
        "matched_by_country_name",
        "matched_by_base_name",
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

    duplicate_groups, duplicate_entries = write_duplicate_report(channels)
    log(f"Групп совпадений по названию или tvg-id: {duplicate_groups}")
    log(f"Записей плейлиста в этих группах: {duplicate_entries}")

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
    log("  playlist_duplicates.csv")
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
