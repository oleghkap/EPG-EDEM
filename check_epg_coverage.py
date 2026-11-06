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

DAYS_BEFORE = 1
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
    channel_icons,
    exact_programmes_seen,
    merge_diagnostics,
    merge_collision_details,
    duplicate_channel_id_details,
    channel_mapping_details,
):
    stats = {
        "source": source_name,
        "status": "ERROR",
        "error": "",
        "xml_channels": 0,
        "duplicate_xml_channel_ids": 0,
        "xml_channels_with_programmes": 0,
        "matched_playlist_channels": 0,
        "channels_with_programmes": 0,
        "programmes_read": 0,
        "programmes_in_period": 0,
        "unique_programmes": 0,
        "duplicates_skipped": 0,
        "exact_duplicates": 0,
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
    source_channel_ids = set()
    source_channel_id_counts = defaultdict(int)
    source_channel_display_names = defaultdict(set)
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
                    if channel_id:
                        source_channel_id_counts[channel_id] += 1
                        if source_channel_id_counts[channel_id] > 1:
                            stats["duplicate_xml_channel_ids"] += 1
                        source_channel_ids.add(channel_id)
                    display_names = [
                        "".join(item.itertext()).strip()
                        for item in elem
                        if local_name(item.tag) == "display-name"
                    ]

                    if channel_id:
                        source_channel_display_names[channel_id].update(name for name in display_names if name)

                    matched_indexes, method = map_source_channels(
                        channel_id,
                        display_names,
                        indexes,
                    )

                    if matched_indexes and channel_id:
                        mapped.setdefault(channel_id, set()).update(
                            matched_indexes
                        )

                        # Сохраняем один логотип на канал: первый доступный
                        # по приоритету источников (EPG.ONE обрабатывается первым).
                        icon_src = next(
                            (
                                item.attrib.get("src", "").strip()
                                for item in elem
                                if local_name(item.tag) == "icon"
                                and item.attrib.get("src", "").strip()
                            ),
                            "",
                        )
                        if icon_src:
                            for playlist_index in matched_indexes:
                                channel_icons.setdefault(playlist_index, icon_src)

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
                                    exact_signature = (
                                        index,
                                        start_dt.isoformat(),
                                        stop_text,
                                        title,
                                        description,
                                        tuple(categories),
                                    )
                                    if exact_signature in exact_programmes_seen:
                                        stats["exact_duplicates"] += 1
                                        exact_programmes_seen[exact_signature] += 1
                                    else:
                                        exact_programmes_seen[exact_signature] = 1

                                    # Для одного канала не может одновременно
                                    # начинаться две разные передачи. Источники
                                    # часто расходятся в title/stop, поэтому эти
                                    # поля не должны создавать дубликаты.
                                    identity = (index, start_dt.isoformat())

                                    channels_with_programmes.add(index)

                                    existing = global_programmes.get(identity)
                                    if existing is None:
                                        existing = source_programmes.get(identity)

                                    if existing is not None:
                                        stats["duplicates_skipped"] += 1
                                        merge_diagnostics["identity_collisions"] += 1
                                        same_source = existing["source"] == source_name
                                        merge_diagnostics["same_source_collisions" if same_source else "cross_source_collisions"] += 1
                                        existing_xml_id = existing.get("xml_channel_id", "")
                                        if same_source:
                                            relation = "same ID" if existing_xml_id == channel_id else "different IDs"
                                            merge_diagnostics["same_source_same_xml_id" if existing_xml_id == channel_id else "same_source_different_xml_id"] += 1
                                            merge_collision_details.append({
                                                "source": source_name,
                                                "playlist_channel": playlist_channels[index]["title"],
                                                "playlist_index": index,
                                                "start": start_dt.isoformat(),
                                                "existing_xml_channel_id": existing_xml_id,
                                                "incoming_xml_channel_id": channel_id,
                                                "xml_channel_id_relation": relation,
                                                "existing_title_chars": len(existing["title"] or ""),
                                                "incoming_title_chars": len(title or ""),
                                                "existing_desc_chars": len(existing["description"] or ""),
                                                "incoming_desc_chars": len(description or ""),
                                            })
                                        if title == existing["title"]:
                                            merge_diagnostics["title_same"] += 1
                                        else:
                                            merge_diagnostics["title_different"] += 1
                                            if " ".join(title.casefold().split()) == " ".join((existing["title"] or "").casefold().split()):
                                                merge_diagnostics["title_normalized_same"] += 1
                                            if len(title) > len(existing["title"] or ""):
                                                merge_diagnostics["title_incoming_longer"] += 1
                                            elif len(title) < len(existing["title"] or ""):
                                                merge_diagnostics["title_incoming_shorter"] += 1
                                            else:
                                                merge_diagnostics["title_equal_length"] += 1
                                        if description and existing["description"]:
                                            if description == existing["description"]:
                                                merge_diagnostics["desc_same_nonempty"] += 1
                                            else:
                                                merge_diagnostics["desc_different_nonempty"] += 1
                                                if " ".join(description.casefold().split()) == " ".join((existing["description"] or "").casefold().split()):
                                                    merge_diagnostics["desc_normalized_same"] += 1
                                                if len(description) > len(existing["description"] or ""):
                                                    merge_diagnostics["desc_incoming_longer"] += 1
                                                elif len(description) < len(existing["description"] or ""):
                                                    merge_diagnostics["desc_incoming_shorter"] += 1
                                                else:
                                                    merge_diagnostics["desc_equal_length"] += 1
                                        elif description and not existing["description"]:
                                            merge_diagnostics["desc_filled_from_later_source"] += 1
                                        elif not description and existing["description"]:
                                            merge_diagnostics["incoming_desc_empty"] += 1
                                        existing_categories = set(existing["categories"])
                                        incoming_categories = set(categories)
                                        if existing_categories == incoming_categories:
                                            merge_diagnostics["categories_same_set"] += 1
                                        else:
                                            merge_diagnostics["categories_different_set"] += 1
                                        merge_diagnostics["new_categories_added"] += len(incoming_categories - existing_categories)
                                        # Не теряем описание и категории, если
                                        # приоритетный источник их не предоставил.
                                        if not existing["description"] and description:
                                            existing["description"] = description
                                        if categories:
                                            merged_categories = list(existing["categories"])
                                            for category in categories:
                                                if category and category not in merged_categories:
                                                    merged_categories.append(category)
                                            existing["categories"] = merged_categories
                                    else:
                                        source_programmes[identity] = {
                                            "channel_index": index,
                                            "start": start_dt,
                                            "stop": stop_dt,
                                            "title": title,
                                            "description": description,
                                            "categories": categories,
                                            "source": source_name,
                                            "xml_channel_id": channel_id,
                                        }

                            current_date = start_dt.strftime("%Y-%m-%d %H:%M")
                            if (
                                not stats["first_programme"]
                                or current_date < stats["first_programme"]
                            ):
                                stats["first_programme"] = current_date
                            if current_date > stats["last_programme"]:
                                stats["last_programme"] = current_date

                # Чистим только завершённые channel/programme, не их дочерние
                # display-name/title/desc/category до обработки родителя.
                if tag in ("channel", "programme"):
                    elem.clear()
                    if root is not None:
                        root.clear()

        for channel_id, occurrences in source_channel_id_counts.items():
            if occurrences > 1:
                indexes_for_id = sorted(mapped.get(channel_id, set()))
                duplicate_channel_id_details.append({
                    "source": source_name,
                    "xml_channel_id": channel_id,
                    "occurrences": occurrences,
                    "display_names": " | ".join(sorted(source_channel_display_names[channel_id])),
                    "playlist_channels": " | ".join(playlist_channels[i]["title"] for i in indexes_for_id),
                    "playlist_indexes": " | ".join(str(i) for i in indexes_for_id),
                })

        reverse_mapping = defaultdict(set)
        for channel_id, matched_set in mapped.items():
            for playlist_index in matched_set:
                reverse_mapping[playlist_index].add(channel_id)
        for playlist_index, channel_ids in reverse_mapping.items():
            if len(channel_ids) > 1:
                channel_mapping_details.append({
                    "source": source_name,
                    "playlist_index": playlist_index,
                    "playlist_channel": playlist_channels[playlist_index]["title"],
                    "xml_channel_id_count": len(channel_ids),
                    "xml_channel_ids": " | ".join(sorted(channel_ids)),
                })

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
        "duplicate_xml_channel_ids",
        "xml_channels_with_programmes",
        "matched_playlist_channels",
        "channels_with_programmes",
        "programmes_read",
        "programmes_in_period",
        "unique_programmes",
        "duplicates_skipped",
        "exact_duplicates",
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

def write_combined_xml(
    channels,
    global_programmes,
    channel_icons,
    description_limit=None,
    output_path=None,
):
    path = output_path or (OUTPUT_DIR / "epg_test_combined.xml.gz")
    uncompressed_size = 0

    # Пишем XML потоково и параллельно считаем точный размер XML до GZIP.
    with gzip.open(path, "wb", compresslevel=6) as file:
        def write_xml(data):
            nonlocal uncompressed_size
            file.write(data)
            uncompressed_size += len(data)

        write_xml(b'<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="EPG-EDEM Coverage Test">')

        for index, channel in enumerate(channels):
            channel_element = ET.Element(
                "channel",
                {"id": f"playlist_{index + 1}"},
            )
            ET.SubElement(channel_element, "display-name").text = channel["title"]

            icon_src = channel_icons.get(index, "")
            if icon_src:
                ET.SubElement(channel_element, "icon", {"src": icon_src})

            write_xml(ET.tostring(channel_element, encoding="utf-8"))

        ordered = sorted(
            global_programmes.values(),
            key=lambda item: (
                item["channel_index"],
                item["start"],
                item["title"].casefold(),
            ),
        )

        for programme in ordered:
            element = ET.Element(
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

            description = programme["description"]
            if description_limit is not None:
                description = description[:description_limit]

            if description:
                ET.SubElement(element, "desc").text = description

            for category in programme["categories"]:
                if category:
                    ET.SubElement(element, "category").text = category

            write_xml(ET.tostring(element, encoding="utf-8"))

        write_xml(b"</tv>")

    return {
        "xml_uncompressed_bytes": uncompressed_size,
        "gzip_bytes": path.stat().st_size,
    }


def write_diagnostic_csv(filename, rows, fieldnames):
    path = OUTPUT_DIR / filename
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_epg_diagnostics(
    channels,
    global_programmes,
    channel_icons,
    period_start,
    period_end,
    xml_sizes,
    exact_programmes_seen,
    merge_diagnostics,
):
    """Сводная диагностика объёма XMLTV, описаний и распределения передач."""
    programmes = list(global_programmes.values())

    title_bytes = sum(
        len((item.get("title") or "").encode("utf-8"))
        for item in programmes
    )
    descriptions = [(item.get("description") or "") for item in programmes]
    description_bytes = sum(len(value.encode("utf-8")) for value in descriptions)
    description_chars = sum(len(value) for value in descriptions)
    category_bytes = sum(
        len((category or "").encode("utf-8"))
        for item in programmes
        for category in item.get("categories", [])
    )

    nonempty_descriptions = [value for value in descriptions if value]
    description_lengths_chars = [len(value) for value in nonempty_descriptions]
    description_lengths_bytes = [
        len(value.encode("utf-8")) for value in nonempty_descriptions
    ]

    def percentile(values, percent):
        """Линейный процентиль; возвращает 0 для пустого набора."""
        if not values:
            return 0
        ordered = sorted(values)
        position = (len(ordered) - 1) * percent / 100
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)

    description_percentiles = {
        percent: percentile(description_lengths_bytes, percent)
        for percent in (50, 75, 90, 95, 99, 100)
    }

    # Распределение непустых описаний по длине в UTF-8 байтах.
    description_buckets = [
        ("1–256 байт", 1, 256),
        ("257–512 байт", 257, 512),
        ("513–1024 байта", 513, 1024),
        ("1025–2048 байт", 1025, 2048),
        ("2049–4096 байт", 2049, 4096),
        ("более 4096 байт", 4097, None),
    ]
    bucket_counts = {}
    for label, lower, upper in description_buckets:
        bucket_counts[label] = sum(
            1 for length in description_lengths_bytes
            if length >= lower and (upper is None or length <= upper)
        )

    # Оценка экономии при ограничении по символам. Срез строки безопасен
    # для UTF-8: он не разрезает многобайтовый символ.
    truncation_savings = {}
    for limit in (500, 750, 1000):
        saved = sum(
            len(value.encode("utf-8")) - len(value[:limit].encode("utf-8"))
            for value in nonempty_descriptions
            if len(value) > limit
        )
        truncation_savings[limit] = saved

    # Десять самых длинных описаний: сами тексты описаний в отчёт не попадают.
    longest_descriptions = sorted(
        (
            (
                len((item.get("description") or "")),
                len((item.get("description") or "").encode("utf-8")),
                item.get("channel_index", -1),
                item.get("title") or "",
            )
            for item in programmes
            if item.get("description")
        ),
        reverse=True,
    )[:10]

    exact_duplicate_count = sum(
        count - 1 for count in exact_programmes_seen.values() if count > 1
    )
    exact_duplicate_groups = sum(
        1 for count in exact_programmes_seen.values() if count > 1
    )

    def duplicate_value_stats(values):
        counts = defaultdict(int)
        channel_sets = defaultdict(set)
        for channel_index, value in values:
            if value:
                counts[value] += 1
                channel_sets[value].add(channel_index)
        repeated_groups = {value: count for value, count in counts.items() if count > 1}
        return {
            "groups": len(repeated_groups),
            "extra_occurrences": sum(count - 1 for count in repeated_groups.values()),
            "items_in_repeated_groups": sum(repeated_groups.values()),
            "repeated_on_same_channel_groups": sum(
                1 for value in repeated_groups if len(channel_sets[value]) == 1
            ),
            "repeated_across_channels_groups": sum(
                1 for value in repeated_groups if len(channel_sets[value]) > 1
            ),
        }

    title_duplicates = duplicate_value_stats(
        [(item["channel_index"], item.get("title", "")) for item in programmes]
    )
    desc_duplicates = duplicate_value_stats(
        [(item["channel_index"], item.get("description", "")) for item in programmes]
    )
    category_values = [
        (item["channel_index"], category)
        for item in programmes
        for category in item.get("categories", [])
    ]
    category_duplicates = duplicate_value_stats(category_values)
    duplicate_categories_within_programme = sum(
        len(item.get("categories", [])) - len(set(item.get("categories", [])))
        for item in programmes
    )

    # Передача учитывается в каждом календарном дне, с которым пересекается
    # её интервал. Если stop отсутствует, учитываем день её начала.
    daily_counts = {}
    day = period_start.date()
    while day < period_end.date():
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
        day_end = day_start + timedelta(days=1)
        daily_counts[day.isoformat()] = 0

        for item in programmes:
            start = item["start"].astimezone(TZ)
            stop = item["stop"].astimezone(TZ) if item["stop"] is not None else None
            overlaps = (
                day_start <= start < day_end
                if stop is None
                else start < day_end and stop > day_start
            )
            if overlaps:
                daily_counts[day.isoformat()] += 1

        day += timedelta(days=1)

    xml_bytes = xml_sizes["xml_uncompressed_bytes"]
    gzip_bytes = xml_sizes["gzip_bytes"]
    # Точное измерение GZIP для нескольких лимитов desc.
    # Временные файлы не заменяют основной EPG и удаляются после измерения.
    variant_gzip_sizes = {}
    for limit in (200, 250, 300, 350, 400, 500, 750):
        variant_path = OUTPUT_DIR / f".epg_test_combined_desc_{limit}.xml.gz"
        try:
            variant_sizes = write_combined_xml(
                channels,
                global_programmes,
                channel_icons,
                description_limit=limit,
                output_path=variant_path,
            )
            variant_gzip_sizes[limit] = variant_sizes["gzip_bytes"]
        finally:
            if variant_path.exists():
                variant_path.unlink()

    report_path = OUTPUT_DIR / "epg_diagnostics.txt"

    lines = [
        "EPG-EDEM — ДИАГНОСТИКА ИТОГОВОГО EPG",
        "=" * 55,
        f"Период: {period_start.isoformat()} — {period_end.isoformat()} (конец не включён)",
        f"Каналов в итоговом XML: {len(channels)}",
        f"Передач в итоговом XML: {len(programmes)}",
        f"Размер несжатого XML: {xml_bytes:,} байт ({xml_bytes / 1024 / 1024:.2f} MiB)",
        f"Размер GZIP: {gzip_bytes:,} байт ({gzip_bytes / 1024 / 1024:.2f} MiB)",
        "",
        "Объём текстовых полей (UTF-8, без XML-разметки):",
        f"  title: {title_bytes:,} байт ({title_bytes / 1024 / 1024:.2f} MiB)",
        f"  desc: {description_bytes:,} байт ({description_bytes / 1024 / 1024:.2f} MiB)",
        f"  category: {category_bytes:,} байт ({category_bytes / 1024 / 1024:.2f} MiB)",
        f"  Всего текста: {title_bytes + description_bytes + category_bytes:,} байт",
        "",
        "ДЕТАЛИЗАЦИЯ ОПИСАНИЙ DESC",
        f"  Всего передач: {len(descriptions)}",
        f"  С непустым desc: {len(nonempty_descriptions)}",
        f"  С пустым desc: {len(descriptions) - len(nonempty_descriptions)}",
        f"  Всего символов: {description_chars:,}",
        f"  Всего UTF-8 байт: {description_bytes:,}",
    ]

    lines.append("  Процентили длины непустого desc (UTF-8 байты):")
    lines.extend(
        f"    P{percent}: {value:,} байт"
        for percent, value in description_percentiles.items()
    )

    lines.append("  Распределение непустых описаний по длине:")
    lines.extend(
        f"    {label}: {bucket_counts[label]:,}"
        for label, _, _ in description_buckets
    )

    lines.append("  Фактический GZIP при ограничении desc (level 6):")
    for limit, size in variant_gzip_sizes.items():
        reduction = gzip_bytes - size
        lines.append(
            f"    До {limit} символов: {size:,} байт "
            f"({size / 1_000_000:.2f} MB; {size / 1024 / 1024:.2f} MiB), "
            f"экономия {reduction / 1_000_000:.2f} MB"
        )

    lines.append("  Оценка экономии при ограничении длины (по символам):")
    for limit, saved in truncation_savings.items():
        share = (saved / description_bytes * 100) if description_bytes else 0
        lines.append(
            f"    До {limit} символов: {saved:,} байт "
            f"({saved / 1024 / 1024:.2f} MiB; {share:.1f}% объёма desc)"
        )
    lines.extend([
        "    Примечание: это оценка экономии UTF-8 текста до XML-экранирования и GZIP.",
        "  10 самых длинных описаний (текст desc не выводится):",
    ])
    for chars, byte_count, channel_index, title in longest_descriptions:
        channel_name = (
            channels[channel_index].get("title", "")
            if 0 <= channel_index < len(channels)
            else "неизвестный канал"
        )
        safe_title = " ".join(title.split())[:80]
        lines.append(
            f"    {byte_count:,} байт / {chars:,} симв. | "
            f"{channel_name} | {safe_title}"
        )

    lines.extend([
        "",
        "ПОВТОРЫ ПОЛЕЙ В ИТОГОВОМ EPG (непустые значения, точное совпадение):",
        f"  title: групп {title_duplicates['groups']}, повторных вхождений {title_duplicates['extra_occurrences']}, групп только на одном канале {title_duplicates['repeated_on_same_channel_groups']}, на разных каналах {title_duplicates['repeated_across_channels_groups']}",
        f"  desc: групп {desc_duplicates['groups']}, повторных вхождений {desc_duplicates['extra_occurrences']}, групп только на одном канале {desc_duplicates['repeated_on_same_channel_groups']}, на разных каналах {desc_duplicates['repeated_across_channels_groups']}",
        f"  category: групп {category_duplicates['groups']}, повторных вхождений {category_duplicates['extra_occurrences']}, групп только на одном канале {category_duplicates['repeated_on_same_channel_groups']}, на разных каналах {category_duplicates['repeated_across_channels_groups']}",
        f"  Повторяющихся category внутри одной передачи: {duplicate_categories_within_programme}",
        "",
        "СОВПАДЕНИЯ ПРИ СЛИЯНИИ ИСТОЧНИКОВ ДЛЯ ОДНОГО КАНАЛА И ВРЕМЕНИ:",
        f"  Всего совпадений identity: {merge_diagnostics['identity_collisions']}",
        f"  Между разными источниками: {merge_diagnostics['cross_source_collisions']}",
        f"  Внутри одного источника: {merge_diagnostics['same_source_collisions']}",
        f"    Один и тот же XMLTV ID: {merge_diagnostics['same_source_same_xml_id']}",
        f"    Разные XMLTV ID сопоставлены одному каналу плейлиста: {merge_diagnostics['same_source_different_xml_id']}",
        f"  Нормализованные совпадения title среди различающихся: {merge_diagnostics['title_normalized_same']}",
        f"  Входящий title длиннее: {merge_diagnostics['title_incoming_longer']}; короче: {merge_diagnostics['title_incoming_shorter']}; равной длины: {merge_diagnostics['title_equal_length']}",
        f"  Нормализованные совпадения desc среди различающихся: {merge_diagnostics['desc_normalized_same']}",
        f"  Входящий непустой desc длиннее: {merge_diagnostics['desc_incoming_longer']}; короче: {merge_diagnostics['desc_incoming_shorter']}; равной длины: {merge_diagnostics['desc_equal_length']}",
        f"  Одинаковый title: {merge_diagnostics['title_same']}; различается: {merge_diagnostics['title_different']}",
        f"  Одинаковый непустой desc: {merge_diagnostics['desc_same_nonempty']}; различается: {merge_diagnostics['desc_different_nonempty']}",
        f"  Поздний источник заполнил пустой desc: {merge_diagnostics['desc_filled_from_later_source']}",
        f"  У поздней записи desc пустой, у сохранённой непустой: {merge_diagnostics['incoming_desc_empty']}",
        f"  Одинаковые наборы category: {merge_diagnostics['categories_same_set']}; разные: {merge_diagnostics['categories_different_set']}",
        f"  Дополнительных категорий добавлено при слиянии: {merge_diagnostics['new_categories_added']}",
        "",
        "Точные дубликаты programme по каналу, времени и полному содержимому:",
        f"  Повторных записей в источниках: {exact_duplicate_count}",
        f"  Групп дубликатов: {exact_duplicate_groups}",
        "",
        "Передач по календарным дням (по пересечению интервала):",
    ])

    lines.extend(
        f"  {date}: {count}"
        for date, count in daily_counts.items()
    )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "channels": len(channels),
        "programmes": len(programmes),
        "title_bytes": title_bytes,
        "description_bytes": description_bytes,
        "description_chars": description_chars,
        "nonempty_descriptions": len(nonempty_descriptions),
        "empty_descriptions": len(descriptions) - len(nonempty_descriptions),
        "description_percentiles": description_percentiles,
        "description_buckets": bucket_counts,
        "truncation_savings": truncation_savings,
        "variant_gzip_sizes": variant_gzip_sizes,
        "category_bytes": category_bytes,
        "exact_duplicate_count": exact_duplicate_count,
        "daily_counts": daily_counts,
        "xml_uncompressed_bytes": xml_bytes,
        "gzip_bytes": gzip_bytes,
    }


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
    log(f"Количество календарных дней: {(period_end.date() - period_start.date()).days}")
    log(f"Количество источников: {len(SOURCES)}")
    log()

    channels = load_playlist()
    write_channels(channels)

    duplicate_groups, duplicate_entries = write_duplicate_report(channels)
    log(f"Групп совпадений по названию или tvg-id: {duplicate_groups}")
    log(f"Записей плейлиста в этих группах: {duplicate_entries}")

    indexes = build_playlist_indexes(channels)

    global_programmes = {}
    channel_icons = {}
    exact_programmes_seen = {}
    merge_diagnostics = defaultdict(int)
    merge_collision_details = []
    duplicate_channel_id_details = []
    channel_mapping_details = []
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
            channel_icons,
            exact_programmes_seen,
            merge_diagnostics,
            merge_collision_details,
            duplicate_channel_id_details,
            channel_mapping_details,
        )

        all_stats.append(stats)

    write_coverage_report(all_stats)
    write_channel_report(channels, global_programmes)
    write_diagnostic_csv("merge_collision_details.csv", merge_collision_details, ["source", "playlist_channel", "playlist_index", "start", "existing_xml_channel_id", "incoming_xml_channel_id", "xml_channel_id_relation", "existing_title_chars", "incoming_title_chars", "existing_desc_chars", "incoming_desc_chars"])
    write_diagnostic_csv("duplicate_xml_channel_ids.csv", duplicate_channel_id_details, ["source", "xml_channel_id", "occurrences", "display_names", "playlist_channels", "playlist_indexes"])
    write_diagnostic_csv("source_channel_mapping_collisions.csv", channel_mapping_details, ["source", "playlist_index", "playlist_channel", "xml_channel_id_count", "xml_channel_ids"])
    xml_sizes = write_combined_xml(channels, global_programmes, channel_icons)
    diagnostics = write_epg_diagnostics(
        channels,
        global_programmes,
        channel_icons,
        period_start,
        period_end,
        xml_sizes,
        exact_programmes_seen,
        merge_diagnostics,
    )

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
    log("ДИАГНОСТИКА ИТОГОВОГО EPG")
    log(f"Размер несжатого XML: {diagnostics['xml_uncompressed_bytes'] / 1024 / 1024:.2f} MiB")
    log(f"Размер GZIP: {diagnostics['gzip_bytes'] / 1024 / 1024:.2f} MiB")
    log(f"Объём title: {diagnostics['title_bytes'] / 1024 / 1024:.2f} MiB")
    log(f"Объём desc: {diagnostics['description_bytes'] / 1024 / 1024:.2f} MiB")
    log("Фактический GZIP при ограничении desc (level 6):")
    for limit, size in diagnostics["variant_gzip_sizes"].items():
        log(f"  До {limit} символов: {size / 1_000_000:.2f} MB ({size / 1024 / 1024:.2f} MiB)")
    log(f"Непустых desc: {diagnostics['nonempty_descriptions']}; пустых: {diagnostics['empty_descriptions']}")
    for percent, value in diagnostics["description_percentiles"].items():
        log(f"  P{percent} длины desc: {value} байт")
    for limit, saved in diagnostics["truncation_savings"].items():
        log(f"  Экономия при ограничении desc до {limit} символов: {saved / 1024 / 1024:.2f} MiB")
    log(f"Объём category: {diagnostics['category_bytes'] / 1024 / 1024:.2f} MiB")
    log(f"Точных дубликатов в источниках: {diagnostics['exact_duplicate_count']}")
    for date, count in diagnostics["daily_counts"].items():
        log(f"  Передач за {date}: {count}")

    log()
    log("Созданы файлы:")
    log("  channels_test.txt")
    log("  playlist_duplicates.csv")
    log("  coverage_report.csv")
    log("  channel_coverage.csv")
    log("  epg_diagnostics.txt")
    log("  epg_test_combined.xml.gz")

    log()
    log(f"Время выполнения: {(time.time() - started) / 60:.1f} мин.")

    if not any(item["status"] == "OK" for item in all_stats):
        log("ВНИМАНИЕ: ни один источник не обработан успешно.")
        sys.exit(1)


if __name__ == "__main__":
    main()
