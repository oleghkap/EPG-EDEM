#!/usr/bin/env python3

"""EPG-EDEM production cleaner: priority merge of EPG.ONE and IPTVX."""

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
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Production configuration
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
OUTPUT_FILE = OUTPUT_DIR / "epg_custom.xml.gz"
SOURCES = [
    ("EPG.ONE", "http://epg.one/epg2.xml.gz"),
    ("IPTVX", "https://iptvx.one/epg/epg.xml.gz"),
]
SEQUENCE_FINGERPRINT_MIN_PROGRAMMES = 3
SEQUENCE_FINGERPRINT_MIN_MATCH_RATIO = 0.60
SEQUENCE_FINGERPRINT_MAX_OTHER_RATIO = 0.20

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


def log(message=""):
    print(message, flush=True)


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
    Ключ логического канала: качество и городские пометки не разделяют
    один канал, но явная страна и временной сдвиг остаются значимыми.
    """
    raw = (value or "").strip()
    countries = edge_country_codes(raw)
    value = strip_country_suffix(strip_country_prefix(raw)).strip()
    value = re.sub(
        r"(\+\s*\d+)\s*\([^)]*\)\s*$",
        r"\1",
        value,
        flags=re.IGNORECASE,
    )
    # Географические подписи региональных версий без явного +N.
    # Удаляем только известные региональные метки из реальных имён плейлиста.
    value = re.sub(
        r"\s*\([^)]*(?:Уфа|Нефтекамск|Липецк|Невинномысск|Алания|Белгород|"
        r"Элиста|Тамбов|Владивосток|Омск|Томск|Абакан|Орион|Канск|Алтай|"
        r"Дианэт|Белорецк|Китеж)[^)]*\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s+Нижний\s+Новгород\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*\+\s*0\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    normalized = normalize_base_name(value) or normalize_name(value)
    if countries and normalized:
        normalized += " " + " ".join(sorted(countries))
    return normalized


def expand_logical_channel_variants(matches, display_names, indexes):
    """
    Добавляет SD/HD/FHD и региональные варианты только одного логического
    канала. Страна и +N сохранены в ключе, поэтому разные регионы/сдвиги
    не смешиваются.
    """
    matches = set(matches)
    source_keys = {
        canonical_mapping_name(name)
        for name in display_names
        if name and canonical_mapping_name(name)
    }
    if len(source_keys) != 1:
        return matches
    by_logical_name = indexes[3]
    return matches | set(by_logical_name.get(next(iter(source_keys)), set()))


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
        r"^(\d{14})(?:\s*([+-]\d{2}:?\d{2}))?$",
        value,
    )

    if not match:
        return None

    try:
        dt = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        offset = match.group(2)

        if offset:
            sign = 1 if offset[0] == "+" else -1
            offset_digits = offset[1:].replace(":", "")
            hours = int(offset_digits[:2])
            minutes = int(offset_digits[2:4])

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


def build_playlist_indexes(channels):
    by_id = defaultdict(set)
    by_name = defaultdict(set)
    by_base_name = defaultdict(set)
    by_logical_name = defaultdict(set)

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

            logical_name = canonical_mapping_name(name)
            if logical_name:
                by_logical_name[logical_name].add(index)

    return by_id, by_name, by_base_name, by_logical_name


def audit_mapping_candidates(display_names, indexes, playlist_channels):
    """Диагностика кандидатов без изменения действующего сопоставления."""
    _, by_name, by_base_name, _ = indexes
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

    # Явный маркер качества должен совпадать. Базовый EPG переносится
    # отдельным fallback-этапом, только если у варианта нет своего расписания.
    if source_quality != playlist_quality:
        return False

    source_countries = edge_country_codes(source_name)
    playlist_countries = edge_country_codes(playlist_name)
    if source_countries and playlist_countries and source_countries.isdisjoint(playlist_countries):
        return False

    return True


def map_source_channels(channel_id, display_names, indexes, playlist_channels):
    """
    Сопоставление по названию канала: точное, региональное,
    затем по базовому имени при совместимых маркерах качества/страны.
    tvg-id плейлиста не используется: это provider-specific ID,
    который не является надёжным ключом XMLTV.
    Ослабленные правила назначают канал только при единственном
    кандидате в плейлисте.
    """
    _, by_name, by_base_name, _ = indexes

    # XMLTV ID связывает programme с channel внутри самого XMLTV.
    # Для сопоставления с M3U используем display-name, а не tvg-id:
    # идентификаторы провайдера и EPG могут иметь разную семантику.
    exact_matches = set()
    for name in display_names:
        normalized = normalize_name(name)
        if normalized:
            exact_matches.update(by_name.get(normalized, set()))

    if exact_matches:
        return exact_matches, "exact name"

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
        # Не отбрасываем кандидатов: sequence fingerprint и проверка
        # логического имени ниже должны попытаться безопасно сузить набор.
        return country_matches, "ambiguous"

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
        # Сохраняем неоднозначные кандидаты для проверки последовательностей.
        # При конфликте они будут изолированы на этапе mapping_finalized.
        return base_matches, "ambiguous"
    if region_filtered:
        return set(), "region-ambiguous"
    return set(), "unmatched"


def local_name(tag):
    return tag.split("}", 1)[-1]


def element_text(parent, tag):
    """Текст дочернего XMLTV-элемента, в том числе с namespace."""
    for element in parent:
        if local_name(element.tag) == tag:
            return "".join(element.itertext()).strip()
    return ""


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
):
    """Первый проход: строит компактные fingerprints только для конфликтующих ID и их якорей."""
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
                    # Каналы остаются доступными для сопоставления во всех
                    # источниках: это нужно для дозаполнения отсутствующих
                    # логотипов и EPG. Блокировка применяется только к передачам.
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
    programme_fallback_blocked_indexes=None,
):
    programme_fallback_blocked_indexes = set(programme_fallback_blocked_indexes or ())
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
        "new_channels_matched": 0,
        "fallback_channels_matched": 0,
        "channels_eligible_for_programmes": 0,
        "logos_found": 0,
        "logos_added": 0,
        "new_channels_with_programmes": 0,
        "fallback_channels_with_programmes": 0,
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
    # Логотипы обрабатываем отдельно от programme mapping.
    # Для EPG.ONE и IPTVX сначала назначаем точные/регионально-нормализованные
    # совпадения, затем заполняем оставшиеся варианты качества как fallback.
    # Это не позволяет базовому каналу перехватить логотип HD/FHD/Orig,
    # который объявлен отдельным XMLTV channel.
    source_channel_logo_candidates_by_id = {}
    source_channel_icons = {}
    source_channel_icon_sources = {}
    # Разные передачи с разным временем начала сохраняются даже при
    # пересечении интервалов. Пересечения анализируются отдельно после merge.

    with tempfile.NamedTemporaryFile(delete=False) as temp:
        temp_path = temp.name

    try:
        size = download_to_file(source_url, temp_path, MAX_SOURCE_SIZE)

        sequence_mapped, sequence_fingerprints = scan_source_mapping_fingerprints(
            temp_path, indexes, playlist_channels, period_start, period_end,
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
        }
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
                        "mapping_decision_evidence": "",
                    })
                    matched_indexes, method = map_source_channels(
                        channel_id,
                        display_names,
                        indexes,
                        playlist_channels,
                    )
                    # Сопоставление выполняется во всех источниках: более поздние
                    # источники могут заполнить отсутствующий логотип или EPG.
                    # Запрет на повторные передачи применяется отдельно ниже.
                    if method == "ambiguous":
                        stats["ambiguous_channels"] += 1

                    # Логотипы собираем из всех девяти источников в порядке
                    # приоритета. Более поздние источники смогут заполнить только
                    # пустые места; уже назначенные логотипы не перезаписываются.
                    # Кандидатов фиксируем до programme ambiguity quarantine:
                    # неоднозначность расписания не должна отнимать корректный logo.
                    if channel_id:
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
                            logo_entry = source_channel_logo_candidates_by_id.setdefault(
                                channel_id,
                                {
                                    "icon": icon_src,
                                    "exact": set(),
                                    "base": set(),
                                    "display_names": set(),
                                },
                            )
                            logo_entry["display_names"].update(
                                name for name in display_names if name
                            )
                            for field, target in (
                                ("exact_country_indexes", "exact"),
                                ("base_only_indexes", "base"),
                            ):
                                for value in audit.get(field, "").split(" | "):
                                    value = value.strip()
                                    if value.isdigit():
                                        logo_entry[target].add(int(value))

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

                            # При недостаточной/конфликтующей fingerprint-проверке
                            # допускаем сужение только по точному названию из
                            # channels_test.txt. Нормализация базового имени (HD/4K)
                            # не является доказательством одинакового расписания.
                            strict_exact_indexes = {
                                playlist_index
                                for playlist_index in candidate_indexes
                                if normalize_name(
                                    playlist_channels[playlist_index].get("title", "")
                                ) in {
                                    normalize_name(name)
                                    for name in source_names
                                    if name
                                }
                            }
                            exact_name_narrowed = (
                                sequence_result["status"] in ("insufficient", "conflicting")
                                and len(strict_exact_indexes) == 1
                            )
                            if exact_name_narrowed:
                                exact_winner = next(iter(strict_exact_indexes))
                                matching_indexes = {exact_winner}
                                matched_keys_by_index = {
                                    exact_winner: matched_keys_by_index.get(exact_winner, set())
                                }
                                matched_logical_keys = set(
                                    matched_keys_by_index[exact_winner]
                                )
                                if matching_indexes != candidate_indexes:
                                    mapping_filtered_ids.add(mapped_id)

                            # Конфликт последовательностей оставляет канал только
                            # при единственном точном совпадении с эталонным именем.
                            # Все остальные shared-ID с конфликтом изолируются.
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

                            decision_reason = (
                                "sequence_conflict_and_multiple_logical_names"
                                if conflicting_matches
                                else "no_name_intersection_with_multiple_candidate_names"
                                if no_safe_match
                                else "narrowed_by_exact_reference_name"
                                if exact_name_narrowed
                                else "sequence_verified_or_name_match"
                            )
                            decision_evidence = (
                                f"reason={decision_reason}; "
                                f"source_names={sorted(source_names)!r}; "
                                f"source_keys={sorted(source_keys)!r}; "
                                f"candidate_indexes={sorted(candidate_indexes)!r}; "
                                f"candidate_keys={sorted(candidate_name_keys)!r}; "
                                f"matched_indexes={sorted(matching_indexes)!r}; "
                                f"matched_keys={sorted(matched_logical_keys)!r}; "
                                f"sequence={sequence_result.get('status', '')}; "
                                f"sequence_evidence={sequence_result.get('evidence', '')}"
                            )
                            for detail in mapping_rule_details:
                                if detail.get("source") == source_name and detail.get("xml_channel_id") == mapped_id:
                                    detail["mapping_decision_evidence"] = decision_evidence

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

                            # Передачи для канала берём только из первого источника,
                            # в котором нашлась хотя бы одна передача. Если у канала
                            # уже есть EPG из источника выше по приоритету, текущий
                            # источник может дать ему логотип, но не расписание.
                            programme_indexes = matched_indexes - programme_fallback_blocked_indexes
                            if programme_indexes:
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

                                for index in programme_indexes:
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
                                            "start_raw": start_value,
                                            "stop_raw": stop_value,
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

        # Разделяем охват каналов и назначение передач:
        # mapped содержит только подтверждённые соответствия для EPG.
        # Для включения каналов и переноса логотипов расширяем соответствие
        # на варианты того же логического канала (HD/FHD/SD/Orig).
        # canonical_mapping_name сохраняет +N и страну, поэтому разные
        # временные и региональные версии не смешиваются.
        all_matched_indexes = set()
        for channel_id, matched_set in mapped.items():
            if not matched_set:
                continue
            display_names = source_channel_display_names.get(channel_id, set())
            channel_indexes = expand_logical_channel_variants(
                matched_set, display_names, indexes
            )
            all_matched_indexes.update(channel_indexes)

        # Сначала точные совпадения текущего источника для ВСЕХ XMLTV ID.
        # Затем разрешаем fallback на варианты качества только в пустые места.
        # Источники обрабатываются по приоритету; поздние не перезаписывают ранние.
        logo_direct_indexes_by_id = {}
        for channel_id, logo_entry in source_channel_logo_candidates_by_id.items():
            direct_indexes = set(logo_entry["exact"])
            if not direct_indexes and len(logo_entry["base"]) == 1:
                direct_indexes = set(logo_entry["base"])
            logo_direct_indexes_by_id[channel_id] = direct_indexes
            for playlist_index in sorted(direct_indexes):
                if playlist_index not in source_channel_icons:
                    source_channel_icons[playlist_index] = logo_entry["icon"]
                    source_channel_icon_sources[playlist_index] = source_name

        # Fallback на другой маркер качества — только в пустое место и
        # только внутри того же базового имени/+N/региона.
        for channel_id, logo_entry in source_channel_logo_candidates_by_id.items():
            direct_indexes = logo_direct_indexes_by_id.get(channel_id, set())
            if not direct_indexes:
                continue
            expanded_indexes = expand_logical_channel_variants(
                direct_indexes,
                logo_entry["display_names"],
                indexes,
            )
            for playlist_index in sorted(expanded_indexes - direct_indexes):
                if playlist_index not in source_channel_icons:
                    source_channel_icons[playlist_index] = logo_entry["icon"]
                    source_channel_icon_sources[playlist_index] = source_name

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
        stats["_channel_indexes_matched"] = sorted(all_matched_indexes)
        stats["_channel_indexes_with_programmes"] = sorted(accepted_indexes)
        stats["logos_found"] = len(source_channel_icons)
        stats["xml_channels_with_programmes"] = len(source_channels_with_programmes)
        stats["channels_with_programmes"] = len(accepted_indexes)
        stats["unique_programmes"] = len(source_programmes)
        stats["status"] = "OK"

        # Фиксируем данные источника только после успешного полного разбора.
        # При ошибке частично прочитанный источник не должен оставлять логотипы.
        global_programmes.update(source_programmes)
        for playlist_index, icon_src in source_channel_icons.items():
            if playlist_index not in channel_icons:
                channel_icons[playlist_index] = icon_src
                channel_icon_sources[playlist_index] = source_channel_icon_sources[playlist_index]
                stats["logos_added"] += 1


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


def apply_base_schedule_fallback(channels, included_channel_indexes, global_programmes):
    """
    Копирует расписание базового канала в HD/FHD/Orig и другие варианты
    качества только тогда, когда вариант не получил собственных передач
    ни из одного источника.
    """
    programmes_by_channel = defaultdict(list)
    for programme in global_programmes.values():
        programmes_by_channel[programme["channel_index"]].append(programme)

    logical_groups = defaultdict(lambda: {"base": [], "variants": []})
    for index in sorted(included_channel_indexes):
        title = channels[index].get("title", "")
        logical_key = canonical_mapping_name(title)
        if not logical_key:
            continue
        if quality_markers(title):
            logical_groups[logical_key]["variants"].append(index)
        else:
            logical_groups[logical_key]["base"].append(index)

    inherited_channels = 0
    inherited_programmes = 0
    ambiguous_base_groups = 0

    for group in logical_groups.values():
        base_candidates = [
            index for index in group["base"]
            if programmes_by_channel.get(index)
        ]
        if len(base_candidates) != 1:
            if len(base_candidates) > 1 and group["variants"]:
                ambiguous_base_groups += 1
            continue

        base_index = base_candidates[0]
        base_programmes = programmes_by_channel[base_index]

        for variant_index in group["variants"]:
            # Не подмешиваем базовый EPG даже при частичном собственном расписании.
            if programmes_by_channel.get(variant_index):
                continue

            copied = 0
            for programme in base_programmes:
                identity = (variant_index, programme["start"].isoformat())
                if identity in global_programmes:
                    continue
                inherited = dict(programme)
                inherited["channel_index"] = variant_index
                inherited["inherited_from_base"] = base_index
                global_programmes[identity] = inherited
                programmes_by_channel[variant_index].append(inherited)
                copied += 1

            if copied:
                inherited_channels += 1
                inherited_programmes += copied

    return {
        "channels": inherited_channels,
        "programmes": inherited_programmes,
        "ambiguous_base_groups": ambiguous_base_groups,
    }


def find_m3u_title_separator(value):
    """Find the first comma outside quoted EXTINF attributes."""
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


def parse_m3u_channels(path):
    """Parse channel titles and groups from an M3U playlist (names only)."""
    raw = Path(path).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1251")

    lines = content.splitlines()
    first_content = next((line.strip() for line in lines if line.strip()), "")
    if first_content != "#EXTM3U" and not first_content.startswith("#EXTM3U "):
        raise RuntimeError("playlist is not a valid M3U file (missing #EXTM3U header)")

    channels = []
    current_group = ""
    pending_channel_index = None
    attribute_re = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTGRP:"):
            group = line.partition(":")[2].strip()
            if pending_channel_index is not None:
                # Some IPTV playlists place EXTGRP after EXTINF, before the URL.
                if not channels[pending_channel_index]["group_from_attribute"]:
                    channels[pending_channel_index]["group"] = group
                pending_channel_index = None
            current_group = group
            continue
        if line.startswith("#EXTINF:"):
            separator = find_m3u_title_separator(line)
            if separator < 0:
                raise RuntimeError(f"invalid #EXTINF record at playlist line {line_number}")

            metadata = line[len("#EXTINF:"):separator]
            title = line[separator + 1:].strip()
            if not title:
                raise RuntimeError(f"empty channel title at playlist line {line_number}")

            attributes = {key.casefold(): value.strip() for key, value in attribute_re.findall(metadata)}
            group_title = attributes.get("group-title", "")
            group = group_title or current_group
            title = title.replace("|", "｜")
            group = group.replace("|", "｜")
            channels.append({
                "title": title,
                "group": group,
                "group_from_attribute": bool(group_title),
            })
            pending_channel_index = len(channels) - 1
            continue

        if not line.startswith("#"):
            # Stream URL reached: subsequent EXTGRP markers belong to the next entry.
            pending_channel_index = None

    if not channels:
        raise RuntimeError("playlist contains no #EXTINF channel records")
    return channels


def render_reference_channels(channels):
    """Render the canonical channels_test.txt table."""
    lines = [
        "№    | CHANNEL                                 | GRP            ",
        "-----+-----------------------------------------+----------------",
    ]
    for number, channel in enumerate(channels, start=1):
        title = channel["title"].strip()
        group = channel["group"].strip() or "Без категории"
        if not title:
            raise RuntimeError(f"empty playlist channel title at record {number}")
        lines.append(f"{number:<5}| {title:<39} | {group:<14}")
    return "\n".join(lines) + "\n"


def sync_reference_channels():
    """Refresh channels_test.txt from the configured private M3U URL."""
    playlist_url = os.environ.get("PLAYLIST_URL", "").strip()
    if not playlist_url:
        raise RuntimeError("PLAYLIST_URL is not configured; refusing to use a stale channels_test.txt")

    path = OUTPUT_DIR / "channels_test.txt"
    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded = Path(temp_dir) / "playlist.m3u"
        size = download_to_file(playlist_url, downloaded, MAX_PLAYLIST_SIZE)
        channels = parse_m3u_channels(downloaded)

    updated_content = render_reference_channels(channels)
    previous_content = path.read_text(encoding="utf-8-sig") if path.exists() else None
    if previous_content == updated_content:
        log(f"Плейлист: {len(channels)} каналов; channels_test.txt уже актуален.")
        return len(channels), False

    temporary_path = path.with_name(path.name + ".pending")
    try:
        temporary_path.write_text(updated_content, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

    log(f"Плейлист: {len(channels)} каналов; channels_test.txt обновлён ({size / 1024:.1f} KiB).")
    return len(channels), True


def load_reference_channels():
    """
    Читает канонический список каналов из channels_test.txt.
    Состав, порядок, группы и повторяющиеся записи берутся из этого файла.
    """
    path = OUTPUT_DIR / "channels_test.txt"
    if not path.is_file():
        raise RuntimeError("required reference file channels_test.txt is missing")

    channels = []
    expected_number = 1
    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("---"):
                continue
            if line_number == 1 and "CHANNEL" in line.upper():
                continue
            if not re.match(r"^\s*\d+\s*\|", line):
                continue

            columns = [part.strip() for part in line.split("|")]
            if len(columns) < 3:
                raise RuntimeError(
                    f"invalid channels_test.txt row at line {line_number}"
                )
            try:
                number = int(columns[0])
            except ValueError as error:
                raise RuntimeError(
                    f"invalid channel number at channels_test.txt line {line_number}"
                ) from error

            title = columns[1]
            group = columns[2]
            if number != expected_number or not title:
                raise RuntimeError(
                    f"channels_test.txt sequence/name error at line {line_number}: "
                    f"expected channel {expected_number}, got {number}"
                )

            channels.append({
                "title": title,
                "tvg_id": "",
                "tvg_name": title,
                "tvg_logo": "",
                "group": group,
                "reference_number": number,
            })
            expected_number += 1

    if not channels:
        raise RuntimeError("channels_test.txt contains no channel records")

    log(f"Эталонных каналов из channels_test.txt: {len(channels)}")
    return channels


def write_combined_xml(
    channels,
    global_programmes,
    channel_icons,
    description_limit=None,
    output_path=None,
    included_channel_indexes=None,
):
    path = output_path or OUTPUT_FILE
    uncompressed_size = 0

    # Пишем XML потоково и параллельно считаем точный размер XML до GZIP.
    with gzip.open(path, "wb", compresslevel=6) as file:
        def write_xml(data):
            nonlocal uncompressed_size
            file.write(data)
            uncompressed_size += len(data)

        write_xml(b'<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="EPG-EDEM">')

        channel_indexes_to_write = (
            sorted(included_channel_indexes)
            if included_channel_indexes is not None
            else list(range(len(channels)))
        )
        for index in channel_indexes_to_write:
            if index < 0 or index >= len(channels):
                raise RuntimeError(f"Invalid included playlist channel index: {index}")
            channel = channels[index]
            channel_element = ET.Element(
                "channel",
                {"id": f"playlist_{index + 1}"},
            )
            ET.SubElement(channel_element, "display-name").text = channel["title"]

            icon_src = channel_icons.get(index, "")
            if icon_src:
                ET.SubElement(channel_element, "icon", {"src": icon_src})

            write_xml(ET.tostring(channel_element, encoding="utf-8"))

        included_for_validation = set(channel_indexes_to_write)
        orphan_programmes = [
            item for item in global_programmes.values()
            if item["channel_index"] not in included_for_validation
        ]
        if orphan_programmes:
            raise RuntimeError(
                f"Cannot write {len(orphan_programmes)} programmes for channels absent from the source union."
            )

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
                    "start": programme["start_raw"],
                    "channel": f"playlist_{programme['channel_index'] + 1}",
                },
            )

            if programme["stop"] is not None:
                element.set(
                    "stop",
                    programme["stop_raw"],
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


def validate_non_overlapping_programmes(programmes):
    by_channel = defaultdict(list)
    for item in programmes.values():
        start, stop = item.get("start"), item.get("stop")
        if start is not None and stop is not None:
            by_channel[item["channel_index"]].append((start, stop, item.get("title", "")))
    overlaps = 0
    for intervals in by_channel.values():
        intervals.sort(key=lambda item: (item[0], item[1]))
        previous_stop = None
        for start, stop, _ in intervals:
            if previous_stop is not None and start < previous_stop:
                overlaps += 1
            if previous_stop is None or stop > previous_stop:
                previous_stop = stop
    if overlaps:
        raise RuntimeError(f"Итоговый EPG содержит пересекающиеся интервалы: {overlaps}")
    return overlaps



class NullCollector:
    def append(self, value):
        pass

    def __iter__(self):
        return iter(())


def main():
    period_start, period_end = local_period()
    log("=" * 62)
    log("EPG-EDEM: CLEAN — объединение двух источников XMLTV")
    log("=" * 62)
    log(f"Период по Москве: {period_start:%Y-%m-%d %H:%M} — {period_end:%Y-%m-%d %H:%M}")
    log("Дней: 3 (вчера, сегодня, завтра)")

    sync_reference_channels()
    channels = load_reference_channels()
    indexes = build_playlist_indexes(channels)
    global_programmes = {}
    channel_icons = {}
    channel_icon_sources = {}
    exact_programmes_seen = {}
    merge_diagnostics = defaultdict(int)
    diagnostic_sink = NullCollector()
    included_channel_indexes = set()
    previously_matched_channels = set()
    new_channels_by_source = {}

    with tempfile.TemporaryDirectory() as temp_dir:
        for source_number, (source_name, source_url) in enumerate(SOURCES, start=1):
            blocked = {item["channel_index"] for item in global_programmes.values()}
            stats = process_source(
                source_name, source_url, channels, indexes, period_start, period_end,
                global_programmes, channel_icons, channel_icon_sources, exact_programmes_seen,
                merge_diagnostics, diagnostic_sink, diagnostic_sink,
                diagnostic_sink, diagnostic_sink, diagnostic_sink,
                blocked,
            )
            if stats.get("status") != "OK":
                if source_number == 1:
                    raise RuntimeError("Приоритетный источник EPG.ONE не обработан: " + stats.get("error", "unknown error"))
                log("ПРЕДУПРЕЖДЕНИЕ: IPTVX не обработан; сохраняются данные EPG.ONE.")
                continue
            matched_indexes = set(stats.pop("_channel_indexes_matched", []))
            stats.pop("_channel_indexes_with_programmes", None)
            new_indexes = matched_indexes - previously_matched_channels
            new_channels_by_source[source_name] = len(new_indexes)
            previously_matched_channels.update(matched_indexes)
            included_channel_indexes.update(matched_indexes)
            log(
                f"{source_name}: сопоставлено каналов {stats['matched_playlist_channels']}; "
                f"новых каналов {len(new_indexes)}; "
                f"логотипов добавлено {stats['logos_added']}; "
                f"передач добавлено {stats['unique_programmes']}; "
                f"каналов с передачами {stats['channels_with_programmes']}"
            )

        fallback = apply_base_schedule_fallback(channels, included_channel_indexes, global_programmes)
        validate_non_overlapping_programmes(global_programmes)
        pending = OUTPUT_DIR / ".epg_custom.pending.xml.gz"
        try:
            sizes = write_combined_xml(
                channels, global_programmes, channel_icons, output_path=pending,
                included_channel_indexes=included_channel_indexes,
            )
            expected_programmes = len(global_programmes)
            validated_channels, validated_programmes = validate_generated_xml(
                pending, len(included_channel_indexes), expected_programmes
            )
            os.replace(pending, OUTPUT_FILE)
        finally:
            pending.unlink(missing_ok=True)

    inherited_programmes = [item for item in global_programmes.values() if item.get("inherited_from_base") is not None]
    original_programmes = [item for item in global_programmes.values() if item.get("inherited_from_base") is None]
    programmes_by_source = Counter(item.get("source", "") for item in original_programmes)
    logos_by_source = Counter(channel_icon_sources.values())
    covered = {item["channel_index"] for item in global_programmes.values()}
    total_channels = len(included_channel_indexes)
    log("-" * 62)
    log("ИТОГ CLEAN")
    log(f"Каналов EPG.ONE: {new_channels_by_source.get('EPG.ONE', 0)}")
    log(f"Каналов, добавленных IPTVX: {new_channels_by_source.get('IPTVX', 0)}")
    log(f"Всего каналов: {total_channels}; с программами: {len(covered)}; без программ: {total_channels - len(covered)}")
    log(f"Логотипов EPG.ONE: {logos_by_source.get('EPG.ONE', 0)}; IPTVX: {logos_by_source.get('IPTVX', 0)}; всего: {len(channel_icons)}")
    log(f"Передач EPG.ONE: {programmes_by_source.get('EPG.ONE', 0)}; IPTVX: {programmes_by_source.get('IPTVX', 0)}; всего исходных: {len(original_programmes)}; унаследовано: {len(inherited_programmes)}; итог: {len(global_programmes)}")
    log(f"Наследование базового расписания: {fallback['channels']} каналов, {fallback['programmes']} передач")
    log(f"Размер XML: {sizes['xml_uncompressed_bytes'] / 1024 / 1024:.2f} MiB; GZIP: {sizes['gzip_bytes'] / 1024 / 1024:.2f} MiB")
    log(f"Проверка пройдена: {validated_channels} каналов, {validated_programmes} передач; пересечений 0.")
    log(f"Итоговый файл: {OUTPUT_FILE}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        summary = [
            "# EPG-EDEM — clean",
            "",
            f"- Период: {period_start:%Y-%m-%d %H:%M} — {period_end:%Y-%m-%d %H:%M} (Москва, UTC+3)",
            f"- Каналы: EPG.ONE {new_channels_by_source.get('EPG.ONE', 0)}; IPTVX {new_channels_by_source.get('IPTVX', 0)}; всего {total_channels}",
            f"- Каналы с программами: {len(covered)}; без программ: {total_channels - len(covered)}",
            f"- Логотипы: EPG.ONE {logos_by_source.get('EPG.ONE', 0)}; IPTVX {logos_by_source.get('IPTVX', 0)}; всего {len(channel_icons)}",
            f"- Передачи: EPG.ONE {programmes_by_source.get('EPG.ONE', 0)}; IPTVX {programmes_by_source.get('IPTVX', 0)}; унаследовано {len(inherited_programmes)}; всего {len(global_programmes)}",
            f"- Размер GZIP: {sizes['gzip_bytes'] / 1024 / 1024:.2f} MiB",
            f"- Проверка: {validated_channels} каналов, {validated_programmes} передач, пересечений 0",
            f"- Файл: `{OUTPUT_FILE}`",
            "",
        ]
        with open(summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(summary))


if __name__ == "__main__":
    main()

