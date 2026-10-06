import asyncio
import contextvars
import html
import io
import hashlib
import json
import math
import os
import re
import time
import uuid
from bisect import bisect_right
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

server = Server("crawl4ai")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

PDF_CACHE_DIR = Path.home() / ".claude" / "mcp_servers" / "pdf_cache"
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Отдельная папка от прежнего ilex_cache: там лежат тексты, полученные
# экспортом RTF через Chrome, — с текстом из API их смешивать нельзя.
ILEX_CACHE_DIR = Path.home() / ".claude" / "mcp_servers" / "ilex_api_cache"
ILEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)

ILEX_SEARCH_CACHE_DIR = (
    Path.home() / ".claude" / "mcp_servers" / "ilex_search_cache"
)
ILEX_SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Выдача меняется редко, а каждый запрос к API расходует лимит тестового
# доступа (1000 запросов), поэтому выдача живёт сутки.
ILEX_SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60

PERF_LOG_PATH = (
    Path.home() / ".claude" / "mcp_servers" / "logs" / "belarus_legal_mcp.jsonl"
)
PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

CONTEXT_PARAGRAPHS = 1
MAX_FRAGMENTS = 5
MAX_PARAGRAPH_CHARS = 2000
MAX_RESPONSE_CHARS = 12000

_PERF_CALL_ID = contextvars.ContextVar("perf_call_id", default=None)
_PERF_TOOL_NAME = contextvars.ContextVar("perf_tool_name", default=None)
_last_tool_finished_at: float | None = None
_LEGAL_RESEARCH_EVIDENCE: dict[str, dict] = {}
# _LEGAL_RESEARCH_EVIDENCE живёt весь срок MCP-процесса и не привязан к
# конкретному диалогу. Без TTL доказательства, полученные в одном (давно
# завершённом или вообще другом) разговоре, могли бы тихо засчитаться в
# validate_legal_research для не связанного с ними вопроса.
EVIDENCE_TTL_SECONDS = 30 * 60


def log_perf(event: str, **fields) -> None:
    """Пишет машинно-читаемые замеры, не загрязняя stdout протокола MCP."""
    record = {
        "timestamp": datetime.now().isoformat(),
        "event": event,
        "call_id": _PERF_CALL_ID.get(),
        "tool": _PERF_TOOL_NAME.get(),
        **fields,
    }
    try:
        with PERF_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Диагностика не должна мешать юридическому поиску.
        pass


@contextmanager
def perf_stage(stage: str):
    """Измеряет синхронный или async-этап внутри текущего вызова инструмента."""
    started_at = time.perf_counter()
    try:
        yield
    finally:
        log_perf(
            "stage",
            stage=stage,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )

try:
    import pymorphy3 as _pymorphy3
    _morph = _pymorphy3.MorphAnalyzer()
    def normalize(word: str) -> str:
        return _morph.parse(word)[0].normal_form
except ImportError:
    def normalize(word: str) -> str:
        return word.lower()


def url_to_cache_path(url: str) -> Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return PDF_CACHE_DIR / f"{key}.json"


async def get_pravo_by_last_revision(card_url: str) -> str | None:
    """Скрапит карточку документа на pravo.by и возвращает дату последней редакции."""
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=card_url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS))
        if not result.success:
            return None
        # Ищем паттерны дат в блоке «Изменения и дополнения»
        text = result.markdown or ""
        # Ищем последнюю дату в формате дд.мм.гггг
        dates = re.findall(r'\b(\d{2}\.\d{2}\.\d{4})\b', text)
        return dates[-1] if dates else None
    except Exception:
        return None


def is_pravo_by_url(url: str) -> bool:
    return "pravo.by" in url


def get_card_url_from_pdf_url(pdf_url: str) -> str | None:
    """Пытается получить URL карточки документа из URL PDF на pravo.by."""
    # pravo.by PDF URL вида: https://pravo.by/upload/docs/op/W21226212_1344459600.pdf
    # Карточка вида: https://pravo.by/document/?guid=3871&p0=W21226212
    match = re.search(r'/(W\d+)_', pdf_url)
    if match:
        doc_id = match.group(1)
        return f"https://pravo.by/document/?guid=3871&p0={doc_id}"
    return None


async def fetch_pdf_pages(url: str, referer: str, bypass_cache: bool = False) -> tuple[list[str] | str, str]:
    """
    Скачивает PDF и возвращает (список страниц | строку с ошибкой, статус кеша).
    Статус кеша: 'cached', 'downloaded', 'updated', 'refreshed', 'error'
    """
    import httpx
    from pypdf import PdfReader

    cache_path = url_to_cache_path(url)
    forced_refresh = bypass_cache
    revision_changed = False

    # Если кеш есть и не форсируем — проверяем актуальность для pravo.by
    if cache_path.exists() and not bypass_cache:
        data = json.loads(cache_path.read_text(encoding="utf-8"))

        if is_pravo_by_url(url):
            card_url = get_card_url_from_pdf_url(url)
            if card_url:
                latest_revision = await get_pravo_by_last_revision(card_url)
                cached_revision = data.get("last_revision")
                if latest_revision and latest_revision != cached_revision:
                    # Редакция изменилась — перекачиваем
                    bypass_cache = True
                    revision_changed = True
                    data["old_revision"] = cached_revision
                    data["new_revision"] = latest_revision
                else:
                    return data["pages"], "cached"
            else:
                return data["pages"], "cached"
        else:
            return data["pages"], "cached"

    # Скачиваем PDF
    headers = {**HEADERS, "Referer": referer}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url, headers=headers)

    if response.status_code != 200:
        return f"Ошибка загрузки: HTTP {response.status_code}", "error"

    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type and not url.lower().endswith(".pdf"):
        return f"Ответ не является PDF (content-type: {content_type})\n\n{response.text[:500]}", "error"

    reader = PdfReader(io.BytesIO(response.content))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())

    if not pages:
        return "PDF скачан, но текст не удалось извлечь (возможно, скан).", "error"

    # Определяем дату редакции для pravo.by
    last_revision = None
    if is_pravo_by_url(url):
        card_url = get_card_url_from_pdf_url(url)
        if card_url:
            last_revision = await get_pravo_by_last_revision(card_url)

    was_updated = cache_path.exists()
    cache_path.write_text(json.dumps({
        "url": url,
        "pages": pages,
        # Индекс хранит только границы структурных элементов. При следующем
        # запросе статьи/пункта не нужно снова прогонять весь документ через
        # регулярные выражения.
        "structure_index": build_structural_index(pages),
        "cached_at": datetime.now().isoformat(),
        "last_revision": last_revision,
    }, ensure_ascii=False), encoding="utf-8")

    if not was_updated:
        return pages, "downloaded"
    if revision_changed:
        return pages, "updated"
    return pages, "refreshed" if forced_refresh else "updated"


def tokenize(text: str) -> list[str]:
    """
    Числа (номера статей, пунктов) выделяются отдельными токенами без морфологической
    нормализации — без этого запрос вида «статья 169» терял «169» полностью, оставляя
    только общее слово «статья», которое ничего не отличает от любого другого места
    в документе.
    """
    raw_tokens = re.findall(r'[а-яёa-z]+|\d+', text.lower())
    tokens = []
    for t in raw_tokens:
        if t.isdigit():
            tokens.append(t)
        elif len(t) > 2:
            tokens.append(normalize(t))
    return tokens


def split_paragraphs(text: str) -> list[str]:
    """
    RTF→текст экспорт ilex.by (через textutil) иногда вставляет невидимые пробельные
    символы (hair space   и подобные) на пустых строках между абзацами. Из-за этого
    буквальный \n{2,} не находит границу абзаца, и целые документы схлопываются в один
    гигантский «абзац» — поиск и релевантность по нему бессмысленны. Нормализуем такие
    строки в чистые пустые перед разбиением.
    """
    normalized = re.sub(r'(?:\n[ \t ​ ]*)+\n', '\n\n', text)
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', normalized) if p.strip()]

    # Markdown крупных консолидированных текстов на pravo.by (весь кодекс на одной
    # странице) вообще не содержит пустых строк между пунктами — там, где предыдущая
    # нормализация не помогает, абзац схлопывается в весь документ целиком (мегабайты).
    # Для таких аномально длинных «абзацев» дробим дополнительно по одинарному переносу
    # строки — иначе один фрагмент результата фактически равен всему документу.
    result = []
    for p in paragraphs:
        if len(p) > MAX_PARAGRAPH_CHARS:
            result.extend(line.strip() for line in p.split("\n") if line.strip())
        else:
            result.append(p)
    return result


def search_in_pages(
    pages: list[str],
    query: str,
    context: int = CONTEXT_PARAGRAPHS,
    max_results: int = MAX_FRAGMENTS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> str:
    """
    Ищет абзацы, релевантные запросу, с IDF-взвешиванием: слова, встречающиеся
    в большинстве абзацев документа (частые, неспецифичные — «труда», «журналы»
    в кадровом НПА), получают меньший вес, чем редкие/специфичные слова.
    Без этого в больших многотемных документах общие разделы систематически
    вытесняют из топа релевантный, но менее «многословный» раздел.
    """
    keyword_set = set(tokenize(query))
    if not keyword_set:
        return "Пустой запрос."

    pages_paragraphs = []
    total_paragraphs = 0
    doc_freq = {kw: 0 for kw in keyword_set}
    for page_text in pages:
        paragraphs = split_paragraphs(page_text)
        para_tokens_list = [tokenize(p) for p in paragraphs]
        pages_paragraphs.append((paragraphs, para_tokens_list))
        total_paragraphs += len(paragraphs)
        for tokens in para_tokens_list:
            token_set = set(tokens)
            for kw in keyword_set:
                if kw in token_set:
                    doc_freq[kw] += 1

    if total_paragraphs == 0:
        return "Документ пуст."

    idf = {kw: math.log((total_paragraphs + 1) / (doc_freq[kw] + 1)) + 1 for kw in keyword_set}

    matches = []  # (score, page_num, para_index)
    for page_num, (paragraphs, para_tokens_list) in enumerate(pages_paragraphs, 1):
        for i, tokens in enumerate(para_tokens_list):
            matched = keyword_set & set(tokens)
            if not matched:
                continue
            matches.append((sum(idf[kw] for kw in matched), page_num, i))

    if not matches:
        return f"По запросу «{query}» ничего не найдено в документе."

    matches.sort(key=lambda x: -x[0])
    top = matches[:max_results]

    # Строим контекстные диапазоны и объединяем пересекающиеся/смежные в пределах страницы —
    # иначе соседние совпадения (частое дело в документах-перечнях) дублируют общий текст
    # в двух-трёх отдельных фрагментах вместо одного.
    ranges_by_page: dict[int, list[tuple[int, int, float]]] = {}
    for score, page_num, i in top:
        paragraphs = pages_paragraphs[page_num - 1][0]
        start, end = max(0, i - context), min(len(paragraphs), i + context + 1)
        ranges_by_page.setdefault(page_num, []).append((start, end, score))

    blocks = []
    for page_num, ranges in ranges_by_page.items():
        ranges.sort()
        merged: list[list[float | int]] = []
        for start, end, score in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
                merged[-1][2] = max(merged[-1][2], score)
            else:
                merged.append([start, end, score])
        paragraphs = pages_paragraphs[page_num - 1][0]
        for start, end, score in merged:
            blocks.append((score, page_num, start, "\n\n".join(paragraphs[start:end])))

    # Сначала отбираем наиболее релевантные блоки в пределах общего бюджета ответа.
    # Первый блок всегда возвращается целиком: обрезать норму посередине опаснее, чем
    # однократно превысить мягкий лимит. Остальные блоки можно запросить отдельно.
    ranked_blocks = sorted(blocks, key=lambda x: (-x[0], x[1], x[2]))
    selected = []
    selected_chars = 0
    omitted_by_budget = 0
    for block in ranked_blocks:
        block_chars = len(block[3])
        if selected and max_chars > 0 and selected_chars + block_chars > max_chars:
            omitted_by_budget += 1
            continue
        selected.append(block)
        selected_chars += block_chars

    selected.sort(key=lambda x: (x[1], x[2]))

    multi_page = len({b[1] for b in selected}) > 1
    budget_note = (
        f", пропущено по лимиту размера: {omitted_by_budget}"
        if omitted_by_budget else ""
    )
    header = (
        f"Найдено совпадений: {len(matches)}, показано {len(selected)} "
        f"релевантных фрагментов (из топ {len(top)}){budget_note}\n\n---\n\n"
    )
    if multi_page:
        parts = [
            f"**[Стр. {page_num}]**\n{fragment}"
            for _, page_num, _, fragment in selected
        ]
    else:
        parts = [fragment for _, _, _, fragment in selected]
    return header + "\n\n---\n\n".join(parts)


_DASHES = "‐‑‒–—−"
_ARTICLE_HEADING_RE = re.compile(
    rf"(?im)^[ \t]*статья[ \t]+(\d+(?:[-{_DASHES}]\d+)?)(?=[ \t]*(?:\.|$))"
)
_POINT_HEADING_RE = re.compile(
    rf"(?m)^[ \t]*(\d+(?:(?:\.|[-{_DASHES}])\d+)*)\.(?=[ \t]+)"
)
STRUCTURE_INDEX_VERSION = 2
_EXPLICIT_LOCATOR_RE = re.compile(
    rf"(?i)\b(ст(?:ать(?:я|и|ю|е|ёй|ей)|\.)|пункт(?:а|у|е|ом)?)"
    rf"\s+(\d+(?:[-{_DASHES}.]\d+)*)(?:#(\d+))?"
)


def normalize_section_id(value: str) -> str:
    normalized = value.strip()
    for dash in _DASHES:
        normalized = normalized.replace(dash, "-")
    return normalized.rstrip(".")


def parse_section_locator(locator: str) -> tuple[str, str, int | None]:
    """Возвращает тип, номер и необязательный селектор варианта ``#N``."""
    value = locator.strip()
    explicit = _EXPLICIT_LOCATOR_RE.search(value)
    if explicit:
        kind = "article" if explicit.group(1).lower().startswith("ст") else "point"
        occurrence = int(explicit.group(3)) if explicit.group(3) else None
        if occurrence == 0:
            raise ValueError(f"Номер варианта должен начинаться с 1: {locator}")
        return kind, normalize_section_id(explicit.group(2)), occurrence

    bare = re.fullmatch(
        rf"(\d+(?:[-{_DASHES}.]\d+)*)(?:#(\d+))?", value
    )
    if not bare:
        raise ValueError(f"Не удалось распознать структурный номер: {locator}")
    section_id = normalize_section_id(bare.group(1))
    occurrence = int(bare.group(2)) if bare.group(2) else None
    if occurrence == 0:
        raise ValueError(f"Номер варианта должен начинаться с 1: {locator}")
    if "." in section_id:
        return "point", section_id, occurrence
    return "auto", section_id, occurrence


def explicit_locators_from_query(query: str) -> list[str]:
    """Извлекает только явно названные статьи/пункты, не угадывая голые числа."""
    locators = []
    for match in _EXPLICIT_LOCATOR_RE.finditer(query):
        label = "статья" if match.group(1).lower().startswith("ст") else "пункт"
        selector = f"#{match.group(3)}" if match.group(3) else ""
        locator = f"{label} {normalize_section_id(match.group(2))}{selector}"
        if locator not in locators:
            locators.append(locator)
    return locators


def _ambiguous_section_options(
    kind: str,
    section_id: str,
    spans: list[tuple[int, int]],
    text: str,
) -> list[str]:
    """Формирует устойчивые селекторы и краткие подсказки для вариантов."""
    label = "статья" if kind == "article" else "пункт"
    options = []
    for occurrence, (start, end) in enumerate(spans, 1):
        first_line = next(
            (line.strip() for line in text[start:end].splitlines() if line.strip()),
            "",
        )
        if len(first_line) > 140:
            first_line = first_line[:137].rstrip() + "..."
        options.append(
            f"{label} {section_id}#{occurrence} — «{first_line}»"
        )
    return options


def _page_offsets(pages: list[str]) -> tuple[str, list[int]]:
    starts = []
    parts = []
    offset = 0
    for page in pages:
        starts.append(offset)
        parts.append(page)
        offset += len(page) + 2
    return "\n\n".join(parts), starts


def _section_spans(text: str, pattern: re.Pattern) -> list[tuple[str, int, int]]:
    matches = list(pattern.finditer(text))
    spans = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        spans.append((normalize_section_id(match.group(1)), match.start(), end))
    return spans


def _point_spans(text: str) -> list[tuple[str, int, int]]:
    """
    Пункт включает вложенные подпункты: 21.4 продолжается через 21.4.1 и
    заканчивается перед 21.5 либо 22, а не перед первым дочерним номером.
    """
    matches = list(_POINT_HEADING_RE.finditer(text))
    spans = []
    for index, match in enumerate(matches):
        section_id = normalize_section_id(match.group(1))
        depth = section_id.count(".") + 1
        end = len(text)
        for next_match in matches[index + 1:]:
            next_id = normalize_section_id(next_match.group(1))
            next_depth = next_id.count(".") + 1
            if next_depth <= depth:
                end = next_match.start()
                break
        spans.append((section_id, match.start(), end))
    return spans


def _span_index(
    spans: list[tuple[str, int, int]]
) -> dict[str, list[tuple[int, int]]]:
    index: dict[str, list[tuple[int, int]]] = {}
    for section_id, start, end in spans:
        index.setdefault(section_id, []).append((start, end))
    return index


def build_structural_index(pages: list[str]) -> dict:
    """Строит компактный, JSON-совместимый индекс статей и пунктов документа."""
    text, page_starts = _page_offsets(pages)
    return {
        "version": STRUCTURE_INDEX_VERSION,
        "page_starts": page_starts,
        "article": _span_index(_section_spans(text, _ARTICLE_HEADING_RE)),
        "point": _span_index(_point_spans(text)),
    }


def cached_structural_index(cache_path: Path, pages: list[str]) -> dict:
    """Берёт индекс из кеша, а старый кеш однократно дополняет им."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        index = data.get("structure_index")
        if index and index.get("version") == STRUCTURE_INDEX_VERSION:
            return index
        index = build_structural_index(pages)
        data["structure_index"] = index
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return index
    except (OSError, json.JSONDecodeError):
        # Невозможность обновить кеш не должна мешать чтению нормы.
        return build_structural_index(pages)


def extract_structured_sections(
    pages: list[str],
    locators: list[str],
    max_chars: int = MAX_RESPONSE_CHARS,
    structure_index: dict | None = None,
) -> str:
    """
    Извлекает статьи/пункты целиком. Лимит мягкий: первая найденная норма никогда
    не обрезается; нормы, не поместившиеся после неё, перечисляются как пропущенные.
    """
    if not locators:
        return "Не указаны номера статей или пунктов."

    text, computed_page_starts = _page_offsets(pages)
    structure_index = structure_index or build_structural_index(pages)
    # JSON превращает кортежи в списки; для алгоритма это безразлично.
    page_starts = structure_index.get("page_starts", computed_page_starts)
    span_maps = {
        "article": structure_index.get("article", {}),
        "point": structure_index.get("point", {}),
    }

    found = []
    missing = []
    ambiguous = []
    ambiguous_options = []
    invalid = []
    seen_spans = set()
    for locator in locators:
        try:
            kind, section_id, occurrence = parse_section_locator(locator)
        except ValueError:
            invalid.append(locator)
            continue

        candidates = ("article", "point") if kind == "auto" else (kind,)
        span = None
        matched_kind = None
        for candidate in candidates:
            candidate_spans = span_maps[candidate].get(section_id, [])
            if occurrence is not None:
                if occurrence > len(candidate_spans):
                    continue
                span = tuple(candidate_spans[occurrence - 1])
                matched_kind = candidate
                break
            if len(candidate_spans) > 1:
                if candidate == "article":
                    # В экспортированных документах ilex статья часто встречается
                    # сначала одной строкой в оглавлении, а затем полным текстом.
                    # Основной текст надёжно отличается самым длинным диапазоном.
                    span = max(
                        candidate_spans,
                        key=lambda candidate_span: candidate_span[1] - candidate_span[0],
                    )
                    span = tuple(span)
                    matched_kind = candidate
                    break
                ambiguous.append(locator)
                ambiguous_options.extend(
                    _ambiguous_section_options(
                        candidate, section_id, candidate_spans, text
                    )
                )
                span = None
                matched_kind = None
                break
            if candidate_spans:
                span = tuple(candidate_spans[0])
                matched_kind = candidate
                break
        if locator in ambiguous:
            continue
        if not span:
            missing.append(locator)
            continue
        if span in seen_spans:
            continue
        seen_spans.add(span)
        start, end = span
        page_num = bisect_right(page_starts, start)
        label = "Статья" if matched_kind == "article" else "Пункт"
        found.append({
            "label": label,
            "section_id": section_id,
            "occurrence": occurrence,
            "page": page_num,
            "text": text[start:end].strip(),
        })

    selected = []
    omitted = []
    selected_chars = 0
    for item in found:
        item_chars = len(item["text"])
        if selected and max_chars > 0 and selected_chars + item_chars > max_chars:
            omitted.append(f"{item['label']} {item['section_id']}")
            continue
        selected.append(item)
        selected_chars += item_chars

    details = [f"Извлечено структурных элементов: {len(selected)}."]
    if missing:
        details.append("Не найдены: " + ", ".join(missing) + ".")
    if ambiguous:
        details.append(
            "Неоднозначные номера: " + ", ".join(ambiguous) + ". "
            "Повторите запрос с одним из идентификаторов: "
            + "; ".join(ambiguous_options) + "."
        )
    if invalid:
        details.append("Не распознаны: " + ", ".join(invalid) + ".")
    if omitted:
        details.append("Не помещены в лимит: " + ", ".join(omitted) + ".")

    blocks = []
    multi_page = len({item["page"] for item in selected}) > 1
    for item in selected:
        page = f", стр. {item['page']}" if multi_page else ""
        selector = (
            f"#{item['occurrence']}" if item.get("occurrence") is not None else ""
        )
        blocks.append(
            f"**{item['label']} {item['section_id']}{selector}{page}**\n"
            f"{item['text']}"
        )
    if not blocks:
        return " ".join(details)
    return " ".join(details) + "\n\n---\n\n" + "\n\n---\n\n".join(blocks)


def search_with_structural_preference(
    pages: list[str],
    query: str,
    max_results: int = MAX_FRAGMENTS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> str:
    """
    Для явно названных статей/пунктов сначала пробует точное извлечение.
    Если формат документа не распознан, безопасно возвращается к IDF-поиску.
    """
    locators = explicit_locators_from_query(query)
    if locators:
        structured = extract_structured_sections(
            pages, locators, max_chars=max_chars
        )
        if not structured.startswith("Извлечено структурных элементов: 0."):
            return structured
    return search_in_pages(
        pages, query, max_results=max_results, max_chars=max_chars
    )


def cache_status_note(status: str) -> str:
    if status == "cached":
        return "_[из кеша, редакция актуальна]_\n\n"
    if status == "downloaded":
        return "_[скачан впервые]_\n\n"
    if status == "updated":
        return "_[⚠️ обнаружена новая редакция — кеш обновлён]_\n\n"
    if status == "refreshed":
        return "_[кеш принудительно обновлён по запросу]_\n\n"
    return ""


ILEX_WEB_BASE_URL = "https://ilex-private.ilex.by"
ILEX_API_BASE_URL = os.environ.get(
    "ILEX_API_BASE_URL", f"{ILEX_WEB_BASE_URL}/backend-client/api/v1"
).rstrip("/")
ILEX_API_TIMEOUT_SECONDS = 90
ILEX_SEARCH_MAX_QUERY_CHARS = 255
ILEX_SEARCH_MAX_RESULTS = 10
# Ключ поисковой выдачи версионируется, чтобы не подхватить из кеша результаты,
# полученные ещё скрапингом страницы через Chrome.
ILEX_SEARCH_CACHE_KEY_PREFIX = "api-v1"
# Повторная проверка через documents/history — раз в 6 часов: модель обращается
# к одному документу много раз, а тестовый доступ к API ограничен 1000 запросов.
ILEX_REVISION_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
ILEX_DOCUMENT_CACHE_VERSION = 4
# Максимальная разница дат from/to, которую принимает documents/history
# (строго меньше 7 дней), минус запас на смену суток.
ILEX_HISTORY_MAX_DAYS = 6
_ILEX_CREDENTIALS_HINT = (
    "Укажите ILEX_USERNAME и ILEX_PASSWORD в блоке \"env\" конфигурации "
    "MCP-сервера в Claude Desktop или в файле .env рядом с server.py."
)


ILEX_API_ATTEMPTS = 3
ILEX_API_RETRY_DELAY_SECONDS = 2.0
ILEX_API_MAX_RETRY_DELAY_SECONDS = 10.0
ILEX_API_RETRY_STATUSES = {429, 502, 503, 504, 509}
ILEX_API_RATE_LIMIT_STATUSES = {429, 509}
# ilex ограничивает API 15–20 запросами в минуту (дальше — HTTP 509).
# Claude Desktop запускает несколько процессов сервера, у каждого свой счётчик,
# поэтому лимит одного процесса взят с запасом.
ILEX_API_MAX_REQUESTS_PER_MINUTE = 12
ILEX_API_RATE_LIMIT_RETRY_SECONDS = 20.0


def _ilex_retry_delay(response, attempt: int) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        pass
    if response.status_code in ILEX_API_RATE_LIMIT_STATUSES:
        # Окно лимита — минута: короткая пауза только сожгла бы попытку.
        return ILEX_API_RATE_LIMIT_RETRY_SECONDS * (attempt + 1)
    delay = ILEX_API_RETRY_DELAY_SECONDS * (attempt + 1)
    return max(0.0, min(delay, ILEX_API_MAX_RETRY_DELAY_SECONDS))


class SlidingWindowRateLimiter:
    """Не пускает больше limit запросов за window секунд, заставляя ждать."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self._window:
                    self._sent.popleft()
                if len(self._sent) < self._limit:
                    self._sent.append(now)
                    return
                wait = self._window - (now - self._sent[0])
                log_perf("ilex_api_throttled", wait_ms=round(wait * 1000))
                await asyncio.sleep(wait)


class IlexApiError(Exception):
    """Ошибка API ilex с сообщением, пригодным для показа модели."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def load_dotenv_values(path: Path) -> dict[str, str]:
    """Читает простой KEY=VALUE .env без отдельной зависимости."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def ilex_credentials() -> tuple[str, str]:
    """Переменные окружения важнее .env: так конфиг Claude Desktop всегда главный."""
    file_values = load_dotenv_values(Path(__file__).with_name(".env"))
    username = os.environ.get("ILEX_USERNAME") or file_values.get("ILEX_USERNAME", "")
    password = os.environ.get("ILEX_PASSWORD") or file_values.get("ILEX_PASSWORD", "")
    if not username or not password:
        raise IlexApiError(
            "Не заданы учётные данные API ilex. " + _ILEX_CREDENTIALS_HINT
        )
    return username, password


def _ilex_error_detail(response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:300]
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "title"):
            if data.get(key):
                return str(data[key])[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def ilex_api_error(response, action: str) -> IlexApiError:
    status = response.status_code
    if status == 400:
        reason = "некорректный запрос"
    elif status == 401:
        reason = (
            "авторизация отклонена (неверные учётные данные, аккаунт "
            "заблокирован или нет активной подписки)"
        )
    elif status == 403:
        reason = (
            "нет доступа (аккаунт заблокирован, к аккаунту не подключено API "
            "или документ не входит в разделы подписки)"
        )
    elif status == 404:
        reason = "не найдено"
    elif status in {429, 509}:
        reason = "превышен лимит частоты запросов, повторите позже"
    else:
        reason = f"HTTP {status}"
    detail = _ilex_error_detail(response)
    message = f"API ilex: {action} — {reason}"
    if detail:
        message += f". Ответ сервера: {detail}"
    if status == 401:
        message += ". " + _ILEX_CREDENTIALS_HINT
    return IlexApiError(message, status)


class IlexApiClient:
    """
    Клиент официального API ilex (backend-client). Токен получается лениво при
    первом обращении и обновляется один раз при ответе 401 — срок жизни токена
    API не документирован.
    """

    def __init__(self, base_url: str = ILEX_API_BASE_URL) -> None:
        self._base_url = base_url
        self._client = None
        self._token: str | None = None
        self._auth_lock = asyncio.Lock()
        self._rate_limiter = SlidingWindowRateLimiter(ILEX_API_MAX_REQUESTS_PER_MINUTE)

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=ILEX_API_TIMEOUT_SECONDS,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def _send(self, method: str, path: str, **kwargs):
        """
        Повторяет запрос при сетевой ошибке, недоступности шлюза и ограничении
        частоты (API ilex отвечает 509 на серию быстрых запросов).
        """
        import httpx

        for attempt in range(ILEX_API_ATTEMPTS):
            last_attempt = attempt == ILEX_API_ATTEMPTS - 1
            try:
                await self._rate_limiter.acquire()
                response = await self._http().request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise IlexApiError(f"API ilex недоступно: {exc}") from exc
                await asyncio.sleep(ILEX_API_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            if response.status_code in ILEX_API_RETRY_STATUSES and not last_attempt:
                log_perf("ilex_api_retry", status_code=response.status_code)
                await asyncio.sleep(_ilex_retry_delay(response, attempt))
                continue
            return response
        raise IlexApiError("API ilex недоступно.")

    async def _authenticate(self, stale_token: str | None) -> str:
        async with self._auth_lock:
            # Пока этот вызов ждал блокировку, токен мог обновить другой вызов.
            if self._token and self._token != stale_token:
                return self._token
            username, password = ilex_credentials()
            with perf_stage("ilex_api_authenticate"):
                response = await self._send(
                    "POST",
                    "/authenticate",
                    json={"username": username, "password": password},
                )
            if response.status_code != 200:
                raise ilex_api_error(response, "получение токена")
            try:
                token = response.json().get("token")
            except (ValueError, AttributeError):
                token = None
            if not token:
                raise IlexApiError("API ilex не вернуло токен авторизации.")
            self._token = token
            return token

    async def request_json(self, method: str, path: str, action: str, **kwargs):
        token = self._token or await self._authenticate(None)
        response = await self._send(
            method, path, headers={"x-auth-token": token}, **kwargs
        )
        if response.status_code == 401:
            token = await self._authenticate(token)
            response = await self._send(
                method, path, headers={"x-auth-token": token}, **kwargs
            )
        if response.status_code != 200:
            raise ilex_api_error(response, action)
        try:
            return response.json()
        except ValueError as exc:
            raise IlexApiError(
                f"API ilex: {action} — ответ не является JSON."
            ) from exc

    async def search_documents(self, query: str, category: str | None = None) -> dict:
        payload = {"query": query}
        if category:
            payload["category"] = category
        with perf_stage("ilex_api_search"):
            return await self.request_json(
                "POST", "/search/documents", "поиск документов", json=payload
            )

    async def search_categories(self) -> list[dict]:
        data = await self.request_json(
            "GET", "/search/categories", "получение категорий поиска"
        )
        categories = data.get("categories", []) if isinstance(data, dict) else []
        return [item for item in categories if isinstance(item, dict)]

    async def get_document(self, infobank: str, doc_id: int) -> dict:
        with perf_stage("ilex_api_document"):
            return await self.request_json(
                "GET",
                f"/documents/{infobank}/{doc_id}",
                f"получение документа {infobank}/{doc_id}",
            )

    async def document_history(
        self,
        date_from: str,
        date_to: str,
        infobanks: list[str] | None = None,
        docs: list[int] | None = None,
    ) -> dict:
        params: dict = {"from": date_from, "to": date_to}
        if infobanks:
            params["infobanks"] = infobanks
        if docs:
            params["docs"] = docs
        with perf_stage("ilex_api_history"):
            return await self.request_json(
                "GET",
                "/documents/history",
                "проверка обновлений документов",
                params=params,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


ILEX_API = IlexApiClient()


def ilex_search_cache_path(query: str) -> Path:
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    key = hashlib.sha256(normalized.encode()).hexdigest()
    return ILEX_SEARCH_CACHE_DIR / f"{key}.json"


def load_ilex_search_cache(
    query: str,
    max_results: int,
    now: float | None = None,
) -> list[dict] | None:
    """Читает только свежую положительную выдачу достаточного размера."""
    cache_path = ilex_search_cache_path(query)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - data["cached_at"]
        cached_max_results = data.get("max_results", 0)
        results = data.get("results", [])
        if (
            0 <= age <= ILEX_SEARCH_CACHE_TTL_SECONDS
            and cached_max_results >= max_results
            and results
        ):
            log_perf(
                "ilex_search_cache_hit",
                age_seconds=round(age, 2),
                result_count=min(len(results), max_results),
            )
            return results[:max_results]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass

    if cache_path.exists():
        try:
            cache_path.unlink()
        except OSError:
            pass
    return None


def write_json_atomic(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass


def save_ilex_search_cache(
    query: str,
    max_results: int,
    results: list[dict],
) -> None:
    """Атомарно кеширует выдачу; сам пользовательский запрос не сохраняется."""
    if not results:
        return
    write_json_atomic(ilex_search_cache_path(query), {
        "cached_at": time.time(),
        "max_results": max_results,
        "results": results,
    })


_ILEX_LINK_RE = re.compile(r"\{[СC][СC]_([^{}]*)\}(.*?)\{[КK][СC][СC]\}", re.DOTALL)
_ILEX_LINKED_DOCUMENT_RE = re.compile(r"\[([A-Z]+)/(\d+)\]")
_ILEX_HTML_LINK_RE = re.compile(
    r"<document-link\b([^>]*)>(.*?)</document-link>", re.DOTALL | re.IGNORECASE
)
_ILEX_HTML_ADDITIONAL_LINK_RE = re.compile(
    r"<document-additional-link\b[^>]*>(?:.*?</document-additional-link>)?",
    re.DOTALL | re.IGNORECASE,
)
_ILEX_HTML_BREAK_RE = re.compile(r"<br\s*/?>|</p\s*>|</div\s*>|</li\s*>", re.IGNORECASE)
# Только известные теги: в тексте норм встречаются знаки «<» и «>» как
# математические, и общий <[^>]+> мог бы съесть часть нормы.
_ILEX_HTML_TAG_RE = re.compile(
    r"</?(?:p|br|div|span|em|strong|b|i|u|sup|sub|a|ul|ol|li|font|"
    r"document-[a-z-]+)\b[^>]*>",
    re.IGNORECASE,
)


def _ilex_link_label(label: str, infobank: str | None, doc_id: str | None) -> str:
    if infobank and doc_id:
        return f"{label} [{infobank.upper()}/{doc_id}]"
    return label


def clean_ilex_markup(text: str) -> str:
    """
    Убирает служебную разметку ilex: HTML справки (<p>, <em>, <document-link
    infobank="BELAW" doc-id="219162">Законом</document-link>) и текстовые
    ссылки вида {СС_НУЛ_Б=BELAW_Д=175777_М=100011}постановления{КСС}. Ссылки
    на другие документы сохраняются компактной меткой [BELAW/175777] — её
    понимают все ilex-инструменты как адрес документа; ссылки внутри
    документа превращаются в обычный текст.
    """
    text = text or ""

    def replace_text_link(match: re.Match) -> str:
        params, label = match.group(1), match.group(2)
        infobank = re.search(r"Б=([A-Za-z]+)", params)
        doc_id = re.search(r"Д=(\d+)", params)
        return _ilex_link_label(
            label,
            infobank.group(1) if infobank else None,
            doc_id.group(1) if doc_id else None,
        )

    def replace_html_link(match: re.Match) -> str:
        attributes, label = match.group(1), match.group(2)
        infobank = re.search(r'infobank="([A-Za-z]+)"', attributes)
        doc_id = re.search(r'doc-id="(\d+)"', attributes)
        return _ilex_link_label(
            _ILEX_HTML_TAG_RE.sub("", label),
            infobank.group(1) if infobank else None,
            doc_id.group(1) if doc_id else None,
        )

    text = _ILEX_LINK_RE.sub(replace_text_link, text)
    if _ILEX_HTML_TAG_RE.search(text):
        text = _ILEX_HTML_ADDITIONAL_LINK_RE.sub("", text)
        text = _ILEX_HTML_LINK_RE.sub(replace_html_link, text)
        text = _ILEX_HTML_BREAK_RE.sub("\n", text)
        text = html.unescape(_ILEX_HTML_TAG_RE.sub("", text))
        text = "\n".join(line.strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def ilex_linked_documents(text: str) -> list[tuple[str, int]]:
    """Возвращает документы, на которые ссылается очищенный текст, без повторов."""
    refs = []
    for match in _ILEX_LINKED_DOCUMENT_RE.finditer(text or ""):
        ref = (match.group(1), int(match.group(2)))
        if ref not in refs:
            refs.append(ref)
    return refs


def parse_ilex_document_ref(value: str) -> tuple[str, int] | None:
    """Принимает ссылку .../view-document/BELAW/13142/... или краткую BELAW/13142."""
    value = value or ""
    match = re.search(r"view-document/([A-Za-z]+)/(\d+)", value)
    if not match:
        match = re.fullmatch(r"\s*\[?([A-Za-z]+)[/:](\d+)\]?/?\s*", value)
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def ilex_document_url(infobank: str, number: int | str) -> str:
    return f"{ILEX_WEB_BASE_URL}/view-document/{infobank}/{number}/"


def canonical_ilex_document_url(url: str) -> str:
    """
    Приводит любую ссылку на документ к одному виду: API отдаёт ссылки на
    ilex.by, а пользователь и старые ответы — на ilex-private.ilex.by. Без
    этого доказательства в validate_legal_research расходились бы по хостам.
    """
    ref = parse_ilex_document_ref(url)
    if ref:
        return ilex_document_url(*ref)
    return url.split("#", 1)[0].split("?", 1)[0]


def add_unique_ilex_result(results: list[dict], result: dict, max_results: int) -> None:
    if len(results) >= max_results:
        return
    canonical = canonical_ilex_document_url(result["url"])
    if any(canonical_ilex_document_url(item["url"]) == canonical for item in results):
        return
    results.append(result)


def parse_ilex_search_results(data: dict, max_results: int = ILEX_SEARCH_MAX_RESULTS) -> list[dict]:
    """Преобразует ответ search/documents в уникальные документы."""
    results: list[dict] = []
    documents = data.get("documents", []) if isinstance(data, dict) else []
    for document in documents:
        if not isinstance(document, dict):
            continue
        infobank = str(document.get("infoBank") or "").upper()
        number = document.get("number")
        if not infobank or number in (None, ""):
            continue
        segments = document.get("segments") or []
        segment = segments[0] if segments and isinstance(segments[0], dict) else {}
        add_unique_ilex_result(results, {
            "title": re.sub(
                r"\s+", " ", clean_ilex_markup(str(document.get("title") or ""))
            ).strip(),
            "url": ilex_document_url(infobank, number),
            "infobank": infobank,
            "status": str(document.get("status") or ""),
            "segment_id": str(segment.get("id") or ""),
            "snippet": re.sub(
                r"\s+", " ", clean_ilex_markup(str(segment.get("text") or ""))
            ).strip(),
        }, max_results)
    return results


async def search_ilex(
    query: str,
    max_results: int = ILEX_SEARCH_MAX_RESULTS,
    category: str | None = None,
) -> list[dict]:
    """Ищет документы через официальное API ilex (не более 10 по релевантности)."""
    query = re.sub(r"\s+", " ", query or "").strip()
    if not query:
        raise IlexApiError("Пустой поисковый запрос.")
    if len(query) > ILEX_SEARCH_MAX_QUERY_CHARS:
        raise IlexApiError(
            f"Поисковый запрос длиннее {ILEX_SEARCH_MAX_QUERY_CHARS} символов "
            f"({len(query)}). Сформулируйте короткий предметный запрос."
        )
    max_results = max(1, min(int(max_results), ILEX_SEARCH_MAX_RESULTS))
    cache_key = f"{ILEX_SEARCH_CACHE_KEY_PREFIX}\n{category or ''}\n{query}"
    cached_results = load_ilex_search_cache(cache_key, max_results)
    if cached_results is not None:
        return cached_results

    data = await ILEX_API.search_documents(query, category)
    # Кешируется вся выдача API, чтобы запрос с большим max_results не шёл в сеть.
    results = parse_ilex_search_results(data, ILEX_SEARCH_MAX_RESULTS)
    save_ilex_search_cache(cache_key, ILEX_SEARCH_MAX_RESULTS, results)
    return results[:max_results]


def canonical_section_locator(locator: str) -> str:
    """Нормализует ссылку на норму для машинной проверки полноты."""
    kind, section_id, occurrence = parse_section_locator(locator)
    selector = f"#{occurrence}" if occurrence is not None else ""
    if kind == "article":
        return f"статья {section_id}{selector}"
    if kind == "point":
        return f"пункт {section_id}{selector}"
    return f"{section_id}{selector}"


def _research_evidence(url: str) -> dict:
    canonical = canonical_ilex_document_url(url)
    evidence = _LEGAL_RESEARCH_EVIDENCE.setdefault(canonical, {
        "url": canonical,
        "exact_sections": set(),
        "exact_section_texts": {},
        "document_searched": False,
        "full_text_loaded": False,
        "revision_checked": False,
        "related_inspected": False,
        "related_candidates": [],
        "document_title": "",
    })
    evidence["updated_at"] = time.time()
    return evidence


def _fresh_evidence(url: str) -> dict | None:
    """Возвращает evidence только если оно записано не более EVIDENCE_TTL_SECONDS назад."""
    canonical = canonical_ilex_document_url(url)
    evidence = _LEGAL_RESEARCH_EVIDENCE.get(canonical)
    if evidence is None:
        return None
    if time.time() - evidence.get("updated_at", 0) > EVIDENCE_TTL_SECONDS:
        return None
    return evidence


def record_exact_ilex_sections(
    url: str,
    locators: list[str],
    result: str,
    status: str,
) -> None:
    """Запоминает только нормы, которые действительно присутствуют в ответе."""
    evidence = _research_evidence(url)
    evidence["revision_checked"] = status in {
        "cached", "downloaded", "updated", "refreshed"
    }
    for match in re.finditer(
        r"(?ms)^\*\*(Статья|Пункт) "
        rf"(\d+(?:[-{_DASHES}.]\d+)*)"
        r"(?:#(\d+))?"
        r"(?:, стр\. \d+)?\*\*\n"
        r"(.*?)(?=^\s*---\s*$|\Z)",
        result,
    ):
        label = "статья" if match.group(1) == "Статья" else "пункт"
        section_id = normalize_section_id(match.group(2))
        selector = f"#{match.group(3)}" if match.group(3) else ""
        evidence["exact_section_texts"][
            f"{label} {section_id}{selector}"
        ] = match.group(4).strip()
    for locator in locators:
        try:
            canonical = canonical_section_locator(locator)
        except ValueError:
            continue
        kind, section_id, occurrence = parse_section_locator(locator)
        label = "Статья" if kind == "article" else "Пункт"
        selector = f"#{occurrence}" if occurrence is not None else ""
        if kind == "auto":
            pattern = (
                rf"\*\*(?:Статья|Пункт) {re.escape(section_id)}"
                rf"{re.escape(selector)}(?:,|\*)"
            )
        else:
            pattern = (
                rf"\*\*{label} {re.escape(section_id)}"
                rf"{re.escape(selector)}(?:,|\*)"
            )
        if re.search(pattern, result):
            evidence["exact_sections"].add(canonical)


_RELATION_MARKER_RE = re.compile(
    r"(?i)\b(?:протокол\s+к|о\s+внесении\s+изменений|"
    r"об\s+изменении|о\s+дополнении|дополнительное\s+соглашение)\b"
)
_RELATION_STOPWORDS = {
    "республика", "республики", "беларусь", "беларуси", "правительство",
    "правительством", "соглашение", "соглашению", "между", "отношении",
    "налогов", "налога", "доходов", "имущества", "документ", "статья",
}


def ilex_document_heading(text: str) -> str:
    """Возвращает компактный заголовок из начала экспортированного документа."""
    heading_start = re.compile(
        r"^(?:КОНСТИТУЦИЯ|КОДЕКС|НАЛОГОВЫЙ КОДЕКС|ТРУДОВОЙ КОДЕКС|"
        r"ЗАКОН|УКАЗ|ДЕКРЕТ|ПОСТАНОВЛЕНИЕ|РЕШЕНИЕ|СОГЛАШЕНИЕ|"
        r"КОНВЕНЦИЯ|ДОГОВОР|ПРОТОКОЛ)\b",
        re.IGNORECASE,
    )
    lines = []
    for raw_line in text[:5000].splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t-*")
        if not line:
            if lines:
                break
            continue
        if not lines and not heading_start.search(line):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= 500:
            break
    return " ".join(lines)[:600]


def ilex_heading_source(document: dict) -> str:
    """
    Текст для распознавания заголовка: название из карточки API идёт первым
    абзацем, поэтому ilex_document_heading берёт именно его, а не угадывает
    заголовок по первым строкам текста.
    """
    name = (document.get("properties") or {}).get("name", "")
    text = document.get("text", "")
    return f"{name}\n\n{text}" if name else text


def _distinctive_tokens(value: str) -> set[str]:
    words = {
        word.lower()
        for word in re.findall(r"[А-Яа-яЁёA-Za-z]{6,}", value)
    }
    return words - _RELATION_STOPWORDS


def related_document_score(base_text: str, candidate_text: str) -> float:
    """Оценивает, является ли кандидат протоколом/изменением базового акта."""
    candidate_head = candidate_text[:5000]
    if not _RELATION_MARKER_RE.search(candidate_head):
        return 0.0
    base_tokens = _distinctive_tokens(ilex_document_heading(base_text))
    candidate_tokens = _distinctive_tokens(candidate_head)
    if not base_tokens:
        return 0.0
    return len(base_tokens & candidate_tokens) / len(base_tokens)


def discover_related_cached_ilex_documents(
    url: str,
    text: str,
    min_score: float = 0.45,
) -> list[dict]:
    """
    Ищет связанные протоколы и изменяющие акты среди уже полученных BELAW.
    Это самообучающийся индекс: никаких списков стран или номеров документов.
    """
    current = canonical_ilex_document_url(url)
    candidates = []
    for cache_path in ILEX_CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            candidate_url = canonical_ilex_document_url(data.get("url", ""))
            candidate_text = ilex_heading_source(data)
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            continue
        if not candidate_url or candidate_url == current or "/BELAW/" not in candidate_url:
            continue
        score = related_document_score(text, candidate_text)
        if score < min_score:
            continue
        candidates.append({
            "url": candidate_url,
            "title": ilex_document_heading(candidate_text),
            "score": round(score, 3),
            "source": "локальный индекс ранее полученных BELAW",
        })
    candidates.sort(key=lambda item: (-item["score"], item["url"]))
    return candidates


def document_requires_related_review(text: str) -> bool:
    """Консервативно отмечает документы, для которых отдельные акты типичны."""
    heading = ilex_document_heading(text).lower()
    if heading.startswith("протокол"):
        return False
    return any(kind in heading for kind in (
        "соглашение", "конвенция", "договор между",
    ))


def extract_future_change_markers(
    text: str,
    current_year: int | None = None,
) -> list[str]:
    """Возвращает компактные маркеры явно будущих дат вступления изменений."""
    current_year = current_year or datetime.now().year
    markers = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(line) < 8 or not re.search(
            r"(?i)(?:вступ\w*\s+в\s+силу|ввод\w*\s+в\s+действие|"
            r"редакц\w*,?\s+действующ\w*\s+с|изменени\w*\s+с)",
            line,
        ):
            continue
        years = [int(year) for year in re.findall(r"\b(20\d{2})\b", line)]
        if years and max(years) > current_year and line not in markers:
            markers.append(line[:500])
        if len(markers) >= 10:
            break
    return markers


def ilex_document_metadata(
    text: str,
    revision: str | None = None,
    current_year: int | None = None,
    properties: dict | None = None,
) -> dict:
    """
    Карточка API (properties) точнее разбора текста; текст используется как
    запасной источник, если поля карточки пусты.
    """
    properties = properties or {}
    note = properties.get("note", "")
    heading_text = (
        f"{properties['name']}\n\n{text}" if properties.get("name") else text
    )
    entry_force = re.search(
        r"(?i)начало\s+действия\s+документа\s*[-‐‑‒–—−]\s*(\d{2}\.\d{2}\.\d{4})",
        note,
    ) or re.search(
        r"(?i)\bвступил[оа]?\s+в\s+силу\s+"
        r"(\d{1,2}\s+[а-яё]+\s+\d{4}\s+года|\d{2}\.\d{2}\.\d{4})",
        text[:8000],
    )
    return {
        "title": ilex_document_heading(heading_text) or properties.get("name", ""),
        "revision": revision or properties.get("editionDate") or None,
        "entry_into_force": entry_force.group(1) if entry_force else None,
        "requires_related_review": document_requires_related_review(heading_text),
        "future_change_markers": extract_future_change_markers(
            f"{note}\n{text}" if note else text, current_year=current_year
        ),
        "linked_documents": ilex_linked_documents(note),
    }


def related_search_query(text: str) -> str:
    """Строит короткий запрос связанных актов без знания вида документа заранее."""
    heading = ilex_document_heading(text)
    tokens = []
    for word in re.findall(r"[А-Яа-яЁёA-Za-z]{6,}", heading):
        lowered = word.lower()
        if lowered in _RELATION_STOPWORDS or lowered in tokens:
            continue
        tokens.append(lowered)
    suffix = " ".join(tokens[:8])
    return f"протокол изменения {suffix}".strip()


def validate_legal_research_state(
    requirements: list[dict],
    related_assessments: list[dict] | None = None,
    require_related_review: bool = True,
    question: str = "",
) -> dict:
    """
    Проверяет фактическое получение норм и оценку связанных документов.

    Это универсальная часть проверки — она работает для любой отрасли права.
    Ниже, по question, дополнительно включаются несколько захардкоженных
    доменных проверок (обязанность налогового агента, трансграничный доход,
    возврат/зачёт по международному договору) — они написаны под конкретные
    формулировки Налогового кодекса РБ и типового соглашения об избежании
    двойного налогообложения, встретившиеся в реальных вопросах. Для любого
    вопроса вне этих 4 паттернов (трудовое, гражданское, корпоративное право
    и т.п.) complete=True означает только «запрошенные статьи получены и
    связанные акты оценены» — без доменной проверки по существу. Это не
    расширяемый общий фреймворк; при появлении новых часто повторяющихся
    сценариев их стоит добавлять так же точечно, а не пытаться угадать
    общее правило заранее.
    """
    gaps = []
    warnings = []
    assessments = {
        canonical_ilex_document_url(item.get("url", "")): item
        for item in (related_assessments or [])
        if item.get("url")
    }
    for requirement in requirements:
        url = canonical_ilex_document_url(requirement.get("url", ""))
        evidence = _fresh_evidence(url)
        if evidence is None:
            gaps.append(f"Документ не был получен в этой MCP-сессии: {url}")
            continue
        if not evidence["revision_checked"]:
            gaps.append(f"Не проверена актуальность документа: {url}")
        for locator in requirement.get("sections", []):
            try:
                canonical = canonical_section_locator(locator)
            except ValueError:
                gaps.append(f"Не распознан номер нормы: {locator}")
                continue
            if canonical not in evidence["exact_sections"]:
                gaps.append(f"Не получен точный текст {canonical}: {url}")
        if require_related_review and not evidence["related_inspected"]:
            gaps.append(f"Не выполнена проверка связанных актов: {url}")
        for candidate in evidence.get("related_candidates", []):
            candidate_url = canonical_ilex_document_url(candidate["url"])
            candidate_evidence = _fresh_evidence(candidate_url)
            assessment = assessments.get(candidate_url)
            checked = bool(
                candidate_evidence
                and (
                    candidate_evidence["exact_sections"]
                    or candidate_evidence["document_searched"]
                    or candidate_evidence["full_text_loaded"]
                )
            )
            if not assessment:
                gaps.append(
                    "Не указана применимость связанного BELAW-документа: "
                    f"{candidate.get('title') or candidate_url} ({candidate_url})"
                )
            elif not assessment.get("reason"):
                warnings.append(
                    f"Для связанного документа не указано обоснование: {candidate_url}"
                )
            elif assessment.get("status") == "applicable" and not checked:
                gaps.append(
                    "Связанный документ признан применимым, но его нормы не получены: "
                    f"{candidate_url}"
                )

    evidence_texts = []
    titled_evidence_texts = []
    for requirement in requirements:
        url = canonical_ilex_document_url(requirement.get("url", ""))
        evidence = _fresh_evidence(url)
        if not evidence:
            continue
        for section_text in evidence["exact_section_texts"].values():
            evidence_texts.append(section_text)
            titled_evidence_texts.append(
                (evidence.get("document_title", ""), section_text)
            )
    combined_text = "\n".join(evidence_texts).lower()
    question_lower = question.lower()

    if re.search(r"удерж\w*\s+(?:подоходн\w+\s+)?налог|налог\w*\s+агент", question_lower):
        has_withholding_duty = any(
            re.search(r"налогов\w*\s+агент", text, re.IGNORECASE)
            and re.search(r"обязан\w*.*удерж|удерж\w*.*обязан", text, re.IGNORECASE | re.DOTALL)
            for text in evidence_texts
        )
        if not has_withholding_duty:
            gaps.append(
                "Для вопроса об удержании не получена норма, устанавливающая "
                "обязанность налогового агента удерживать налог."
            )

    cross_border_tax = (
        re.search(r"налог|налогооблож", question_lower)
        and re.search(r"резидент|иностран|за предел|территори\w+\s+рф|дистанцион", question_lower)
    )
    if cross_border_tax:
        has_domestic_source_rule = bool(
            re.search(r"доход\w*.*источник\w*.*республик\w+\s+беларусь", combined_text, re.DOTALL)
            and re.search(r"независимо\s+от\s+места", combined_text)
        )
        if not has_domestic_source_rule:
            gaps.append(
                "Для трансграничного дохода не получена внутренняя норма об "
                "источнике дохода и влиянии места фактической работы."
            )

    # "возврат"/"зачёт" — общеупотребимые гражданско-правовые термины (возврат
    # товара, зачёт встречных требований), не только налоговые. Без требования
    # налогового контекста в самом вопросе эти проверки ложно блокировали бы
    # ответ на любой вопрос о возврате/зачёте, не имеющий отношения к налогам.
    tax_context = re.search(r"налог|подоходн|удерж", question_lower)

    if "возврат" in question_lower and tax_context:
        if not (
            "излишне удержан" in combined_text
            and re.search(
                r"международн\w+\s+договор\w+.*иные\s+положения",
                combined_text,
                re.DOTALL,
            )
        ):
            gaps.append(
                "Для вопроса о возврате не получены одновременно нормы об "
                "излишнем удержании и возврате по международному договору."
            )

    if re.search(r"зач[её]т", question_lower) and tax_context:
        treaty_credit = any(
            re.search(r"соглашение|конвенция|протокол", title, re.IGNORECASE)
            and re.search(r"вычтен\w*\s+из\s+сумм\w+\s+налог", text, re.IGNORECASE)
            for title, text in titled_evidence_texts
        )
        if not treaty_credit:
            gaps.append(
                "Для международного зачёта не получена договорная норма о "
                "вычете налога в государстве резидентства."
            )
    return {
        "complete": not gaps,
        "gaps": gaps,
        "warnings": warnings,
    }


def url_to_ilex_cache_path(url: str) -> Path:
    ref = parse_ilex_document_ref(url)
    if ref:
        return ILEX_CACHE_DIR / f"{ref[0]}_{ref[1]}.json"
    key = hashlib.md5(url.encode()).hexdigest()
    return ILEX_CACHE_DIR / f"{key}.json"


ILEX_STATUS_LABELS = {
    "ё": "Не вступил в силу",
    "+": "Утратил силу или отменен",
    "-": "Все акты, кроме утративших силу или не вступивших в силу",
    "=": "Не применяется",
    "%": "Изменен (для суд. актов по конкретным делам)",
    "!": "Утратил значение",
}
ILEX_EDITION_LABELS = {
    "d": "Последняя редакция",
    "v": "Редакция с изменениями, не вступившими в силу",
    "n": "Другая (не последняя) редакция",
}
ILEX_INFO_TYPE_LABELS = {
    "а": "Нормативный акт",
    "a": "Нормативный акт",
    "g": "Индивидуальный правовой акт",
    "j": "Частное письмо",
    "l": "Судебное решение",
    "p": "Справочная информация",
    "r": "Проект",
    "t": "Материал консультационного характера",
    "z": "Тип не определен",
}


def ilex_property_code(value, labels: dict[str, str]) -> tuple[str, str]:
    """Разбирает поле карточки вида «d» или «d_Последняя редакция»."""
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    code, _, label = raw.partition("_")
    code = code.strip()
    return code, label.strip() or labels.get(code, code)


def _ilex_segment_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_ilex_segment_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_ilex_segment_text(item) for item in value)
    return str(value)


def _ilex_segment_items(raw) -> list[tuple[str, str]]:
    """
    Сегменты приходят списком объектов {"<id>": "<текст>"}. Допускаются и
    варианты {"id": ..., "text": ...} или один объект-словарь — формат таблиц
    в документации API не показан.
    """
    if isinstance(raw, dict):
        raw = [raw]
    items = []
    # Ключ бывает составным: «104267##104247,100001» — основная метка сегмента
    # идёт до «##», дальше исторические метки (так размечает ilex).
    for entry in raw or []:
        if isinstance(entry, dict):
            if "text" in entry and ("id" in entry or "segmentId" in entry):
                segment_id = entry.get("id", entry.get("segmentId"))
                items.append((
                    str(segment_id).split("##", 1)[0],
                    _ilex_segment_text(entry["text"]),
                ))
                continue
            for segment_id, value in entry.items():
                items.append((
                    str(segment_id).split("##", 1)[0],
                    _ilex_segment_text(value),
                ))
        elif entry is not None:
            items.append(("", _ilex_segment_text(entry)))
    return items


_ILEX_BARE_POINT_NUMBER_RE = re.compile(rf"\d+(?:(?:\.|[-{_DASHES}])\d+)*\.")


def parse_ilex_api_document(data: dict) -> tuple[str, dict, list[str]]:
    """
    Собирает текст документа из сегментов API. Абзацы разделяются пустой
    строкой — на это рассчитаны split_paragraphs и структурный индекс. Таблицы
    встраиваются по номеру сегмента, если номера числовые.
    """
    if not isinstance(data, dict):
        raise IlexApiError("API ilex вернуло документ в неожиданном формате.")
    segments = _ilex_segment_items(data.get("segments"))
    tables = _ilex_segment_items(data.get("tablesSegments"))
    items = segments + tables
    if tables and all(segment_id.isdigit() for segment_id, _ in items):
        # sorted() стабилен: при совпадении номера обычный текст идёт раньше.
        items = sorted(items, key=lambda item: int(item[0]))
    paragraphs = []
    segment_ids = []
    for segment_id, value in items:
        paragraph = clean_ilex_markup(value).strip()
        if not paragraph:
            continue
        if paragraphs and _ILEX_BARE_POINT_NUMBER_RE.fullmatch(paragraphs[-1]):
            # В таблицах номер пункта («21.4.») — отдельная ячейка, а текст
            # пункта — следующая. Без склейки структурный индекс не видит
            # заголовок пункта: после номера нет текста на той же строке.
            paragraphs[-1] = f"{paragraphs[-1]} {paragraph}"
            continue
        paragraphs.append(paragraph)
        segment_ids.append(segment_id)

    raw_properties = data.get("properties")
    if not isinstance(raw_properties, dict):
        raw_properties = {
            key: value for key, value in data.items()
            if key not in {"segments", "tablesSegments"}
        }
    properties = {}
    for key, value in raw_properties.items():
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(item) for item in value if item not in (None, ""))
        properties[key] = clean_ilex_markup("" if value is None else str(value)).strip()
    if "infobank" in properties and "infoBank" not in properties:
        properties["infoBank"] = properties.pop("infobank")
    return "\n\n".join(paragraphs), properties, segment_ids


def _parse_ilex_date(value: str):
    try:
        return datetime.strptime((value or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def ilex_document_warnings(properties: dict, today=None) -> list[str]:
    """Предупреждения о статусе, которые меняют применимость норм документа."""
    today = today or datetime.now().date()
    warnings = []
    status_code, status_label = ilex_property_code(
        properties.get("status"), ILEX_STATUS_LABELS
    )
    if status_code in {"+", "ё", "=", "!"}:
        warnings.append(f"⚠️ Статус документа: {status_label}.")
    edition_code, _ = ilex_property_code(
        properties.get("edition"), ILEX_EDITION_LABELS
    )
    if edition_code == "v":
        warnings.append(
            "⚠️ Это редакция с изменениями, которые ещё не вступили в силу. "
            "Для действующих норм нужна последняя действующая редакция."
        )
    elif edition_code == "n":
        first_edition = properties.get("firstEdition")
        hint = (
            f" (первая редакция этого документа: "
            f"{properties.get('infoBank') or 'BELAW'}/{first_edition})"
            if first_edition else ""
        )
        warnings.append(
            "⚠️ Это не последняя редакция документа" + hint
            + ". Найдите действующую редакцию через search_ilex."
        )
    terminate = _parse_ilex_date(properties.get("terminateEffectDate", ""))
    if terminate and terminate < today:
        warnings.append(
            "⚠️ Действие этой редакции окончено "
            f"{properties['terminateEffectDate']}."
        )
    elif terminate and edition_code == "d" and ilex_property_code(
        properties.get("controlType"), {}
    )[0] == "5":
        # Дата окончания сама по себе бывает технической (у международных
        # договоров встречается 31.12.2035); о будущих изменениях достоверно
        # говорит только тип контроля 5 — «создана редакция с изменениями,
        # не вступившими в силу».
        warnings.append(
            f"⚠️ Эта редакция действует по {properties['terminateEffectDate']}: "
            "после этой даты вступают в силу изменения, которые в текст не "
            "включены. Для отношений после неё нужна будущая редакция; к текущим "
            "отношениям эти изменения не применяй."
        )
    take_effect = _parse_ilex_date(properties.get("takeEffectDate", ""))
    if take_effect and take_effect > today:
        warnings.append(
            "⚠️ Эта редакция начинает действовать только с "
            f"{properties['takeEffectDate']}."
        )
    info_code, info_label = ilex_property_code(
        properties.get("infoType"), ILEX_INFO_TYPE_LABELS
    )
    if info_code and info_code not in {"a", "а"}:
        warnings.append(
            f"⚠️ Тип информации: {info_label} — не нормативный правовой акт."
        )
    return warnings


def ilex_authorities_label(value: str) -> str:
    """«Сокращение_Полное наименование» → полное наименование, без повторов."""
    names = []
    for item in (value or "").split("; "):
        short, _, full = item.partition("_")
        name = (full or short).strip()
        if name and name not in names:
            names.append(name)
    return "; ".join(names)


def ilex_document_brief(document: dict) -> str:
    """Одна строка реквизитов редакции для ответов с текстом норм."""
    properties = document.get("properties") or {}
    parts = []
    ref = parse_ilex_document_ref(document.get("url", ""))
    if ref:
        parts.append(f"{ref[0]}/{ref[1]}")
    _, edition_label = ilex_property_code(properties.get("edition"), ILEX_EDITION_LABELS)
    if edition_label:
        edition_date = properties.get("editionDate")
        parts.append(
            edition_label + (f" (изменения от {edition_date})" if edition_date else "")
        )
    if properties.get("takeEffectDate"):
        parts.append(f"редакция действует с {properties['takeEffectDate']}")
    if properties.get("terminateEffectDate"):
        parts.append(f"по {properties['terminateEffectDate']}")
    lines = [f"_[{' · '.join(parts)}]_"] if parts else []
    lines.extend(ilex_document_warnings(properties))
    if document.get("amendment_notes_missing"):
        lines.append(AMENDMENT_NOTES_WARNING)
    if parse_ilex_document_ref(document.get("url", "")):
        lines.append(EDITORIAL_NOTES_WARNING)
    return "\n".join(lines) + "\n\n" if lines else ""


# Редакционная пометка — отдельный абзац вида «(в ред. Закона … N …)»,
# «(п. 2 исключен. - …)». Оговорки «(утратил силу …)» внутри текста нормы
# пометками не считаются: по ним нельзя отличить полный текст от урезанного.
_AMENDMENT_NOTE_RE = re.compile(
    r"(?m)^[ \t]*\((?:[^()\n]{0,40}\b)?"
    r"(?:в\s+ред\.|введен\w*|исключен\w*|утратил\w*\s+силу)"
    r"[^()]{0,400}\)[ \t]*$",
    re.IGNORECASE,
)


def amendment_notes_missing(text: str, properties: dict) -> bool:
    """
    API ilex отдаёт текст части документов (ТК, НК, КоАП и др.) без
    редакционных пометок и примечаний со ссылками на связанные акты, которые
    есть в веб-версии. В полных консолидированных текстах у изменённого
    документа (заполнено «редакция от») такие пометки стоят у каждой
    изменённой нормы, поэтому их полное отсутствие означает потерю, а не
    отсутствие изменений.
    """
    return bool(properties.get("editionDate")) and not _AMENDMENT_NOTE_RE.search(text)


# Сравнение с прежним экспортом RTF показало, что API не передаёт
# редакционные примечания ilex ни в одном документе: сроки вступления в силу
# отдельных абзацев, распространение изменений на прошлые отношения, правила
# из других актов и «КонсультантПлюс: примечание» со ссылками на связанные акты.
EDITORIAL_NOTES_WARNING = (
    "ℹ️ API ilex не передаёт редакционные примечания к нормам: отдельные сроки "
    "вступления в силу абзацев и пунктов, распространение изменений на прошлые "
    "отношения, специальные правила из других актов и примечания со ссылками на "
    "связанные акты. Абзацы, ещё не вступившие в силу, могут присутствовать в "
    "тексте без пометки — сверь применяемую норму со справкой "
    "(inspect_ilex_document) и с вводящим её изменяющим актом."
)
AMENDMENT_NOTES_WARNING = (
    "⚠️ API ilex не передаёт для этого документа редакционные пометки к нормам "
    "(«в ред. …», «исключён …», «введён …», примечания со ссылками на связанные "
    "акты). Какой акт последним изменил конкретную норму и была ли исключена "
    "соседняя норма, по этому тексту не подтверждается; изменяющие акты "
    "редакции перечислены в справке (inspect_ilex_document)."
)


def _ilex_text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_ilex_document_cache(cache_path: Path) -> dict | None:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    return data


async def ilex_document_changed_since(
    infobank: str,
    doc_id: int,
    checked_at: float,
    now: float | None = None,
) -> bool:
    """
    Проверяет через documents/history, публиковался ли документ после
    последней подтверждённой проверки. Дата «с» включается, поэтому
    публикация в тот же день после проверки тоже будет замечена.
    """
    now = time.time() if now is None else now
    checked_date = datetime.fromtimestamp(checked_at or now).date()
    today = datetime.fromtimestamp(now).date()
    if (today - checked_date).days >= ILEX_HISTORY_MAX_DAYS:
        # API отклоняет интервал от 7 дней («Date difference should be less
        # than 7 days»): старый кеш проще сверить полной загрузкой.
        return True
    history = await ILEX_API.document_history(
        checked_date.isoformat(), today.isoformat(),
        infobanks=[infobank], docs=[doc_id],
    )
    published = history.get(infobank, []) if isinstance(history, dict) else []
    return str(doc_id) in {str(item) for item in published or []}


_ILEX_DOCUMENT_LOCKS: dict[str, asyncio.Lock] = {}


async def fetch_ilex_document(url: str, bypass_cache: bool = False) -> tuple[dict | str, str]:
    """
    Возвращает (документ | строка с ошибкой, статус кеша) для документа ilex.

    Документ: {"url", "text", "properties", "structure_index", ...}. Кеш
    используется, только если documents/history не сообщает о публикации
    документа после последней проверки; если проверка недоступна, документ
    перекачивается, а не берётся из кеша вслепую.
    """
    ref = parse_ilex_document_ref(url)
    if ref is None:
        return (
            "Не удалось распознать документ ilex: ожидается ссылка вида "
            ".../view-document/BELAW/<номер>/ или BELAW/<номер>."
        ), "error"
    infobank, doc_id = ref
    canonical_url = ilex_document_url(infobank, doc_id)
    cache_path = url_to_ilex_cache_path(canonical_url)

    # Параллельные вызовы по одному документу не должны качать его дважды.
    lock = _ILEX_DOCUMENT_LOCKS.setdefault(canonical_url, asyncio.Lock())
    async with lock:
        history_failed = False
        cached = None if bypass_cache else read_ilex_document_cache(cache_path)
        if cached and cached.get("version") != ILEX_DOCUMENT_CACHE_VERSION:
            cached = None
        if cached:
            checked_at = cached.get("checked_at") or 0
            if 0 <= time.time() - checked_at <= ILEX_REVISION_CHECK_INTERVAL_SECONDS:
                return cached, "cached"
            try:
                changed = await ilex_document_changed_since(
                    infobank, doc_id, checked_at
                )
            except IlexApiError as exc:
                log_perf("ilex_history_check_failed", status_code=exc.status_code)
                changed = True
                history_failed = True
            if not changed:
                cached["checked_at"] = time.time()
                write_json_atomic(cache_path, cached)
                return cached, "cached"

        # Кеш прежнего формата пересобирается, но это не новая редакция.
        had_cache = cached is not None or (bypass_cache and cache_path.exists())
        try:
            data = await ILEX_API.get_document(infobank, doc_id)
            with perf_stage("ilex_api_document_parse"):
                text, properties, segment_ids = parse_ilex_api_document(data)
        except IlexApiError as exc:
            if cached and history_failed:
                # API недоступно целиком: лучше отдать кеш с явной пометкой,
                # чем ничего. validate_legal_research не засчитает такую
                # редакцию как проверенную.
                return cached, "unverified"
            return f"Ошибка загрузки документа {infobank}/{doc_id}: {exc}", "error"
        if not text.strip():
            return "Документ получен из API ilex, но его текст пуст.", "error"

        digest = _ilex_text_digest(text)
        structure_index = build_structural_index([text])
        document = {
            "version": ILEX_DOCUMENT_CACHE_VERSION,
            "url": canonical_url,
            "text": text,
            "properties": properties,
            "segment_ids": segment_ids,
            "text_digest": digest,
            "structure_index": structure_index,
            "amendment_notes_missing": amendment_notes_missing(text, properties),
            "cached_at": datetime.now().isoformat(),
            "checked_at": time.time(),
        }
        with perf_stage("ilex_cache_write"):
            write_json_atomic(cache_path, document)

    if not had_cache:
        return document, "downloaded"
    if bypass_cache:
        return document, "refreshed"
    if cached and cached.get("text_digest") == digest:
        # История отметила публикацию (или была недоступна), но текст не изменился.
        return document, "cached"
    return document, "updated"


def ilex_cache_status_note(status: str) -> str:
    if status == "cached":
        return "_[из кеша, редакция актуальна]_\n\n"
    if status == "downloaded":
        return "_[загружено впервые]_\n\n"
    if status == "updated":
        return "_[⚠️ обнаружена новая редакция — кеш обновлён]_\n\n"
    if status == "refreshed":
        return "_[кеш принудительно обновлён по запросу]_\n\n"
    if status == "unverified":
        return (
            "_[⚠️ API ilex недоступно — показан кеш, актуальность редакции "
            "НЕ проверена; повтори запрос позже]_\n\n"
        )
    return ""


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="crawl",
            description="Скрапит веб-страницу и возвращает чистый Markdown. Работает с JS-страницами.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL страницы для скрапинга"},
                    "bypass_cache": {"type": "boolean", "default": False}
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="search_crawl",
            description=(
                "Скрапит веб-страницу и возвращает только фрагменты, релевантные поисковому запросу. "
                "Используй вместо crawl когда нужен ответ на конкретный вопрос, а не вся страница целиком — "
                "экономит контекст в 10-20 раз. Обязательно используй вместо crawl, если известно или ожидается, "
                "что страница объёмная (например, карточка pravo.by с полным текстом кодекса прямо на странице) — "
                "иначе результат может превысить лимит размера ответа инструмента."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL страницы для скрапинга"},
                    "query": {"type": "string", "description": "Поисковый запрос — что именно найти на странице"},
                    "max_results": {"type": "integer", "description": "Максимум фрагментов в ответе (по умолчанию 5)", "default": 5},
                    "max_chars": {"type": "integer", "description": "Мягкий лимит размера ответа в символах (по умолчанию 12000)", "default": 12000},
                    "bypass_cache": {"type": "boolean", "default": False}
                },
                "required": ["url", "query"]
            }
        ),
        types.Tool(
            name="download_pdf",
            description="Скачивает PDF по URL и возвращает его текстовое содержимое. Кешируется; для pravo.by автоматически проверяет актуальность редакции.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL PDF-файла"},
                    "referer": {"type": "string", "description": "Referer URL (если сайт требует)"},
                    "bypass_cache": {"type": "boolean", "description": "Принудительно перекачать, игнорируя кеш", "default": False}
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="search_ilex",
            description=(
                "Ищет документы ilex.by через официальное API по текстовому запросу. "
                "Возвращает до 10 документов по релевантности: название, ссылку, "
                "информационный банк, статус («Действующая редакция» и т.п.) и один "
                "релевантный фрагмент. Используй, когда нужно найти НПА или статью по теме, "
                "а прямой ссылки нет. Запросы формулируй короткими и по теме («исчисление "
                "среднего заработка»), а не длинными формальными реквизитами акта; лимит "
                "API — 255 символов. Фрагмент выдачи служит только для навигации и не "
                "заменяет текст нормы. "
                "После получения результатов используй get_ilex_sections, если номер нормы известен; "
                "search_ilex_document — если номер неизвестен; crawl_authenticated — только когда "
                "действительно нужен весь текст целиком."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос (например: 'статья 169 трудовой кодекс')"},
                    "max_results": {"type": "integer", "description": "Максимум результатов, не более 10 (по умолчанию 10)", "default": 10},
                    "category": {"type": "string", "description": "Необязательная категория пользователя для поиска (например, 'lawyer' или 'accountant'); без неё используется категория по умолчанию"}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="search_ilex_document",
            description=(
                "Открывает документ ilex.by по ссылке и возвращает только фрагменты, релевантные "
                "поисковому запросу. Используй вместо crawl_authenticated когда нужен ответ на "
                "конкретный вопрос по документу — экономит контекст в 10-20 раз. "
                "НЕ ИСПОЛЬЗУЙ этот инструмент, если номер статьи или пункта уже известен: в таком "
                "случае обязательно вызывай get_ilex_sections, чтобы не возвращать лишние фрагменты. "
                "Полный текст документа сервер получает через официальное API ilex и кеширует "
                "на диск; актуальность редакции проверяется автоматически через журнал "
                "публикаций API. Не устанавливай bypass_cache=true без прямой просьбы пользователя "
                "или подтверждённого повреждения кеша: это запускает повторную загрузку всего документа."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL документа ilex.by (view-document/BELAW/<номер>/) или краткая ссылка BELAW/<номер>"},
                    "query": {"type": "string", "description": "Поисковый запрос — что именно найти в документе"},
                    "max_results": {"type": "integer", "description": "Максимум фрагментов в ответе (по умолчанию 5)", "default": 5},
                    "max_chars": {"type": "integer", "description": "Мягкий лимит размера ответа в символах (по умолчанию 12000)", "default": 12000},
                    "bypass_cache": {"type": "boolean", "description": "Аварийное принудительное обновление. Использовать только по прямой просьбе пользователя или при подтверждённой ошибке кеша", "default": False}
                },
                "required": ["url", "query"]
            }
        ),
        types.Tool(
            name="get_ilex_sections",
            description=(
                "Возвращает точный полный текст указанных статей или пунктов документа ilex.by. "
                "ВСЕГДА используй вместо search_ilex_document, когда номера структурных элементов известны. "
                "Если один номер встречается несколько раз, ответ вернёт допустимые селекторы "
                "вида 'пункт 1#1' и 'пункт 1#2'; повтори запрос с нужным селектором. "
                "Можно получить несколько норм одного документа одним вызовом; реквизиты кеша и "
                "редакции выводятся один раз."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL документа ilex.by (view-document/BELAW/<номер>/) или краткая ссылка BELAW/<номер>"},
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Например: ['статья 18', 'статья 261-3', 'пункт 21.4', 'пункт 1#2']"
                    },
                    "max_chars": {"type": "integer", "description": "Мягкий лимит размера ответа в символах (по умолчанию 12000)", "default": 12000},
                    "bypass_cache": {"type": "boolean", "description": "Аварийное принудительное обновление; обычно оставляй false", "default": False}
                },
                "required": ["url", "sections"]
            }
        ),
        types.Tool(
            name="inspect_ilex_document",
            description=(
                "Проверяет карточку первичного BELAW-документа перед правовым выводом: "
                "возвращает реквизиты из карточки API ilex (статус, вид редакции, дату "
                "изменений, начало и окончание действия редакции, источник публикации, "
                "справку к документу) и универсально ищет связанные протоколы/изменяющие "
                "документы в справке, среди ранее полученных BELAW и через поиск ILEX. Используй для каждого применимого документа в сложном "
                "или многодокументном вопросе. Найденные связанные документы необходимо "
                "получить либо явно оценить через validate_legal_research."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL первичного BELAW-документа или краткая ссылка BELAW/<номер>"},
                    "search_related": {
                        "type": "boolean",
                        "description": "Искать связанные акты через ILEX, если их нет в локальном индексе",
                        "default": True,
                    },
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="validate_legal_research",
            description=(
                "Финальная машинная проверка полноты исследования. Проверяет, что в текущей "
                "MCP-сессии действительно получены точные тексты всех обязательных норм, "
                "проверена актуальность и выполнена оценка связанных BELAW-документов. "
                "Правовой вывод разрешён только при complete=true. Инструмент не проверяет "
                "правильность юридического толкования — её по-прежнему выполняет модель."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "Исходный вопрос пользователя дословно. По нему сервер "
                            "проверяет минимальные виды необходимых доказательств."
                        ),
                    },
                    "requirements": {
                        "type": "array",
                        "description": "Обязательные документы и нормы для ответа",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "sections": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["url", "sections"],
                        },
                    },
                    "related_assessments": {
                        "type": "array",
                        "description": (
                            "Явная оценка каждого найденного связанного документа: "
                            "применим или неприменим и почему"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "applicable", "not_applicable",
                                        "duplicate", "future"
                                    ],
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["url", "status", "reason"],
                        },
                        "default": [],
                    },
                    "require_related_review": {
                        "type": "boolean",
                        "default": True,
                    },
                },
                "required": ["question", "requirements"],
            },
        ),
        types.Tool(
            name="crawl_authenticated",
            description=(
                "Возвращает полный текст документа ilex.by целиком через официальное API "
                "ilex вместе с реквизитами редакции. Используй только когда точечного "
                "получения через get_ilex_sections или search_ilex_document объективно "
                "недостаточно: у кодексов полный текст очень объёмный. Другие сайты не "
                "поддерживаются — для них используй crawl."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL документа ilex.by (view-document/BELAW/<номер>/) или краткая ссылка BELAW/<номер>"}
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="search_pdf",
            description=(
                "Скачивает PDF и возвращает только фрагменты, релевантные поисковому запросу. "
                "Используй вместо download_pdf, когда нужен ответ на конкретный вопрос по документу: "
                "это экономит контекст в 10-20 раз. "
                "Если номер статьи или пункта известен, вместо этого используй get_pdf_sections. "
                "PDF кешируется; для pravo.by автоматически проверяет "
                "актуальность редакции и обновляет кеш если появилась новая версия."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL PDF-файла"},
                    "query": {"type": "string", "description": "Поисковый запрос — что именно найти в документе"},
                    "referer": {"type": "string", "description": "Referer URL (если сайт требует)"},
                    "max_results": {"type": "integer", "description": "Максимум фрагментов в ответе (по умолчанию 5)", "default": 5},
                    "max_chars": {"type": "integer", "description": "Мягкий лимит размера ответа в символах (по умолчанию 12000)", "default": 12000},
                    "bypass_cache": {"type": "boolean", "description": "Аварийное принудительное обновление; не использовать без необходимости", "default": False}
                },
                "required": ["url", "query"]
            }
        ),
        types.Tool(
            name="get_pdf_sections",
            description=(
                "Возвращает точный полный текст указанных статей или пунктов PDF. "
                "Используй вместо тематического поиска, когда номера структурных элементов известны."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL PDF-файла"},
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Например: ['статья 18', 'пункт 21.4']"
                    },
                    "referer": {"type": "string", "description": "Referer URL (если сайт требует)"},
                    "max_chars": {"type": "integer", "description": "Мягкий лимит размера ответа в символах (по умолчанию 12000)", "default": 12000},
                    "bypass_cache": {"type": "boolean", "default": False}
                },
                "required": ["url", "sections"]
            }
        )
    ]


async def dispatch_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "crawl":
        return await do_crawl(arguments)
    elif name == "search_crawl":
        return await do_search_crawl(arguments)
    elif name == "search_ilex":
        return await do_search_ilex(arguments)
    elif name == "search_ilex_document":
        return await do_search_ilex_document(arguments)
    elif name == "get_ilex_sections":
        return await do_get_ilex_sections(arguments)
    elif name == "inspect_ilex_document":
        return await do_inspect_ilex_document(arguments)
    elif name == "validate_legal_research":
        return await do_validate_legal_research(arguments)
    elif name == "crawl_authenticated":
        return await do_crawl_authenticated(arguments)
    elif name == "download_pdf":
        return await do_download_pdf(arguments)
    elif name == "search_pdf":
        return await do_search_pdf(arguments)
    elif name == "get_pdf_sections":
        return await do_get_pdf_sections(arguments)
    raise ValueError(f"Unknown tool: {name}")


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    global _last_tool_finished_at

    call_id = uuid.uuid4().hex[:12]
    call_token = _PERF_CALL_ID.set(call_id)
    tool_token = _PERF_TOOL_NAME.set(name)
    started_at = time.perf_counter()
    since_previous_ms = (
        round((started_at - _last_tool_finished_at) * 1000, 2)
        if _last_tool_finished_at is not None else None
    )
    log_perf(
        "tool_start",
        since_previous_tool_ms=since_previous_ms,
        bypass_cache=bool(arguments.get("bypass_cache", False)),
    )
    status = "ok"
    response_chars = 0
    try:
        result = await dispatch_tool(name, arguments)
        response_chars = sum(
            len(item.text) for item in result if isinstance(item, types.TextContent)
        )
        return result
    except Exception:
        status = "error"
        raise
    finally:
        finished_at = time.perf_counter()
        log_perf(
            "tool_finish",
            status=status,
            duration_ms=round((finished_at - started_at) * 1000, 2),
            response_chars=response_chars,
        )
        _last_tool_finished_at = finished_at
        _PERF_CALL_ID.reset(call_token)
        _PERF_TOOL_NAME.reset(tool_token)


async def do_crawl(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    bypass_cache = arguments.get("bypass_cache", False)
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS if bypass_cache else CacheMode.ENABLED
    )
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url, config=config)
    if not result.success:
        return [types.TextContent(type="text", text=f"Ошибка: {result.error_message}")]
    return [types.TextContent(type="text", text=result.markdown or "(пустая страница)")]


async def do_search_crawl(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    query = arguments["query"]
    max_results = arguments.get("max_results", MAX_FRAGMENTS)
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS if bypass_cache else CacheMode.ENABLED
    )
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url, config=config)
    if not result.success:
        return [types.TextContent(type="text", text=f"Ошибка: {result.error_message}")]
    pages = [result.markdown or ""]
    text = search_with_structural_preference(
        pages, query, max_results=max_results, max_chars=max_chars
    )
    return [types.TextContent(type="text", text=text)]


async def do_search_ilex(arguments: dict) -> list[types.TextContent]:
    query = arguments["query"]
    max_results = arguments.get("max_results", ILEX_SEARCH_MAX_RESULTS)
    category = arguments.get("category") or None
    try:
        with perf_stage("ilex_search_total"):
            results = await search_ilex(query, max_results, category)
    except IlexApiError as e:
        message = f"Ошибка поиска: {e}"
        if category and e.status_code == 400:
            try:
                categories = await ILEX_API.search_categories()
                message += "\nДоступные категории: " + ", ".join(
                    f"{item.get('code')} ({item.get('name')})" for item in categories
                )
            except IlexApiError:
                pass
        return [types.TextContent(type="text", text=message)]
    if not results:
        return [types.TextContent(type="text", text=f"По запросу «{query}» ничего не найдено на ilex.by")]
    lines = [f"Найдено документов: {len(results)}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"**{i}. {r['title']}**")
        lines.append(f"   {r['url']}")
        details = [f"ИБ: {r['infobank']}"] if r.get("infobank") else []
        if r.get("status"):
            details.append(f"Статус: {r['status']}")
        if details:
            lines.append("   " + " · ".join(details))
        if r.get("snippet"):
            segment = f"сегмент {r['segment_id']}" if r.get("segment_id") else "фрагмент"
            lines.append(f"   Релевантный {segment} (только для навигации): {r['snippet']}")
        lines.append("")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _ilex_revision_checked(status: str) -> bool:
    return status in {"cached", "downloaded", "updated", "refreshed"}


async def do_search_ilex_document(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    query = arguments["query"]
    max_results = arguments.get("max_results", MAX_FRAGMENTS)
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    document, status = await fetch_ilex_document(url, bypass_cache)
    if isinstance(document, str):
        return [types.TextContent(type="text", text=document)]
    note = ilex_cache_status_note(status) + ilex_document_brief(document)
    with perf_stage("document_search"):
        result = search_with_structural_preference(
            [document["text"]], query, max_results=max_results, max_chars=max_chars
        )
    evidence = _research_evidence(url)
    evidence["document_searched"] = True
    evidence["revision_checked"] = _ilex_revision_checked(status)
    locators = explicit_locators_from_query(query)
    if locators:
        record_exact_ilex_sections(url, locators, result, status)
    return [types.TextContent(type="text", text=note + result)]


async def do_get_ilex_sections(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    sections = arguments["sections"]
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    document, status = await fetch_ilex_document(url, bypass_cache)
    if isinstance(document, str):
        return [types.TextContent(type="text", text=document)]
    note = ilex_cache_status_note(status) + ilex_document_brief(document)
    pages = [document["text"]]
    with perf_stage("section_index_read_and_extract"):
        index = document.get("structure_index")
        if not index or index.get("version") != STRUCTURE_INDEX_VERSION:
            index = cached_structural_index(url_to_ilex_cache_path(url), pages)
        result = extract_structured_sections(
            pages, sections, max_chars=max_chars, structure_index=index
        )
    record_exact_ilex_sections(url, sections, result, status)
    return [types.TextContent(type="text", text=note + result)]


ILEX_NOTE_HEAD_CHARS = 1500
ILEX_NOTE_TAIL_CHARS = 2500


def compact_ilex_note(note: str) -> str:
    """
    Справка к кодексу перечисляет все изменяющие акты и бывает очень длинной.
    Последний изменяющий акт обычно в конце, поэтому сохраняется и начало, и конец.
    """
    note = note.strip()
    limit = ILEX_NOTE_HEAD_CHARS + ILEX_NOTE_TAIL_CHARS
    if len(note) <= limit:
        return note
    omitted = len(note) - limit
    return (
        note[:ILEX_NOTE_HEAD_CHARS].rstrip()
        + f"\n[… пропущено {omitted} символов справки …]\n"
        + note[-ILEX_NOTE_TAIL_CHARS:].lstrip()
    )


def _ilex_note_context(note: str, ref: tuple[str, int]) -> str:
    marker = f"[{ref[0]}/{ref[1]}]"
    for line in note.splitlines():
        if marker in line:
            line = re.sub(r"\s+", " ", line).strip()
            return line[:300] + ("…" if len(line) > 300 else "")
    return marker


async def do_inspect_ilex_document(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    search_related = arguments.get("search_related", True)
    canonical_url = canonical_ilex_document_url(url)
    existing_evidence = _fresh_evidence(canonical_url)
    document = None
    status = "cached"
    if existing_evidence and existing_evidence["revision_checked"]:
        # Редакция уже проверена в этом исследовании — повторная сверка не нужна.
        document = read_ilex_document_cache(url_to_ilex_cache_path(url))
    if document is None:
        document, status = await fetch_ilex_document(url, False)
    if isinstance(document, str):
        return [types.TextContent(type="text", text=document)]

    text = document["text"]
    properties = document.get("properties") or {}
    metadata = ilex_document_metadata(text, properties=properties)
    heading_text = ilex_heading_source(document)
    current_ref = parse_ilex_document_ref(canonical_url)

    candidates = []
    linked_documents = [
        ref for ref in metadata["linked_documents"] if ref != current_ref
    ]
    if metadata["requires_related_review"]:
        # Справка к международному договору ссылается на протоколы и
        # изменяющие акты напрямую — это точнее текстового сходства.
        for ref in linked_documents:
            if ref[0] != "BELAW":
                continue
            candidates.append({
                "url": ilex_document_url(*ref),
                "title": _ilex_note_context(properties.get("note", ""), ref),
                "score": 1.0,
                "source": "ссылка в справке к документу",
            })
        candidates.extend(discover_related_cached_ilex_documents(url, heading_text))
    search_error = None
    live_search_performed = False
    if search_related and metadata["requires_related_review"] and not candidates:
        live_search_performed = True
        try:
            results = await search_ilex(related_search_query(heading_text), 10)
            for result in results:
                candidate_url = canonical_ilex_document_url(result.get("url", ""))
                candidate_title = result.get("title", "")
                if (
                    "/BELAW/" not in candidate_url
                    or candidate_url == canonical_url
                    or not _RELATION_MARKER_RE.search(candidate_title)
                ):
                    continue
                score = related_document_score(heading_text, candidate_title)
                if score < 0.35:
                    continue
                candidates.append({
                    "url": candidate_url,
                    "title": candidate_title,
                    "score": round(score, 3),
                    "source": "поиск связанных первичных документов ILEX",
                })
        except IlexApiError as exc:
            search_error = str(exc)

    unique_candidates = {}
    for candidate in candidates:
        unique_candidates.setdefault(
            canonical_ilex_document_url(candidate["url"]), candidate
        )
    candidates = list(unique_candidates.values())

    evidence = _research_evidence(url)
    evidence["revision_checked"] = _ilex_revision_checked(status)
    evidence["related_inspected"] = bool(
        not metadata["requires_related_review"]
        or candidates
        or (live_search_performed and not search_error)
    )
    evidence["related_candidates"] = candidates
    evidence["document_title"] = metadata["title"]

    _, status_label = ilex_property_code(properties.get("status"), ILEX_STATUS_LABELS)
    _, edition_label = ilex_property_code(properties.get("edition"), ILEX_EDITION_LABELS)
    _, info_type_label = ilex_property_code(properties.get("infoType"), ILEX_INFO_TYPE_LABELS)
    lines = [
        ilex_cache_status_note(status).strip(),
        f"Документ: {metadata['title'] or '(заголовок не распознан)'}",
        f"URL: {canonical_url}",
    ]
    card_fields = [
        ("Вид документа", properties.get("kinds")),
        ("Тип информации", info_type_label),
        ("Принявший орган", ilex_authorities_label(properties.get("acceptedAuthorities", ""))),
        ("Дата принятия", properties.get("dates")),
        ("Статус", status_label),
        ("Редакция", edition_label),
        ("Дата последних изменений (редакция от)", metadata["revision"]),
        ("Начало действия редакции", properties.get("takeEffectDate")),
        ("Окончание действия редакции", properties.get("terminateEffectDate")),
        ("Вступление документа в силу", metadata["entry_into_force"]),
        ("Источник публикации", properties.get("sourcePublication")),
    ]
    lines.extend(f"{label}: {value}" for label, value in card_fields if value)
    warnings = ilex_document_warnings(properties)
    if document.get("amendment_notes_missing"):
        warnings.append(AMENDMENT_NOTES_WARNING)
    warnings.append(EDITORIAL_NOTES_WARNING)
    if warnings:
        lines.append("")
        lines.extend(warnings)
    if properties.get("note"):
        lines.append("\nСправка к документу:")
        lines.append(compact_ilex_note(properties["note"]))
    if metadata["future_change_markers"]:
        lines.append("\nОбнаружены маркеры будущих изменений; проверь их применимость:")
        lines.extend(f"- {marker}" for marker in metadata["future_change_markers"])
    else:
        lines.append("\nЯвные маркеры будущих изменений в тексте не обнаружены.")
    if candidates:
        lines.append("\nСвязанные первичные BELAW-документы, требующие оценки:")
        for candidate in candidates:
            lines.append(
                f"- {candidate['title']} — {candidate['url']} "
                f"(источник: {candidate['source']})"
            )
    elif search_error:
        lines.append(
            "\n⚠️ Проверка связанных документов не завершена: "
            f"{search_error}. До правового вывода повтори проверку."
        )
    elif metadata["requires_related_review"] and not search_related:
        lines.append(
            "\n⚠️ Проверка связанных документов ограничена справкой и локальным "
            "индексом и не завершена. Повтори вызов с search_related=true."
        )
    elif metadata["requires_related_review"]:
        source = "справка, локальный индекс и поиск ILEX" if live_search_performed else "справка и локальный индекс"
        lines.append(f"\nСвязанные документы не обнаружены ({source}).")
    else:
        lines.append("\nОтдельная проверка связанных актов для этого вида документа не требуется.")
        if linked_documents:
            lines.append(
                "Документы, упомянутые в справке: "
                + ", ".join(ilex_document_url(*ref) for ref in linked_documents[:20])
            )
    return [types.TextContent(type="text", text="\n".join(line for line in lines if line))]


async def do_validate_legal_research(arguments: dict) -> list[types.TextContent]:
    result = validate_legal_research_state(
        arguments["requirements"],
        arguments.get("related_assessments", []),
        arguments.get("require_related_review", True),
        arguments.get("question", ""),
    )
    if result["complete"]:
        lines = [
            "complete=true",
            "Все заявленные нормы фактически получены; актуальность и связанные акты проверены.",
        ]
    else:
        lines = [
            "complete=false",
            "Правовой вывод пока запрещён. Не устранены пробелы:",
            *[f"- {gap}" for gap in result["gaps"]],
        ]
    if result["warnings"]:
        lines.extend(["Предупреждения:", *[f"- {item}" for item in result["warnings"]]])
    return [types.TextContent(type="text", text="\n".join(lines))]


async def do_crawl_authenticated(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    if parse_ilex_document_ref(url) is None:
        return [types.TextContent(type="text", text=(
            "crawl_authenticated работает только с документами ilex.by "
            "(view-document/<ИБ>/<номер>/ или <ИБ>/<номер>). Для других сайтов "
            "используй crawl."
        ))]
    document, status = await fetch_ilex_document(url)
    if isinstance(document, str):
        return [types.TextContent(type="text", text=document)]
    evidence = _research_evidence(url)
    evidence["full_text_loaded"] = True
    evidence["revision_checked"] = _ilex_revision_checked(status)
    note = ilex_cache_status_note(status) + ilex_document_brief(document)
    return [types.TextContent(type="text", text=note + document["text"])]


async def do_download_pdf(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    referer = arguments.get("referer", url)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_pdf_pages(url, referer, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = cache_status_note(status)
    text = note + "\n\n".join(f"### Страница {i}\n\n{p}" for i, p in enumerate(pages, 1))
    return [types.TextContent(type="text", text=text)]


async def do_search_pdf(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    query = arguments["query"]
    referer = arguments.get("referer", url)
    max_results = arguments.get("max_results", MAX_FRAGMENTS)
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_pdf_pages(url, referer, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = cache_status_note(status)
    result = search_with_structural_preference(
        pages, query, max_results=max_results, max_chars=max_chars
    )
    return [types.TextContent(type="text", text=note + result)]


async def do_get_pdf_sections(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    sections = arguments["sections"]
    referer = arguments.get("referer", url)
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_pdf_pages(url, referer, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = cache_status_note(status)
    index = cached_structural_index(url_to_cache_path(url), pages)
    result = extract_structured_sections(
        pages, sections, max_chars=max_chars, structure_index=index
    )
    return [types.TextContent(type="text", text=note + result)]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await ILEX_API.close()

if __name__ == "__main__":
    asyncio.run(main())
