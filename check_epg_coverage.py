#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EPG-EDEM: ручная проверка покрытия персонального M3U

Результаты:
  channels_test.txt
  playlist_duplicates.csv
  coverage_report.csv
  channel_coverage.csv
  epg_custom.xml.gz

PLAYLIST_URL берётся только из GitHub Secrets.
Персональный URL плейлиста не записывается в результаты.
"""

import csv
import gzip
import hashlib
import io
import os
import re
import socket
import subprocess
import sys
import time
import shutil
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

from bisect import bisect_left, insort
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
COUNTRY_CODE_SET = frozenset(COUNTRY_CODES.split("|"))
# XMLTV IDs commonly use ISO country suffixes not all needed in display-name parsing.
XMLTV_ID_COUNTRY_CODE_SET = frozenset(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ "
    "BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR "
    "CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU "
    "ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ "
    "LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ "
    "MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF "
    "PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI "
    "SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR "
    "TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW UK"
    .split()
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


def canonical_mapping_name(value):
    """
    Нормализует имя для сопоставления региональных вариантов.
    Удаляет только +0 (нулевой сдвиг) с необязательным регионом.
    Суффиксы +2/+4/+7 и другие временные сдвиги сохраняются.
    """
    value = strip_country_suffix(strip_country_prefix(value or "")).strip()
    value = re.sub(
        r"\s*\+\s*0\s*(?:\([^)]*\))?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return normalize_base_name(value) or normalize_name(value)


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
                "tvg_logo": attrs.get("tvg-logo", "").strip(),
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

            # Рабочий индекс содержит только полное название.
            # Ослабленные правила используются только в диагностике.
            normalized = normalize_name(name)
            if normalized:
                by_name[normalized].add(index)

            # Для базового поиска учитываем региональный суффикс,
            # но ниже обязательно проверяем совместимость страны и качества.
            base_source_name = strip_country_suffix(strip_country_prefix(name))
            base = normalize_base_name(base_source_name)
            if base:
                by_base_name[base].add(index)

    return by_id, by_name, by_base_name


def audit_mapping_candidates(display_names, indexes, playlist_channels):
    """Диагностика кандидатов без изменения действующего сопоставления."""
    _, by_name, by_base_name = indexes
    exact = set()
    country = set()
    base = set()
    for name in display_names:
        if not name:
            continue
        normalized = normalize_name(name)
        if normalized:
            exact.update(by_name.get(normalized, set()))
        country_name = strip_country_suffix(strip_country_prefix(name))
        normalized_country = normalize_name(country_name)
        if normalized_country:
            country.update(by_name.get(normalized_country, set()))
        normalized_base = normalize_base_name(country_name)
        if normalized_base:
            for index in by_base_name.get(normalized_base, set()):
                playlist_names = {
                    playlist_channels[index].get("title", ""),
                    playlist_channels[index].get("tvg_name", ""),
                }
                if any(
                    candidate and base_names_compatible(name, candidate)
                    for candidate in playlist_names
                ):
                    base.add(index)

    def index_text(values):
        return " | ".join(str(i) for i in sorted(values))
    def name_text(values):
        return " | ".join(f"{i}:{playlist_channels[i]['title']}" for i in sorted(values))
    exact_country = exact | country
    return {
        "exact_count": len(exact), "exact_indexes": index_text(exact), "exact_names": name_text(exact),
        "country_count": len(country), "country_indexes": index_text(country), "country_names": name_text(country),
        "exact_country_indexes": index_text(exact_country), "exact_country_names": name_text(exact_country),
        "base_count": len(base), "base_indexes": index_text(base), "base_names": name_text(base),
        "base_only_count": len(base - exact_country), "base_only_indexes": index_text(base - exact_country),
        "base_only_names": name_text(base - exact_country),
        "all_candidate_indexes": index_text(exact_country | base),
        "all_candidate_names": name_text(exact_country | base),
    }


def quality_markers(value):
    return {
        match.group(1).casefold().replace(" ", "")
        for match in QUALITY_RE.finditer(value or "")
    }


def xmltv_id_country_codes(channel_id):
    """Извлекает региональный код из суффикса XMLTV ID (например, .hr или .it)."""
    match = re.search(r"\.([a-z]{2})\d*$", (channel_id or "").strip(), re.IGNORECASE)
    if not match:
        return set()
    code = match.group(1).upper()
    return {code} if code in XMLTV_ID_COUNTRY_CODE_SET else set()


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


def base_names_compatible(source_name, playlist_name):
    """
    Разрешает базовое совпадение только без явного противоречия:
    разные маркеры качества или разные коды стран не объединяем.
    orig остаётся отдельным вариантом.
    """
    source_quality = quality_markers(source_name)
    playlist_quality = quality_markers(playlist_name)

    if "orig" in source_quality or "orig" in playlist_quality:
        if source_quality != playlist_quality:
            return False

    if source_quality and playlist_quality and source_quality.isdisjoint(playlist_quality):
        return False

    source_countries = edge_country_codes(source_name)
    playlist_countries = edge_country_codes(playlist_name)
    if source_countries and playlist_countries and source_countries.isdisjoint(playlist_countries):
        return False

    return True


def map_source_channels(channel_id, display_names, indexes, playlist_channels):
    """
    Сопоставление по tvg-id, точному имени, региональному имени,
    затем по базовому имени при совместимых маркерах качества/страны.
    Ослабленные правила назначают канал только при единственном
    кандидате в плейлисте.
    """
    by_id, by_name, by_base_name = indexes

    id_matches = (
        set(by_id.get(channel_id.casefold(), set()))
        if channel_id else set()
    )

    exact_matches = set()
    for name in display_names:
        normalized = normalize_name(name)
        if normalized:
            exact_matches.update(by_name.get(normalized, set()))

    if id_matches:
        return id_matches, "tvg-id"

    # Регион в XMLTV ID — такой же значимый признак, как регион в имени.
    # Например, Discovery.Channel.it нельзя присоединять к общему
    # Discovery Channel: это расписание конкретной региональной версии.
    id_countries = xmltv_id_country_codes(channel_id)
    region_filtered = False

    def keep_matching_region(indices):
        nonlocal region_filtered
        if not id_countries:
            return set(indices)
        compatible = set()
        for index in indices:
            playlist_countries = set()
            for name in (
                playlist_channels[index].get("title", ""),
                playlist_channels[index].get("tvg_name", ""),
            ):
                playlist_countries.update(edge_country_codes(name))
            if playlist_countries & id_countries:
                compatible.add(index)
        if compatible != set(indices):
            region_filtered = True
        return compatible

    exact_matches = keep_matching_region(exact_matches)
    if exact_matches:
        return exact_matches, "exact name"

    country_matches = set()
    for name in display_names:
        if not name:
            continue
        country_name = strip_country_suffix(strip_country_prefix(name))
        if country_name == name.strip():
            continue
        normalized_country = normalize_name(country_name)
        if normalized_country:
            country_matches.update(by_name.get(normalized_country, set()))

    country_matches = keep_matching_region(country_matches)
    if len(country_matches) == 1:
        return country_matches, "country-normalized name"
    if len(country_matches) > 1:
        return set(), "ambiguous"

    base_matches = set()
    for source_name in display_names:
        if not source_name:
            continue
        countryless_name = strip_country_suffix(strip_country_prefix(source_name))
        normalized_base = normalize_base_name(countryless_name)
        if not normalized_base:
            continue
        for index in by_base_name.get(normalized_base, set()):
            playlist_names = {
                playlist_channels[index].get("title", ""),
                playlist_channels[index].get("tvg_name", ""),
            }
            if any(
                name and base_names_compatible(source_name, name)
                for name in playlist_names
            ):
                base_matches.add(index)

    base_matches = keep_matching_region(base_matches)
    if len(base_matches) == 1:
        return base_matches, "unique base name"
    if len(base_matches) > 1:
        return set(), "ambiguous"
    if region_filtered:
        return set(), "region-ambiguous"
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


SEQUENCE_FINGERPRINT_MIN_PROGRAMMES = 8
SEQUENCE_FINGERPRINT_MIN_MATCH_RATIO = 0.80
SEQUENCE_FINGERPRINT_MAX_OTHER_RATIO = 0.25


def programme_sequence_signature(title, start_dt, stop_dt=None):
    """Подпись передачи по нормализованному названию и старту; stop не учитывается."""
    if not title or start_dt is None:
        return None
    payload = "\x1f".join((
        normalize_name(title),
        start_dt.isoformat(),
    ))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).digest()


def scan_source_mapping_fingerprints(
    source_path, indexes, playlist_channels, period_start, period_end,
    excluded_playlist_indexes=None,
):
    """Первый проход: строит компактные fingerprints только для конфликтующих ID и их якорей."""
    excluded_playlist_indexes = set(excluded_playlist_indexes or ())
    mapped = {}
    fingerprints = defaultdict(set)
    relevant_ids = set()
    candidate_indexes = set()
    relevant_ids_ready = False
    root = None

    with open_xml_file(source_path) as xml_file:
        for event, elem in ET.iterparse(xml_file, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                continue
            tag = local_name(elem.tag)

            if tag == "channel":
                channel_id = elem.attrib.get("id", "").strip()
                display_names = [
                    "".join(item.itertext()).strip()
                    for item in elem
                    if local_name(item.tag) == "display-name"
                ]
                if channel_id:
                    matched_indexes, _ = map_source_channels(
                        channel_id, display_names, indexes, playlist_channels
                    )
                    matched_indexes -= excluded_playlist_indexes
                    if matched_indexes:
                        mapped.setdefault(channel_id, set()).update(matched_indexes)
                elem.clear()
                if root is not None:
                    root.clear()
                continue

            if tag != "programme":
                continue

            if not relevant_ids_ready:
                candidate_indexes = {
                    index
                    for candidate_set in mapped.values()
                    if len(candidate_set) > 1
                    for index in candidate_set
                }
                relevant_ids = {
                    channel_id
                    for channel_id, candidate_set in mapped.items()
                    if len(candidate_set) > 1
                    or (len(candidate_set) == 1 and next(iter(candidate_set)) in candidate_indexes)
                }
                relevant_ids_ready = True
                if not relevant_ids:
                    elem.clear()
                    if root is not None:
                        root.clear()
                    break

            channel_id = elem.attrib.get("channel", "").strip()
            if channel_id in relevant_ids:
                start_dt = parse_xmltv_datetime(elem.attrib.get("start", ""))
                stop_value = elem.attrib.get("stop")
                stop_dt = parse_xmltv_datetime(stop_value) if stop_value else None
                if stop_value and (stop_dt is None or start_dt is None or stop_dt <= start_dt):
                    start_dt = None
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
                    signature = programme_sequence_signature(title, start_dt, stop_dt)
                    if signature is not None:
                        fingerprints[channel_id].add(signature)
            elem.clear()
            if root is not None:
                root.clear()

    return mapped, fingerprints


def compare_ambiguous_sequence(channel_id, candidate_indexes, mapped, fingerprints):
    """Сравнивает неоднозначный ID с уникальными ID и прежними источниками каждого кандидата."""
    target = fingerprints.get(channel_id, set())
    if len(target) < SEQUENCE_FINGERPRINT_MIN_PROGRAMMES:
        return {"status": "insufficient", "evidence": ""}

    scores = {}
    for playlist_index in sorted(candidate_indexes):
        best = None
        for anchor_id, anchor_indexes in mapped.items():
            if anchor_id == channel_id or anchor_indexes != {playlist_index}:
                continue
            anchor = fingerprints.get(anchor_id, set())
            if len(anchor) < SEQUENCE_FINGERPRINT_MIN_PROGRAMMES:
                continue
            common = len(target & anchor)
            # Двустороннее покрытие: большой набор не должен считаться совпадением
            # только потому, что содержит весь меньший набор.
            ratio = common / max(len(target), len(anchor))
            candidate = (ratio, common, anchor_id, len(anchor))
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is not None:
            scores[playlist_index] = best

    evidence = " | ".join(
        f"{index}:anchor={score[2]},match={score[1]}/{max(len(target), score[3])},ratio={score[0]:.0%}"
        for index, score in sorted(scores.items())
    )
    if len(scores) != len(candidate_indexes):
        return {"status": "insufficient", "evidence": evidence}

    strong = {
        index: score for index, score in scores.items()
        if score[1] >= SEQUENCE_FINGERPRINT_MIN_PROGRAMMES
        and score[0] >= SEQUENCE_FINGERPRINT_MIN_MATCH_RATIO
    }
    if len(strong) == len(candidate_indexes):
        return {"status": "verified_shared", "evidence": evidence}
    if len(strong) == 1:
        winner, winner_score = next(iter(strong.items()))
        other_scores = [score[0] for index, score in scores.items() if index != winner]
        if all(score <= SEQUENCE_FINGERPRINT_MAX_OTHER_RATIO for score in other_scores):
            return {"status": "resolved", "winner": winner, "evidence": evidence}
    return {"status": "conflicting", "evidence": evidence}

def resolve_source_programme_overlaps(global_programmes, source_programmes):
    """Keeps higher-priority sources intact and maximizes non-overlapping
    programme count inside the current source without modifying start/stop."""
    existing_by_channel = defaultdict(list)
    for item in global_programmes.values():
        start, stop = item.get("start"), item.get("stop")
        if start is not None and stop is not None:
            insort(existing_by_channel[item["channel_index"]], (start, stop))

    candidates_by_channel = defaultdict(list)
    accepted = {}
    skipped = 0

    for identity, item in source_programmes.items():
        start, stop = item.get("start"), item.get("stop")
        channel_index = item["channel_index"]
        if start is None or stop is None:
            accepted[identity] = item
            continue

        intervals = existing_by_channel[channel_index]
        pos = bisect_left(intervals, (start,))
        conflict = (
            (pos > 0 and intervals[pos - 1][1] > start)
            or (pos < len(intervals) and intervals[pos][0] < stop)
        )
        if conflict:
            skipped += 1
        else:
            candidates_by_channel[channel_index].append((identity, item))

    # Earliest-finish-time yields the maximum number of compatible intervals.
    for candidates in candidates_by_channel.values():
        candidates.sort(key=lambda pair: (
            pair[1]["stop"], pair[1]["start"],
            normalize_name(pair[1].get("title", "")),
        ))
        last_stop = None
        for identity, item in candidates:
            if last_stop is None or item["start"] >= last_stop:
                accepted[identity] = item
                last_stop = item["stop"]
            else:
                skipped += 1

    return accepted, skipped


def process_source(
    source_name,
    source_url,
    playlist_channels,
    indexes,
    period_start,
    period_end,
    global_programmes,
    channel_icons,
    channel_icon_sources,
    exact_programmes_seen,
    merge_diagnostics,
    merge_collision_details,
    merge_text_details,
    duplicate_channel_id_details,
    channel_mapping_details,
    mapping_rule_details,
    excluded_playlist_indexes=None,
):
    excluded_playlist_indexes = set(excluded_playlist_indexes or ())
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
        "interval_conflicts_skipped": 0,
        "exact_duplicates": 0,
        "matched_by_tvg_id": 0,
        "matched_by_exact_name": 0,
        "matched_by_country_name": 0,
        "matched_by_base_name": 0,
        "ambiguous_channels": 0,
        "region_ambiguous_channels": 0,
        "ambiguous_xml_ids_quarantined": 0,
        "programmes_quarantined_by_ambiguous_xml_id": 0,
        "ambiguous_xml_ids_review": 0,
        "ambiguous_xml_ids_sequence_resolved": 0,
        "ambiguous_xml_ids_sequence_verified_shared": 0,
        "ambiguous_xml_ids_sequence_conflicting": 0,
        "unmatched_channels": 0,
        "first_programme": "",
        "last_programme": "",
        "new_channels_with_programmes": 0,
        "cumulative_gzip_bytes": 0,
        "marginal_gzip_bytes": 0,
    }

    # Один XMLTV ID может соответствовать нескольким записям плейлиста.
    mapped = {}
    source_channels_with_programmes = set()
    source_channel_ids = set()
    source_channel_id_counts = defaultdict(int)
    source_channel_display_names = defaultdict(set)
    channels_with_programmes = set()
    mapping_finalized = False
    ambiguous_xml_id_mappings = {}
    mapping_review_ids = set()
    mapping_filtered_ids = set()
    mapping_sequence_resolved_ids = set()
    mapping_sequence_conflict_review_ids = set()
    source_programmes = {}
    # Разные передачи с разным временем начала сохраняются даже при
    # пересечении интервалов. Пересечения анализируются отдельно после merge.

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name

    try:
        log(f"  Скачивание: {source_name}")
        size = download_to_file(source_url, temp_path, MAX_SOURCE_SIZE)
        log(f"  Получено: {size / 1024 / 1024:.1f} MB")

        sequence_mapped, sequence_fingerprints = scan_source_mapping_fingerprints(
            temp_path, indexes, playlist_channels, period_start, period_end,
            excluded_playlist_indexes,
        )

        # Для последующих источников используем уже принятые расписания как
        # независимые опорные последовательности конкретных playlist-каналов.
        # Это устраняет зависимость проверки исключительно от наличия
        # уникальных XMLTV ID-якорей внутри текущего источника.
        sequence_anchor_indexes = {
            index
            for candidate_set in sequence_mapped.values()
            if len(candidate_set) > 1
            for index in candidate_set
        } - excluded_playlist_indexes
        for playlist_index in sequence_anchor_indexes:
            anchor_id = f"__previous_source_playlist_{playlist_index}"
            anchor_signatures = {
                signature
                for programme in global_programmes.values()
                if programme.get("channel_index") == playlist_index
                for signature in (
                    programme_sequence_signature(
                        programme.get("title", ""),
                        programme.get("start"),
                        programme.get("stop"),
                    ),
                )
                if signature is not None
            }
            if anchor_signatures:
                sequence_mapped[anchor_id] = {playlist_index}
                sequence_fingerprints[anchor_id] = anchor_signatures

        sequence_fingerprint_count = sum(
            len(programme_signatures)
            for programme_signatures in sequence_fingerprints.values()
        )
        log(
            f"  Sequence fingerprints: {sequence_fingerprint_count:,} уникальных передач "
            f"для неоднозначных ID и контрольных каналов"
        )

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

                    audit = audit_mapping_candidates(display_names, indexes, playlist_channels)
                    mapping_rule_details.append({
                        "source": source_name,
                        "xml_channel_id": channel_id,
                        "xml_display_names": " | ".join(display_names),
                        **audit,
                        "mapping_status": "pending",
                        "mapping_playlist_indexes": "",
                        "mapping_playlist_names": "",
                        "sequence_fingerprint_status": "not evaluated",
                        "sequence_fingerprint_evidence": "",
                    })
                    matched_indexes, method = map_source_channels(
                        channel_id,
                        display_names,
                        indexes,
                        playlist_channels,
                    )
                    # Каждый playlist-канал закрепляется за первым источником,
                    # который смог его сопоставить. Последующие источники могут
                    # обрабатывать только ещё не закреплённые каналы.
                    matched_indexes -= excluded_playlist_indexes

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
                                if playlist_index not in channel_icons:
                                    channel_icons[playlist_index] = icon_src
                                    channel_icon_sources[playlist_index] = source_name

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
                    elif method == "region-ambiguous":
                        stats["region_ambiguous_channels"] += 1
                        stats["unmatched_channels"] += 1
                    else:
                        stats["unmatched_channels"] += 1

                elif tag == "programme":
                    # XMLTV channel declarations normally precede programme entries.
                    # Wait until all declarations are read before accepting a reused ID.
                    if not mapping_finalized:
                        for mapped_id, candidate_indexes in list(mapped.items()):
                            if len(candidate_indexes) < 2:
                                continue

                            # XMLTV display-name может сузить общий tvg-id до тех
                            # playlist-каналов, чьи названия действительно совпадают.
                            # +N и регион в скобках трактуются как вариант канала.
                            source_names = source_channel_display_names.get(mapped_id, set())
                            source_keys = {
                                canonical_mapping_name(name)
                                for name in source_names
                                if canonical_mapping_name(name)
                            }
                            matching_indexes = set()
                            matched_keys_by_index = {}
                            candidate_name_keys = set()
                            for playlist_index in candidate_indexes:
                                playlist_channel = playlist_channels[playlist_index]
                                playlist_keys = {
                                    canonical_mapping_name(name)
                                    for name in (
                                        playlist_channel.get("title", ""),
                                        playlist_channel.get("tvg_name", ""),
                                    )
                                    if canonical_mapping_name(name)
                                }
                                candidate_name_keys.update(playlist_keys)
                                matched_keys = source_keys & playlist_keys
                                if matched_keys:
                                    matching_indexes.add(playlist_index)
                                    matched_keys_by_index[playlist_index] = matched_keys

                            # Один XMLTV ID нельзя раздавать нескольким логически
                            # разным каналам. Региональные +0 остаются одним ключом,
                            # но +2/+4/+7 и разные названия сохраняют различия.
                            matched_logical_keys = {
                                key
                                for keys in matched_keys_by_index.values()
                                for key in keys
                            }
                            sequence_result = compare_ambiguous_sequence(
                                mapped_id,
                                candidate_indexes,
                                sequence_mapped,
                                sequence_fingerprints,
                            )
                            for detail in mapping_rule_details:
                                if detail.get("source") == source_name and detail.get("xml_channel_id") == mapped_id:
                                    detail["sequence_fingerprint_status"] = sequence_result["status"]
                                    detail["sequence_fingerprint_evidence"] = sequence_result.get("evidence", "")

                            sequence_conflict = sequence_result["status"] == "conflicting"
                            if sequence_result["status"] == "resolved":
                                winner = sequence_result["winner"]
                                matching_indexes = {winner}
                                matched_keys_by_index = {
                                    winner: matched_keys_by_index.get(winner, set())
                                }
                                matched_logical_keys = set(matched_keys_by_index[winner])
                                mapping_sequence_resolved_ids.add(mapped_id)
                                if {winner} != candidate_indexes:
                                    mapping_filtered_ids.add(mapped_id)
                                stats["ambiguous_xml_ids_sequence_resolved"] += 1
                                merge_diagnostics["ambiguous_xml_ids_sequence_resolved"] += 1
                            elif sequence_result["status"] == "verified_shared":
                                matching_indexes = set(candidate_indexes)
                                stats["ambiguous_xml_ids_sequence_verified_shared"] += 1
                                merge_diagnostics["ambiguous_xml_ids_sequence_verified_shared"] += 1
                            elif sequence_conflict:
                                stats["ambiguous_xml_ids_sequence_conflicting"] += 1
                                merge_diagnostics["ambiguous_xml_ids_sequence_conflicting"] += 1

                            # Если display-name однозначно указывает на один канал,
                            # конфликт fingerprint не должен удалять его EPG.
                            # Сохраняем единственное именное совпадение и помечаем
                            # его для ручной проверки; quarantine нужен только когда
                            # последовательность конфликтует при нескольких/нулевых
                            # подтверждённых кандидатах.
                            if sequence_conflict and len(matching_indexes) == 1:
                                mapping_sequence_conflict_review_ids.add(mapped_id)

                            # Один XMLTV ID может обслуживать SD/HD-варианты
                            # одного логического канала. Совместное назначение безопасно,
                            # когда все подтверждённые кандидаты имеют один canonical key.
                            conflicting_matches = (
                                (
                                    sequence_conflict
                                    and (
                                        not matching_indexes
                                        or len(matched_logical_keys) > 1
                                    )
                                )
                                or (
                                    sequence_result["status"] != "verified_shared"
                                    and len(matching_indexes) > 1
                                    and len(matched_logical_keys) > 1
                                )
                            )
                            no_safe_match = (
                                not matching_indexes
                                and len(candidate_name_keys) > 1
                            )

                            if conflicting_matches or no_safe_match:
                                mapped[mapped_id] = set()
                                ambiguous_xml_id_mappings[mapped_id] = True
                                stats["ambiguous_xml_ids_quarantined"] += 1
                                merge_diagnostics["ambiguous_xml_ids_quarantined"] += 1
                            elif matching_indexes:
                                # Оставляем только подтверждённые имена.
                                if matching_indexes != candidate_indexes:
                                    mapping_filtered_ids.add(mapped_id)
                                mapped[mapped_id] = matching_indexes

                            # Несколько playlist-записей с одним логическим именем
                            # (например, региональные +0) допустимы, но требуют аудита.
                            if (
                                mapped[mapped_id]
                                and len(mapped[mapped_id]) > 1
                                and sequence_result["status"] != "verified_shared"
                            ):
                                mapping_review_ids.add(mapped_id)
                            elif not matching_indexes and mapped[mapped_id]:
                                mapping_review_ids.add(mapped_id)

                        for detail in mapping_rule_details:
                            if detail.get("source") != source_name:
                                continue
                            detail_id = detail.get("xml_channel_id", "")
                            final_indexes = sorted(mapped.get(detail_id, set()))
                            detail["mapping_playlist_indexes"] = " | ".join(
                                str(i) for i in final_indexes
                            )
                            detail["mapping_playlist_names"] = " | ".join(
                                playlist_channels[i]["title"] for i in final_indexes
                            )
                            if detail_id in ambiguous_xml_id_mappings:
                                detail["mapping_status"] = (
                                    "quarantined: conflicting channel names or programme sequences; "
                                    "programmes not assigned"
                                )
                            elif detail.get("sequence_fingerprint_status") == "verified_shared":
                                detail["mapping_status"] = (
                                    "verified: programme sequences match for all mapped candidates"
                                )
                            elif detail_id in mapping_sequence_resolved_ids:
                                detail["mapping_status"] = "accepted: narrowed by programme sequence"
                            elif detail_id in mapping_sequence_conflict_review_ids:
                                detail["mapping_status"] = (
                                    "review: unique display-name match preserved despite sequence conflict"
                                )
                            elif detail_id in mapping_review_ids:
                                detail["mapping_status"] = (
                                    "review: shared XMLTV ID maps to same logical channel; "
                                    "programmes preserved"
                                )
                            elif final_indexes and detail_id in mapping_filtered_ids:
                                detail["mapping_status"] = "accepted: narrowed by display-name"
                            elif final_indexes:
                                detail["mapping_status"] = "accepted"
                            else:
                                detail["mapping_status"] = "unmatched or ambiguous"

                        # Неоднозначные ID с конфликтующими именами изолируются,
                        # чтобы их расписание не попадало на чужие каналы.
                        # Количество непривязанных передач считается при чтении programme.
                        stats["ambiguous_xml_ids_review"] = len(mapping_review_ids)
                        merge_diagnostics["ambiguous_xml_ids_review"] += len(mapping_review_ids)
                        mapping_finalized = True

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

                            if channel_id in ambiguous_xml_id_mappings:
                                stats["programmes_quarantined_by_ambiguous_xml_id"] += 1
                                merge_diagnostics["programmes_quarantined_by_ambiguous_xml_id"] += 1

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

                                            # Сохраняем реальные значения только для конфликтов
                                            # разных XMLTV ID внутри одного источника.
                                            # На итоговое слияние этот диагностический сбор не влияет.
                                            if existing_xml_id != channel_id:
                                                merge_text_details.append({
                                                    "source": source_name,
                                                    "playlist_channel": playlist_channels[index]["title"],
                                                    "playlist_index": index,
                                                    "start": start_dt.isoformat(),
                                                    "existing_xml_channel_id": existing_xml_id,
                                                    "incoming_xml_channel_id": channel_id,
                                                    "existing_stop": (
                                                        existing["stop"].isoformat()
                                                        if existing.get("stop") is not None else ""
                                                    ),
                                                    "incoming_stop": (
                                                        stop_dt.isoformat() if stop_dt is not None else ""
                                                    ),
                                                    "existing_title": existing["title"] or "",
                                                    "incoming_title": title or "",
                                                    "existing_desc": existing["description"] or "",
                                                    "incoming_desc": description or "",
                                                    "existing_categories": " | ".join(existing["categories"]),
                                                    "incoming_categories": " | ".join(categories),
                                                })
                                        if not same_source and (
                                            title != existing["title"] or stop_dt != existing.get("stop")
                                        ):
                                            merge_text_details.append({
                                                "source": source_name,
                                                "existing_source": existing.get("source", ""),
                                                "playlist_channel": playlist_channels[index]["title"],
                                                "playlist_index": index,
                                                "start": start_dt.isoformat(),
                                                "existing_xml_channel_id": existing_xml_id,
                                                "incoming_xml_channel_id": channel_id,
                                                "existing_stop": (
                                                    existing["stop"].isoformat()
                                                    if existing.get("stop") is not None else ""
                                                ),
                                                "incoming_stop": (
                                                    stop_dt.isoformat() if stop_dt is not None else ""
                                                ),
                                                "existing_title": existing["title"] or "",
                                                "incoming_title": title or "",
                                                "existing_desc": existing["description"] or "",
                                                "incoming_desc": description or "",
                                                "existing_categories": " | ".join(existing["categories"]),
                                                "incoming_categories": " | ".join(categories),
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
                                        # Не отбрасываем запись только из-за пересечения
                                        # интервала с другой передачей. Расписание источников
                                        # может содержать альтернативные/региональные эфиры;
                                        # все записи с разным start сохраняем для диагностики.
                                        accepted_programme = {
                                            "channel_index": index,
                                            "start": start_dt,
                                            "stop": stop_dt,
                                            "title": title,
                                            "description": description,
                                            "categories": categories,
                                            "source": source_name,
                                            "xml_channel_id": channel_id,
                                        }
                                        source_programmes[identity] = accepted_programme

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

        # Помечаем в диагностике только XMLTV-каналы, у которых
        # действительно есть передачи в проверяемом периоде.
        for detail in mapping_rule_details:
            if detail.get("source") == source_name:
                detail["has_programmes"] = detail.get("xml_channel_id", "") in source_channels_with_programmes

        # Источники с меньшим номером имеют абсолютный приоритет.
        # В текущем источнике выбираем максимальное число передач,
        # которые не пересекаются с уже принятыми интервалами.
        source_programmes, skipped_overlaps = resolve_source_programme_overlaps(
            global_programmes, source_programmes
        )
        stats["interval_conflicts_skipped"] = skipped_overlaps
        merge_diagnostics["interval_conflicts_skipped"] += skipped_overlaps

        accepted_indexes = {
            item["channel_index"] for item in source_programmes.values()
        }
        stats["matched_playlist_channels"] = len(all_matched_indexes)
        stats["xml_channels_with_programmes"] = len(source_channels_with_programmes)
        stats["channels_with_programmes"] = len(accepted_indexes)
        stats["_channel_indexes_with_programmes"] = sorted(accepted_indexes)
        stats["_channel_indexes_matched"] = sorted(all_matched_indexes)
        stats["unique_programmes"] = len(source_programmes)
        stats["status"] = "OK"

        # Сливаем источник только после завершения его разбора и фильтрации.
        global_programmes.update(source_programmes)

        log(
            f"  Результат: XMLTV-каналов с программами "
            f"{stats['xml_channels_with_programmes']}; "
            f"сопоставлено с плейлистом {stats['matched_playlist_channels']}; "
            f"каналов плейлиста с программами "
            f"{stats['channels_with_programmes']}; "
            f"новых передач {stats['unique_programmes']}; "
            f"дубликатов {stats['duplicates_skipped']}; "
            f"пересекающихся передач пропущено {stats['interval_conflicts_skipped']}"
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
        "interval_conflicts_skipped",
        "exact_duplicates",
        "matched_by_tvg_id",
        "matched_by_exact_name",
        "matched_by_country_name",
        "matched_by_base_name",
        "ambiguous_channels",
        "region_ambiguous_channels",
        "ambiguous_xml_ids_quarantined",
        "programmes_quarantined_by_ambiguous_xml_id",
        "ambiguous_xml_ids_review",
        "ambiguous_xml_ids_sequence_resolved",
        "ambiguous_xml_ids_sequence_verified_shared",
        "ambiguous_xml_ids_sequence_conflicting",
        "unmatched_channels",
        "first_programme",
        "last_programme",
        "new_channels_with_programmes",
        "cumulative_gzip_bytes",
        "marginal_gzip_bytes",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(stats_list)

def write_channel_report(channels, global_programmes, logo_diagnostics):
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
        "m3u_logo_present",
        "epg_logo_present",
        "epg_logo_source",
        "logo_source_comparison",
        "m3u_logo_sha256",
        "epg_logo_sha256",
        "m3u_logo_status",
        "m3u_logo_dimensions",
        "m3u_logo_shape",
        "m3u_logo_transparency",
        "m3u_logo_black_background",
        "epg_logo_status",
        "epg_logo_dimensions",
        "epg_logo_shape",
        "epg_logo_transparency",
        "epg_logo_black_background",
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
                **logo_diagnostics.get(index, {}),
            })

def write_combined_xml(
    channels,
    global_programmes,
    channel_icons,
    description_limit=None,
    output_path=None,
):
    path = output_path or (OUTPUT_DIR / "epg_custom.xml.gz")
    uncompressed_size = 0

    # Пишем XML потоково и параллельно считаем точный размер XML до GZIP.
    with gzip.open(path, "wb", compresslevel=6) as file:
        def write_xml(data):
            nonlocal uncompressed_size
            file.write(data)
            uncompressed_size += len(data)

        write_xml(b'<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="EPG-EDEM">')

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


def validate_generated_xml(path, expected_channels, expected_programmes):
    """Fully reads the generated archive and validates channel references/times."""
    channel_ids = set()
    channel_count = 0
    programme_count = 0
    root = None

    with gzip.open(path, "rb") as source:
        for event, element in ET.iterparse(source, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = element
                continue

            tag = local_name(element.tag)
            if tag == "channel":
                channel_id = element.attrib.get("id", "").strip()
                if not channel_id or channel_id in channel_ids:
                    raise RuntimeError(f"Missing or duplicate channel ID: {channel_id!r}")
                channel_ids.add(channel_id)
                channel_count += 1
            elif tag == "programme":
                channel_id = element.attrib.get("channel", "").strip()
                if channel_id not in channel_ids:
                    raise RuntimeError(f"Programme references unknown channel: {channel_id!r}")
                start = parse_xmltv_datetime(element.attrib.get("start", ""))
                stop_value = element.attrib.get("stop")
                stop = parse_xmltv_datetime(stop_value) if stop_value else None
                if start is None or (
                    stop_value is not None and (stop is None or stop <= start)
                ):
                    raise RuntimeError("Invalid programme start/stop in generated XML.")
                programme_count += 1

            if tag in ("channel", "programme"):
                element.clear()
                if root is not None:
                    root.clear()

    if channel_count != expected_channels or programme_count != expected_programmes:
        raise RuntimeError(
            "Generated XML count mismatch: "
            f"channels {channel_count}/{expected_channels}, "
            f"programmes {programme_count}/{expected_programmes}."
        )
    return channel_count, programme_count


def ensure_pillow():
    """Гарантирует Pillow для анализа прозрачности и пикселей фона."""
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        log("  Pillow не установлен; установка для анализа изображений...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "Pillow"],
                check=True,
                timeout=180,
            )
            import PIL  # noqa: F401
            return True
        except Exception as error:
            log(f"  Не удалось установить Pillow: {type(error).__name__}")
            return False


def inspect_logo_image(url):
    """Загружает и анализирует одно изображение; URL не записывается в отчёт."""
    result = {
        "status": "missing",
        "width": "",
        "height": "",
        "shape": "",
        "transparency": "",
        "black_background": "",
    }
    if not url:
        return result
    if not url.lower().startswith(("http://", "https://")):
        result["status"] = "unsupported_url_scheme"
        return result

    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        data = bytearray()
        with urllib.request.urlopen(request, timeout=15) as response:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    result["status"] = "too_large"
                    return result

        if not data:
            result["status"] = "empty"
            return result

        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        Image.MAX_IMAGE_PIXELS = 40_000_000

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            result["width"] = width
            result["height"] = height
            ratio = width / height if height else 0
            result["shape"] = (
                "square" if 0.90 <= ratio <= 1.10
                else "landscape" if ratio > 1.10
                else "portrait"
            )

            rgba = image.convert("RGBA")
            alpha_min, _ = rgba.getchannel("A").getextrema()
            result["transparency"] = "yes" if alpha_min < 255 else "no"

            # Чёрный фон — эвристика: не менее 85% непрозрачных пикселей
            # в полосе шириной 4% по краям имеют RGB не выше 40.
            sample = rgba.copy()
            sample.thumbnail((256, 256))
            pixels = sample.load()
            sw, sh = sample.size
            border = max(1, min(sw, sh) // 25)
            opaque = 0
            dark = 0
            for y in range(sh):
                for x in range(sw):
                    if x < border or x >= sw - border or y < border or y >= sh - border:
                        red, green, blue, alpha = pixels[x, y]
                        if alpha >= 128:
                            opaque += 1
                            if max(red, green, blue) <= 40:
                                dark += 1
            result["black_background"] = (
                "yes" if opaque and dark / opaque >= 0.85
                else "no" if opaque
                else "indeterminate"
            )
            result["status"] = "ok"
            return result

    except HTTPError as error:
        result["status"] = f"http_{error.code}"
    except (TimeoutError, socket.timeout):
        result["status"] = "timeout"
    except Exception:
        result["status"] = "invalid_image"
    return result


def analyze_channel_logos(channels, channel_icons, channel_icon_sources):
    """Кеширует загрузку по URL и классифицирует логотипы из M3U и EPG."""
    urls = set()
    for index, channel in enumerate(channels):
        m3u_url = channel.get("tvg_logo", "")
        epg_url = channel_icons.get(index, "")
        if m3u_url:
            urls.add(m3u_url)
        if epg_url:
            urls.add(epg_url)

    url_hashes = {
        url: hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        for url in urls
    }
    image_results = {}
    log(f"  Уникальных URL логотипов для анализа: {len(urls)}")
    if urls and not ensure_pillow():
        image_results = {
            url: {
                "status": "pillow_unavailable",
                "width": "", "height": "", "shape": "",
                "transparency": "", "black_background": "",
            }
            for url in urls
        }
    elif urls:
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(inspect_logo_image, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                image_results[url] = future.result()

    per_channel = {}
    for index, channel in enumerate(channels):
        m3u_url = channel.get("tvg_logo", "")
        epg_url = channel_icons.get(index, "")
        m3u_info = image_results.get(m3u_url, {})
        epg_info = image_results.get(epg_url, {})
        if m3u_url and epg_url:
            comparison = "both"
        elif m3u_url:
            comparison = "M3U only"
        elif epg_url:
            comparison = "EPG only"
        else:
            comparison = "no logo"

        def dimension(info):
            return (
                f"{info.get('width')}x{info.get('height')}"
                if info.get("width") and info.get("height") else ""
            )

        per_channel[index] = {
            "m3u_logo_present": bool(m3u_url),
            "epg_logo_present": bool(epg_url),
            "epg_logo_source": channel_icon_sources.get(index, ""),
            "logo_source_comparison": comparison,
            "m3u_logo_sha256": url_hashes.get(m3u_url, ""),
            "epg_logo_sha256": url_hashes.get(epg_url, ""),
            "m3u_logo_status": m3u_info.get("status", ""),
            "m3u_logo_dimensions": dimension(m3u_info),
            "m3u_logo_shape": m3u_info.get("shape", ""),
            "m3u_logo_transparency": m3u_info.get("transparency", ""),
            "m3u_logo_black_background": m3u_info.get("black_background", ""),
            "epg_logo_status": epg_info.get("status", ""),
            "epg_logo_dimensions": dimension(epg_info),
            "epg_logo_shape": epg_info.get("shape", ""),
            "epg_logo_transparency": epg_info.get("transparency", ""),
            "epg_logo_black_background": epg_info.get("black_background", ""),
        }

    m3u_urls = {channel.get("tvg_logo", "") for channel in channels if channel.get("tvg_logo")}
    epg_urls = set(channel_icons.values())
    m3u_channels = sum(bool(channel.get("tvg_logo")) for channel in channels)
    epg_channels = len(channel_icons)
    both_channels = sum(
        bool(channel.get("tvg_logo")) and index in channel_icons
        for index, channel in enumerate(channels)
    )

    lines = [
        "",
        "ДИАГНОСТИКА ЛОГОТИПОВ КАНАЛОВ",
        "  ВАЖНО: итоговый XML сейчас получает иконки только из EPG; tvg-logo из M3U не подставляется.",
        f"  Каналов с tvg-logo в M3U: {m3u_channels}; уникальных URL: {len(m3u_urls)}",
        f"  Каналов с иконкой из EPG: {epg_channels}; уникальных URL: {len(epg_urls)}",
        f"  Иконки есть и в M3U, и в EPG: {both_channels}",
        f"  Только M3U: {m3u_channels - both_channels}",
        f"  Только EPG: {epg_channels - both_channels}",
        f"  Без логотипа в обоих источниках: {len(channels) - m3u_channels - epg_channels + both_channels}",
        "  Классификация изображений по уникальным URL (один URL анализируется один раз):",
        f"    Успешно разобрано: {sum(info['status'] == 'ok' for info in image_results.values())}",
        f"    Ошибки/недоступны: {sum(info['status'] != 'ok' for info in image_results.values())}",
    ]

    for label, selected_urls in (("M3U", m3u_urls), ("EPG", epg_urls)):
        selected = [image_results[url] for url in selected_urls if url in image_results]
        ok = [info for info in selected if info.get("status") == "ok"]
        lines.append(f"  {label}: уникальных URL {len(selected_urls)}; успешно {len(ok)}; ошибок {len(selected) - len(ok)}")
        for key, title in (
            ("square", "квадратных"), ("landscape", "альбомных"), ("portrait", "портретных"),
        ):
            lines.append(f"    {title}: {sum(info.get('shape') == key for info in ok)}")
        lines.append(f"    прозрачных: {sum(info.get('transparency') == 'yes' for info in ok)}")
        lines.append(f"    непрозрачных: {sum(info.get('transparency') == 'no' for info in ok)}")
        lines.append(f"    чёрный фон (эвристика): {sum(info.get('black_background') == 'yes' for info in ok)}")
        lines.append(f"    фон не определён: {sum(info.get('black_background') == 'indeterminate' for info in ok)}")

    owner_counts = defaultdict(int)
    for source in channel_icon_sources.values():
        owner_counts[source] += 1
    lines.append("  Каналов, чьи EPG-логотипы впервые предоставил источник:")
    for source, _ in SOURCES:
        lines.append(f"    {source}: {owner_counts.get(source, 0)}")

    for label, prefix in (("M3U", "m3u"), ("EPG", "epg")):
        records = [
            item for item in per_channel.values()
            if item[f"{prefix}_logo_present"]
        ]
        lines.append(
            f"  {label} по каналам (повторные URL учитываются для каждого канала): "
            f"прозрачных {sum(item[f'{prefix}_logo_transparency'] == 'yes' for item in records)}, "
            f"чёрный фон {sum(item[f'{prefix}_logo_black_background'] == 'yes' for item in records)}, "
            f"квадратных {sum(item[f'{prefix}_logo_shape'] == 'square' for item in records)}, "
            f"альбомных {sum(item[f'{prefix}_logo_shape'] == 'landscape' for item in records)}, "
            f"портретных {sum(item[f'{prefix}_logo_shape'] == 'portrait' for item in records)}"
        )

    return per_channel, lines


def write_diagnostic_csv(filename, rows, fieldnames):
    path = OUTPUT_DIR / filename
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_uncovered_channel_candidates(channels, global_programmes, mapping_rule_details):
    """
    Для каждого канала плейлиста без передач показывает кандидатов
    среди XMLTV-каналов, у которых есть передачи в проверяемом периоде.
    Поиск диагностический: на сопоставление и итоговый EPG не влияет.
    """
    covered = {
        item["channel_index"]
        for item in global_programmes.values()
    }
    rows = []
    candidate_playlist_indexes = set()

    for playlist_index, channel in enumerate(channels):
        if playlist_index in covered:
            continue

        found = {}
        for detail in mapping_rule_details:
            if not detail.get("has_programmes"):
                continue

            tiers = (
                ("exact name", detail.get("exact_indexes", "")),
                ("country-normalized name", detail.get("country_indexes", "")),
                ("compatible base name", detail.get("base_indexes", "")),
            )
            matched_rule = ""
            for rule, raw_indexes in tiers:
                candidate_indexes = {
                    int(value.strip())
                    for value in raw_indexes.split("|")
                    if value.strip().isdigit()
                }
                if playlist_index in candidate_indexes:
                    matched_rule = rule
                    break

            if not matched_rule:
                continue

            key = (
                detail.get("source", ""),
                detail.get("xml_channel_id", ""),
                detail.get("xml_display_names", ""),
                matched_rule,
            )
            found[key] = True

        if found:
            candidate_playlist_indexes.add(playlist_index)
            for source, xml_id, xml_names, rule in sorted(found):
                rows.append({
                    "playlist_index": playlist_index,
                    "playlist_channel": channel.get("title", ""),
                    "playlist_group": channel.get("group", ""),
                    "candidate_status": "candidate_with_programmes",
                    "match_rule": rule,
                    "source": source,
                    "xml_channel_id": xml_id,
                    "xml_display_names": xml_names,
                })
        else:
            rows.append({
                "playlist_index": playlist_index,
                "playlist_channel": channel.get("title", ""),
                "playlist_group": channel.get("group", ""),
                "candidate_status": "no_candidate_with_programmes",
                "match_rule": "",
                "source": "",
                "xml_channel_id": "",
                "xml_display_names": "",
            })

    fieldnames = [
        "playlist_index", "playlist_channel", "playlist_group",
        "candidate_status", "match_rule", "source",
        "xml_channel_id", "xml_display_names",
    ]
    write_diagnostic_csv("uncovered_channel_candidates.csv", rows, fieldnames)
    return len(channels) - len(covered), len(candidate_playlist_indexes)


def write_diagnostic_csv_gzip(filename, rows, fieldnames):
    """Записывает подробную диагностику CSV в GZIP, чтобы не раздувать репозиторий."""
    path = OUTPUT_DIR / filename
    with gzip.open(
        path,
        "wt",
        encoding="utf-8-sig",
        newline="",
        compresslevel=6,
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path.stat().st_size


def analyze_programme_interval_overlaps(global_programmes):
    """Ищет и классифицирует пересечения интервалов передач одного канала."""
    by_channel = defaultdict(list)
    missing_stop = 0
    for item in global_programmes.values():
        if item.get("stop") is None:
            missing_stop += 1
        by_channel[item["channel_index"]].append(item)

    overlaps = []
    same_source = 0
    cross_source = 0
    channel_overlap_counts = defaultdict(int)
    source_pair_counts = defaultdict(int)
    source_pair_analysis = defaultdict(lambda: {
        "total": 0, "exact_title": 0, "normalized_title": 0,
        "different_title": 0, "overlap_minutes": 0.0,
        "start_delta_minutes": defaultdict(int),
    })
    same_source_xml_id_counts = defaultdict(lambda: {"same_xml_id": 0, "different_xml_id": 0})
    title_relation_counts = defaultdict(int)
    overlap_duration_buckets = defaultdict(int)
    start_delta_buckets = defaultdict(int)
    channel_source_counts = defaultdict(int)
    xml_id_pair_analysis = defaultdict(lambda: {
        "total": 0, "exact_title": 0, "normalized_title": 0,
        "different_title": 0, "overlap_minutes": 0.0,
    })

    def bucket(value, limits):
        for upper, label in limits:
            if value <= upper:
                return label
        return limits[-1][1].replace("≤", ">")

    duration_limits = [(5, "≤5 мин"), (15, "6–15 мин"), (30, "16–30 мин"), (60, "31–60 мин"), (120, "61–120 мин"), (10**12, ">120 мин")]
    delta_limits = [(5, "≤5 мин"), (15, "6–15 мин"), (30, "16–30 мин"), (60, "31–60 мин"), (120, "61–120 мин"), (10**12, ">120 мин")]

    for channel_index, items in by_channel.items():
        items.sort(key=lambda item: item["start"])
        active = []
        for current in items:
            start = current["start"]
            active = [
                previous for previous in active
                if previous.get("stop") is not None and previous["stop"] > start
            ]
            if current.get("stop") is not None:
                for previous in active:
                    overlap_start = max(previous["start"], current["start"])
                    overlap_end = min(previous["stop"], current["stop"])
                    if overlap_start >= overlap_end:
                        continue
                    first_source = previous.get("source", "")
                    second_source = current.get("source", "")
                    same = first_source == second_source
                    same_source += int(same)
                    cross_source += int(not same)
                    channel_overlap_counts[channel_index] += 1
                    source_pair = tuple(sorted((first_source, second_source)))
                    source_pair_counts[source_pair] += 1
                    channel_source_counts[(channel_index, source_pair)] += 1

                    first_title = previous.get("title", "") or ""
                    second_title = current.get("title", "") or ""
                    first_norm = normalize_name(first_title)
                    second_norm = normalize_name(second_title)
                    if first_title.casefold().strip() == second_title.casefold().strip():
                        title_relation = "exact_title"
                    elif first_norm and first_norm == second_norm:
                        title_relation = "normalized_title"
                    else:
                        title_relation = "different_title"
                    title_relation_counts[title_relation] += 1

                    overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60
                    start_delta_minutes = abs((current["start"] - previous["start"]).total_seconds()) / 60
                    overlap_duration_buckets[bucket(overlap_minutes, duration_limits)] += 1
                    start_delta_buckets[bucket(start_delta_minutes, delta_limits)] += 1
                    pair_stats = source_pair_analysis[source_pair]
                    pair_stats["total"] += 1
                    pair_stats[title_relation] += 1
                    pair_stats["overlap_minutes"] += overlap_minutes
                    pair_stats["start_delta_minutes"][bucket(start_delta_minutes, delta_limits)] += 1

                    first_xml_id = previous.get("xml_channel_id", "") or ""
                    second_xml_id = current.get("xml_channel_id", "") or ""
                    id_pair = tuple(sorted(((first_source, first_xml_id), (second_source, second_xml_id))))
                    id_pair_stats = xml_id_pair_analysis[(channel_index, id_pair)]
                    id_pair_stats["total"] += 1
                    id_pair_stats[title_relation] += 1
                    id_pair_stats["overlap_minutes"] += overlap_minutes
                    if same:
                        same_source_xml_id_counts[first_source][
                            "same_xml_id" if first_xml_id == second_xml_id else "different_xml_id"
                        ] += 1

                    overlaps.append({
                        "playlist_index": channel_index,
                        "first_source": first_source,
                        "second_source": second_source,
                        "first_xml_channel_id": first_xml_id,
                        "second_xml_channel_id": second_xml_id,
                        "first_start": previous["start"].isoformat(),
                        "first_stop": previous["stop"].isoformat(),
                        "first_title": first_title,
                        "second_start": current["start"].isoformat(),
                        "second_stop": current["stop"].isoformat(),
                        "second_title": second_title,
                        "overlap_minutes": round(overlap_minutes, 2),
                        "start_delta_minutes": round(start_delta_minutes, 2),
                        "title_relation": title_relation,
                    })
                active.append(current)

    return {
        "overlaps": overlaps,
        "count": len(overlaps),
        "same_source": same_source,
        "cross_source": cross_source,
        "missing_stop": missing_stop,
        "channel_counts": sorted(channel_overlap_counts.items(), key=lambda item: (-item[1], item[0])),
        "source_pair_counts": sorted(source_pair_counts.items(), key=lambda item: (-item[1], item[0])),
        "source_pair_analysis": sorted(source_pair_analysis.items(), key=lambda item: (-item[1]["total"], item[0])),
        "same_source_xml_id_counts": dict(same_source_xml_id_counts),
        "title_relation_counts": dict(title_relation_counts),
        "overlap_duration_buckets": dict(overlap_duration_buckets),
        "start_delta_buckets": dict(start_delta_buckets),
        "channel_source_counts": sorted(channel_source_counts.items(), key=lambda item: (-item[1], item[0])),
        "xml_id_pair_analysis": sorted(xml_id_pair_analysis.items(), key=lambda item: (-item[1]["total"], item[0])),
    }


def write_epg_diagnostics(
    channels,
    global_programmes,
    channel_icons,
    period_start,
    period_end,
    xml_sizes,
    exact_programmes_seen,
    merge_diagnostics,
    interval_diagnostics,
    adjustment_diagnostics,
    final_interval_diagnostics,
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

    overlap_size = write_diagnostic_csv_gzip(
        "programme_interval_overlaps.csv.gz",
        interval_diagnostics["overlaps"],
        [
            "playlist_index", "first_source", "second_source",
            "first_xml_channel_id", "second_xml_channel_id",
            "first_start", "first_stop", "first_title",
            "second_start", "second_stop", "second_title",
            "overlap_minutes", "start_delta_minutes", "title_relation",
        ],
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
        f"  XMLTV ID, изолированных из-за неоднозначности: {merge_diagnostics.get('ambiguous_xml_ids_quarantined', 0)}",
        f"  Передач, не назначенных из-за неоднозначности XMLTV ID: {merge_diagnostics.get('programmes_quarantined_by_ambiguous_xml_id', 0)}",
        f"  XMLTV ID оставлено на ручную проверку без удаления EPG: {merge_diagnostics.get('ambiguous_xml_ids_review', 0)}",
        f"  Неоднозначных ID разрешено по последовательности передач: {merge_diagnostics.get('ambiguous_xml_ids_sequence_resolved', 0)}",
        f"  Неоднозначных ID подтверждено одинаковой последовательностью для всех кандидатов: {merge_diagnostics.get('ambiguous_xml_ids_sequence_verified_shared', 0)}",
        f"  Неоднозначных ID с конфликтующей последовательностью: {merge_diagnostics.get('ambiguous_xml_ids_sequence_conflicting', 0)}",
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
        "ПЕРЕСЕЧЕНИЯ ИНТЕРВАЛОВ ПЕРЕДАЧ НА ОДНОМ КАНАЛЕ:",
        f"  Всего пересекающихся пар: {interval_diagnostics['count']}",
        f"  Внутри одного источника: {interval_diagnostics['same_source']}",
        f"  Между разными источниками: {interval_diagnostics['cross_source']}",
        f"  Передач из источников с более низким приоритетом пропущено из-за пересечения: {merge_diagnostics.get('interval_conflicts_skipped', 0):,}",
        f"  Передач без stop (не проверяются на пересечение): {interval_diagnostics['missing_stop']}",
        f"  Полный список пересечений: programme_interval_overlaps.csv.gz ({overlap_size:,} байт)",
        "",
        "СОХРАННОСТЬ ИНТЕРВАЛОВ (исходные start/stop не изменяются):",
        f"  Передач с принудительно сокращённым stop: {adjustment_diagnostics['adjusted_programmes']:,}",
        f"  Искусственно удалено эфирного времени: {adjustment_diagnostics['truncated_minutes'] / 60:.1f} ч",
        f"  Максимальное искусственное сокращение одной передачи: {adjustment_diagnostics['max_truncation_minutes']:.1f} мин",
        f"  Пересечений в итоговом EPG: {final_interval_diagnostics['count']:,}",
        "  По источникам: " + "; ".join(f"{name} — {count}" for name, count in sorted(adjustment_diagnostics["by_source"].items())),
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

    lines.extend([
        "",
        f"Каналов с пересекающимися передачами: {len(interval_diagnostics['channel_counts'])}",
        "",
        "ПЕРЕСЕЧЕНИЯ ПО КАНАЛАМ (все каналы, по убыванию числа пар):",
    ])
    for channel_index, count in interval_diagnostics["channel_counts"]:
        channel_name = (
            channels[channel_index].get("title", "")
            if 0 <= channel_index < len(channels)
            else "неизвестный канал"
        )
        lines.append(f"  {count}: playlist_{channel_index + 1} | {channel_name}")

    lines.extend([
        "",
        "ПЕРЕСЕЧЕНИЯ ПО ПАРАМ ИСТОЧНИКОВ (по убыванию числа пар):",
    ])
    for (first_source, second_source), count in interval_diagnostics["source_pair_counts"]:
        lines.append(f"  {count}: {first_source} + {second_source}")

    lines.extend([
        "",
        "КЛАССИФИКАЦИЯ ПЕРЕСЕЧЕНИЙ:",
        "  По названиям:",
    ])
    for relation, label in (("exact_title", "Полностью одинаковое название"), ("normalized_title", "Совпадает после нормализации"), ("different_title", "Разные названия")):
        lines.append(f"    {label}: {interval_diagnostics['title_relation_counts'].get(relation, 0):,}")
    lines.append("  По длительности самого пересечения:")
    for label in ("≤5 мин", "6–15 мин", "16–30 мин", "31–60 мин", "61–120 мин", ">120 мин"):
        lines.append(f"    {label}: {interval_diagnostics['overlap_duration_buckets'].get(label, 0):,}")
    lines.append("  По разнице времени начала передач:")
    for label in ("≤5 мин", "6–15 мин", "16–30 мин", "31–60 мин", "61–120 мин", ">120 мин"):
        lines.append(f"    {label}: {interval_diagnostics['start_delta_buckets'].get(label, 0):,}")

    lines.extend(["", "ПЕРЕСЕЧЕНИЯ ВНУТРИ ОДНОГО ИСТОЧНИКА: ОДИН ИЛИ РАЗНЫЕ XMLTV ID:"])
    for source, counts in sorted(interval_diagnostics["same_source_xml_id_counts"].items(), key=lambda item: -(item[1]["same_xml_id"] + item[1]["different_xml_id"])):
        total = counts["same_xml_id"] + counts["different_xml_id"]
        lines.append(f"  {source}: всего {total:,}; один XMLTV ID {counts['same_xml_id']:,}; разные XMLTV ID {counts['different_xml_id']:,}")

    lines.extend(["", "ДЕТАЛИ ПО ПАРАМ ИСТОЧНИКОВ:"])
    for (first_source, second_source), stats in interval_diagnostics["source_pair_analysis"]:
        average_overlap = stats["overlap_minutes"] / stats["total"] if stats["total"] else 0
        lines.append(
            f"  {first_source} + {second_source}: всего {stats['total']:,}; "
            f"одинаковое название {stats['exact_title'] + stats['normalized_title']:,}; "
            f"разное название {stats['different_title']:,}; "
            f"среднее пересечение {average_overlap:.1f} мин; "
            f"разница стартов: {dict(stats['start_delta_minutes'])}"
        )

    lines.extend(["", "ПЕРЕСЕЧЕНИЯ ПО КОНКРЕТНЫМ XMLTV ID (все группы):"])
    for (channel_index, id_pair), stats in interval_diagnostics["xml_id_pair_analysis"]:
        channel_name = channels[channel_index].get("title", "") if 0 <= channel_index < len(channels) else "неизвестный канал"
        first_source, first_id = id_pair[0]
        second_source, second_id = id_pair[1]
        average_overlap = stats["overlap_minutes"] / stats["total"] if stats["total"] else 0
        lines.append(
            f"  {stats['total']:,}: {channel_name} | "
            f"{first_source}/{first_id} + {second_source}/{second_id} | "
            f"разные названия {stats['different_title']:,}; "
            f"одинаковые/нормализованные {stats['exact_title'] + stats['normalized_title']:,}; "
            f"средняя накладка {average_overlap:.1f} мин"
        )

    lines.extend(["", "ПРОБЛЕМНЫЕ КАНАЛЫ: ПАРЫ ИСТОЧНИКОВ (топ-100):"])
    for (channel_index, source_pair), count in interval_diagnostics["channel_source_counts"][:100]:
        channel_name = channels[channel_index].get("title", "") if 0 <= channel_index < len(channels) else "неизвестный канал"
        lines.append(f"  {count:,}: {channel_name} | {source_pair[0]} + {source_pair[1]}")

    lines.extend(["", "ПРИМЕРЫ ПЕРЕСЕКАЮЩИХСЯ ПЕРЕДАЧ (до 3 пар для каждого из 20 самых проблемных каналов):"])
    examples_by_channel = defaultdict(list)
    for overlap in interval_diagnostics["overlaps"]:
        examples_by_channel[overlap["playlist_index"]].append(overlap)
    for channel_index, count in interval_diagnostics["channel_counts"][:20]:
        channel_name = channels[channel_index].get("title", "") if 0 <= channel_index < len(channels) else "неизвестный канал"
        lines.append(f"  {channel_name} ({count:,} пар):")
        for overlap in examples_by_channel[channel_index][:3]:
            lines.append(
                f"    {overlap['first_start']}–{overlap['first_stop']} [{overlap['first_source']}/{overlap['first_xml_channel_id']}] "
                f"{overlap['first_title'][:90]} || {overlap['second_start']}–{overlap['second_stop']} "
                f"[{overlap['second_source']}/{overlap['second_xml_channel_id']}] {overlap['second_title'][:90]} "
                f"(накладка {overlap['overlap_minutes']:.1f} мин; старты отличаются на {overlap['start_delta_minutes']:.1f} мин; {overlap['title_relation']})"
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
    channel_icon_sources = {}
    exact_programmes_seen = {}
    merge_diagnostics = defaultdict(int)
    merge_collision_details = []
    merge_text_details = []
    duplicate_channel_id_details = []
    channel_mapping_details = []
    mapping_rule_details = []
    all_stats = []

    size_snapshot_path = OUTPUT_DIR / ".epg_source_size_snapshot.xml.gz"
    try:
        baseline_gzip_bytes = write_combined_xml(
            channels, {}, channel_icons, output_path=size_snapshot_path
        )["gzip_bytes"]
    finally:
        if size_snapshot_path.exists():
            size_snapshot_path.unlink()
    previous_gzip_bytes = baseline_gzip_bytes
    previously_covered_channels = set()
    claimed_channel_indexes = set()

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
            channel_icon_sources,
            exact_programmes_seen,
            merge_diagnostics,
            merge_collision_details,
            merge_text_details,
            duplicate_channel_id_details,
            channel_mapping_details,
            mapping_rule_details,
            claimed_channel_indexes,
        )

        source_matched_indexes = set(
            stats.pop("_channel_indexes_matched", [])
        )
        claimed_channel_indexes.update(source_matched_indexes)

        source_channel_indexes = set(
            stats.pop("_channel_indexes_with_programmes", [])
        )
        new_channel_indexes = source_channel_indexes - previously_covered_channels
        stats["new_channels_with_programmes"] = len(new_channel_indexes)
        previously_covered_channels.update(source_channel_indexes)

        if stats["status"] == "OK":
            try:
                current_size = write_combined_xml(
                    channels,
                    global_programmes,
                    channel_icons,
                    output_path=size_snapshot_path,
                )["gzip_bytes"]
                stats["cumulative_gzip_bytes"] = current_size
                stats["marginal_gzip_bytes"] = current_size - previous_gzip_bytes
                previous_gzip_bytes = current_size
            finally:
                if size_snapshot_path.exists():
                    size_snapshot_path.unlink()

        all_stats.append(stats)
        log(
            f"  Маржинальный вклад: новых каналов с программами "
            f"{stats['new_channels_with_programmes']}; "
            f"изменение GZIP {stats['marginal_gzip_bytes'] / 1_000_000:.2f} MB"
        )

    write_coverage_report(all_stats)
    uncovered_rows, uncovered_with_candidates = write_uncovered_channel_candidates(
        channels, global_programmes, mapping_rule_details
    )
    log(
        f"Уникальных каналов без EPG: {uncovered_rows}; "
        f"с кандидатами из источников, где есть передачи: {uncovered_with_candidates}"
    )
    write_diagnostic_csv("merge_collision_details.csv", merge_collision_details, ["source", "playlist_channel", "playlist_index", "start", "existing_xml_channel_id", "incoming_xml_channel_id", "xml_channel_id_relation", "existing_title_chars", "incoming_title_chars", "existing_desc_chars", "incoming_desc_chars"])
    detailed_size = write_diagnostic_csv_gzip(
        "merge_collision_text_details.csv.gz",
        merge_text_details,
        [
            "source", "existing_source", "playlist_channel", "playlist_index", "start",
            "existing_xml_channel_id", "incoming_xml_channel_id",
            "existing_stop", "incoming_stop",
            "existing_title", "incoming_title",
            "existing_desc", "incoming_desc",
            "existing_categories", "incoming_categories",
        ],
    )
    log(
        f"Подробные конфликтующие кандидаты одинакового старта: {len(merge_text_details)}; "
        f"файл GZIP: {detailed_size:,} байт"
    )
    write_diagnostic_csv("duplicate_xml_channel_ids.csv", duplicate_channel_id_details, ["source", "xml_channel_id", "occurrences", "display_names", "playlist_channels", "playlist_indexes"])
    write_diagnostic_csv("source_channel_mapping_collisions.csv", channel_mapping_details, ["source", "playlist_index", "playlist_channel", "xml_channel_id_count", "xml_channel_ids"])
    write_diagnostic_csv(
        "mapping_rule_candidates.csv",
        mapping_rule_details,
        [
            "source", "xml_channel_id", "xml_display_names",
            "exact_count", "exact_indexes", "exact_names",
            "country_count", "country_indexes", "country_names",
            "exact_country_indexes", "exact_country_names",
            "base_count", "base_indexes", "base_names",
            "base_only_count", "base_only_indexes", "base_only_names",
            "all_candidate_indexes", "all_candidate_names",
            "mapping_status", "mapping_playlist_indexes", "mapping_playlist_names",
            "sequence_fingerprint_status", "sequence_fingerprint_evidence",
        ],
    )
    raw_interval_diagnostics = analyze_programme_interval_overlaps(global_programmes)
    adjustment_diagnostics = {
        "adjusted_programmes": 0,
        "truncated_minutes": 0.0,
        "max_truncation_minutes": 0.0,
        "by_source": {},
    }
    final_interval_diagnostics = raw_interval_diagnostics

    logo_diagnostics, logo_report_lines = analyze_channel_logos(
        channels, channel_icons, channel_icon_sources
    )
    write_channel_report(channels, global_programmes, logo_diagnostics)
    pending_epg_path = OUTPUT_DIR / ".epg_custom.pending.xml.gz"
    xml_sizes = write_combined_xml(
        channels, global_programmes, channel_icons,
        output_path=pending_epg_path,
    )
    diagnostics = write_epg_diagnostics(
        channels,
        global_programmes,
        channel_icons,
        period_start,
        period_end,
        xml_sizes,
        exact_programmes_seen,
        merge_diagnostics,
        raw_interval_diagnostics,
        adjustment_diagnostics,
        final_interval_diagnostics,
    )
    with (OUTPUT_DIR / "epg_diagnostics.txt").open("a", encoding="utf-8") as report:
        report.write("\n".join(logo_report_lines) + "\n")

    covered_channels = {
        programme["channel_index"]
        for programme in global_programmes.values()
    }

    failed_sources = [item["source"] for item in all_stats if item["status"] != "OK"]
    if raw_interval_diagnostics["count"] != 0:
        pending_epg_path.unlink(missing_ok=True)
        log(
            "ОТКАЗ ПУБЛИКАЦИИ: осталось пересекающихся пар: "
            f"{raw_interval_diagnostics['count']}."
        )
        sys.exit(1)
    if len(covered_channels) <= 2200:
        pending_epg_path.unlink(missing_ok=True)
        log(
            "ОТКАЗ ПУБЛИКАЦИИ: EPG получили только "
            f"{len(covered_channels)} каналов; требуется минимум 2201."
        )
        sys.exit(1)
    if failed_sources:
        pending_epg_path.unlink(missing_ok=True)
        log("ОТКАЗ ПУБЛИКАЦИИ: не обработаны источники: " + "; ".join(failed_sources))
        sys.exit(1)

    validated_channels, validated_programmes = validate_generated_xml(
        pending_epg_path, len(channels), len(global_programmes)
    )
    os.replace(pending_epg_path, OUTPUT_DIR / "epg_custom.xml.gz")
    log(
        "epg_custom.xml.gz проверен: "
        f"{validated_channels} каналов, {validated_programmes} передач; пересечений 0."
    )

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
    log("  merge_collision_details.csv")
    log("  merge_collision_text_details.csv.gz")
    log("  duplicate_xml_channel_ids.csv")
    log("  source_channel_mapping_collisions.csv")
    log("  mapping_rule_candidates.csv")
    log("  uncovered_channel_candidates.csv")
    log("  coverage_report.csv")
    log("  channel_coverage.csv")
    log("  epg_diagnostics.txt")
    log("  programme_interval_overlaps.csv.gz")
    log("  epg_custom.xml.gz")

    log()
    log(f"Время выполнения: {(time.time() - started) / 60:.1f} мин.")

    if not any(item["status"] == "OK" for item in all_stats):
        log("ВНИМАНИЕ: ни один источник не обработан успешно.")
        sys.exit(1)


if __name__ == "__main__":
    main()
