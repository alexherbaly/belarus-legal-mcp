import asyncio
import contextvars
import io
import hashlib
import json
import math
import re
import time
import uuid
from bisect import bisect_right
from contextlib import asynccontextmanager, contextmanager
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

ILEX_CACHE_DIR = Path.home() / ".claude" / "mcp_servers" / "ilex_cache"
ILEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)

ILEX_SEARCH_CACHE_DIR = (
    Path.home() / ".claude" / "mcp_servers" / "ilex_search_cache"
)
ILEX_SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
ILEX_SEARCH_CACHE_TTL_SECONDS = 60 * 60

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
    r"(?m)^[ \t]*(\d+(?:\.\d+)*)\.(?=[ \t]+)"
)
_EXPLICIT_LOCATOR_RE = re.compile(
    rf"(?i)\b(ст(?:ать(?:я|и|ю|е|ёй|ей)|\.)|пункт(?:а|у|е|ом)?)"
    rf"\s+(\d+(?:[-{_DASHES}.]\d+)*)"
)


def normalize_section_id(value: str) -> str:
    normalized = value.strip()
    for dash in _DASHES:
        normalized = normalized.replace(dash, "-")
    return normalized.rstrip(".")


def parse_section_locator(locator: str) -> tuple[str, str]:
    """Возвращает (тип, номер): article, point либо auto для голого номера."""
    value = locator.strip()
    explicit = _EXPLICIT_LOCATOR_RE.search(value)
    if explicit:
        kind = "article" if explicit.group(1).lower().startswith("ст") else "point"
        return kind, normalize_section_id(explicit.group(2))

    section_id = normalize_section_id(value)
    if not re.fullmatch(r"\d+(?:[-.]\d+)*", section_id):
        raise ValueError(f"Не удалось распознать структурный номер: {locator}")
    if "." in section_id:
        return "point", section_id
    return "auto", section_id


def explicit_locators_from_query(query: str) -> list[str]:
    """Извлекает только явно названные статьи/пункты, не угадывая голые числа."""
    locators = []
    for match in _EXPLICIT_LOCATOR_RE.finditer(query):
        label = "статья" if match.group(1).lower().startswith("ст") else "пункт"
        locator = f"{label} {normalize_section_id(match.group(2))}"
        if locator not in locators:
            locators.append(locator)
    return locators


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
        "page_starts": page_starts,
        "article": _span_index(_section_spans(text, _ARTICLE_HEADING_RE)),
        "point": _span_index(_point_spans(text)),
    }


def cached_structural_index(cache_path: Path, pages: list[str]) -> dict:
    """Берёт индекс из кеша, а старый кеш однократно дополняет им."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        index = data.get("structure_index")
        if index:
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
    invalid = []
    seen_spans = set()
    for locator in locators:
        try:
            kind, section_id = parse_section_locator(locator)
        except ValueError:
            invalid.append(locator)
            continue

        candidates = ("article", "point") if kind == "auto" else (kind,)
        span = None
        matched_kind = None
        for candidate in candidates:
            candidate_spans = span_maps[candidate].get(section_id, [])
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
            "Неоднозначные номера, уточните статью или полный номер пункта: "
            + ", ".join(ambiguous) + "."
        )
    if invalid:
        details.append("Не распознаны: " + ", ".join(invalid) + ".")
    if omitted:
        details.append("Не помещены в лимит: " + ", ".join(omitted) + ".")

    blocks = []
    multi_page = len({item["page"] for item in selected}) > 1
    for item in selected:
        page = f", стр. {item['page']}" if multi_page else ""
        blocks.append(
            f"**{item['label']} {item['section_id']}{page}**\n{item['text']}"
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


import platform as _platform

if _platform.system() == "Windows":
    CHROME_PROFILE_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
elif _platform.system() == "Darwin":
    CHROME_PROFILE_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
else:
    CHROME_PROFILE_DIR = Path.home() / ".config" / "google-chrome"


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


def save_ilex_search_cache(
    query: str,
    max_results: int,
    results: list[dict],
) -> None:
    """Атомарно кеширует выдачу; сам пользовательский запрос не сохраняется."""
    if not results:
        return
    cache_path = ilex_search_cache_path(query)
    temp_path = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    data = {
        "cached_at": time.time(),
        "max_results": max_results,
        "results": results,
    }
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(cache_path)
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass


_ESSENTIAL_PROFILE_FILENAMES = {"Cookies", "Cookies-journal"}


def _copy_chrome_profile_tolerating_locks(profile_src: Path, profile_dest: Path) -> None:
    """
    Копирует профиль Chrome, не падая из-за некритичных файлов (Sessions,
    Safe Browsing Cookies и т.п.), заблокированных открытым настоящим Chrome —
    особенно часто на Windows, где такие блокировки эксклюзивны. Падаем только
    если заблокирован сам файл сессии входа: без него авторизация ilex.by
    невозможна, и лучше сообщить об этом явно, чем получить непонятный traceback.
    """
    import shutil

    try:
        shutil.copytree(
            profile_src,
            profile_dest,
            ignore=shutil.ignore_patterns(
                "SingletonLock",
                "SingletonCookie",
                "SingletonSocket",
                "lockfile",
            ),
        )
    except shutil.Error as exc:
        failures = exc.args[0] if exc.args else []
        essential_failures = [
            item for item in failures
            if Path(item[0]).name in _ESSENTIAL_PROFILE_FILENAMES
        ]
        if essential_failures:
            locked_name = Path(essential_failures[0][0]).name
            raise RuntimeError(
                f"Профиль Chrome заблокирован: файл сессии входа ({locked_name}) "
                "занят другим процессом — вероятно, у вас открыт настоящий Chrome "
                "с тем же профилем. Закройте все окна Chrome и повторите запрос."
            ) from exc
        log_perf(
            "chrome_profile_copy_nonessential_locked",
            count=len(failures),
            files=[Path(item[0]).name for item in failures],
        )


class PersistentChromeSession:
    """Одна авторизованная Chrome-сессия на весь срок жизни MCP-процесса."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._profile_dir: Path | None = None

    def _is_connected(self) -> bool:
        if self._context is None:
            return False
        try:
            browser = self._context.browser
            return browser is not None and browser.is_connected()
        except Exception:
            return False

    async def _close_unlocked(self) -> None:
        import shutil

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._profile_dir is not None:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    async def _ensure_started_unlocked(self) -> None:
        import platform
        import shutil
        import subprocess
        import tempfile
        from playwright.async_api import async_playwright

        if self._is_connected():
            log_perf("browser_reused")
            return

        await self._close_unlocked()
        self._profile_dir = Path(tempfile.mkdtemp())
        with perf_stage("chrome_profile_copy"):
            profile_src = CHROME_PROFILE_DIR / "Default"
            profile_dest = self._profile_dir / "Default"
            copied = False
            if platform.system() == "Darwin":
                try:
                    result = subprocess.run(
                        ["/bin/cp", "-cR", str(profile_src), str(profile_dest)],
                        capture_output=True,
                        timeout=30,
                    )
                    copied = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    copied = False
                if not copied:
                    shutil.rmtree(profile_dest, ignore_errors=True)
            if copied:
                log_perf("chrome_profile_copy_method", method="apfs_clone")
            else:
                _copy_chrome_profile_tolerating_locks(profile_src, profile_dest)
                log_perf("chrome_profile_copy_method", method="regular")
        self._playwright = await async_playwright().start()
        with perf_stage("chrome_launch"):
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                channel="chrome",
                headless=True,
                args=["--profile-directory=Default"],
                accept_downloads=True,
            )

    @asynccontextmanager
    async def page(self):
        """
        Сериализует операции ilex: сайт и профиль стабильнее работают с одной
        вкладкой за раз, а LLM обычно всё равно вызывает инструменты последовательно.
        """
        async with self._lock:
            await self._ensure_started_unlocked()
            page = await self._context.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                if not self._is_connected():
                    await self._close_unlocked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()


ILEX_BROWSER = PersistentChromeSession()


_ILEX_SESSION_ERROR_MARKER = "сессия не авторизована"


async def search_ilex(query: str, max_results: int = 10) -> list[dict]:
    """
    Ищет документы на ilex.by через поисковую строку.

    Помимо обычной выдачи search/extended ilex показывает тематические блоки
    (например, таблицу «Избежание двойного налогообложения»). Их строки приходят
    отдельным запросом classifier/content и поэтому раньше были невидимы MCP.
    """
    cached_results = load_ilex_search_cache(query, max_results)
    if cached_results is not None:
        return cached_results

    try:
        results = await _search_ilex_once(query, max_results)
    except RuntimeError as exc:
        # Профиль Chrome копируется один раз на весь срок MCP-процесса
        # (PersistentChromeSession). Если реальная сессия в Chrome обновилась
        # и протухший клон больше не авторизован — переклонируем профиль один
        # раз и повторим, прежде чем сообщать пользователю о неавторизованной
        # сессии (которая на деле может быть просто устаревшим снимком).
        if _ILEX_SESSION_ERROR_MARKER not in str(exc):
            raise
        await ILEX_BROWSER.close()
        results = await _search_ilex_once(query, max_results)

    save_ilex_search_cache(query, max_results, results)
    return results


async def _search_ilex_once(query: str, max_results: int) -> list[dict]:
    results = []
    async with ILEX_BROWSER.page() as page:
        # Перехватываем как обычную выдачу, так и тематические классификаторы.
        search_data = {}
        extended_loaded = asyncio.Event()
        smart_entities_loaded = asyncio.Event()
        classifier_loaded = asyncio.Event()
        classifier_expected = False

        async def capture(response):
            nonlocal classifier_expected
            if any(endpoint in response.url for endpoint in (
                "search/extended", "search/autocomplete", "search/smart-entities",
                "classifier/content"
            )):
                try:
                    data = await response.json()
                    search_data[response.url] = data
                    if "search/extended" in response.url:
                        extended_loaded.set()
                    elif "search/smart-entities" in response.url:
                        classifier_expected = bool(
                            isinstance(data, dict) and data.get("classifierBlockModel")
                        )
                        smart_entities_loaded.set()
                    elif "classifier/content" in response.url:
                        classifier_loaded.set()
                except Exception:
                    pass
        page.on("response", capture)

        with perf_stage("ilex_home_load"):
            await page.goto(
                "https://ilex-private.ilex.by/home",
                wait_until="networkidle",
                timeout=30000,
            )

        inp = await page.query_selector("input.search-input")
        if inp is None:
            raise RuntimeError(
                "Поле поиска не найдено на странице ilex.by — вероятно, сессия не авторизована "
                "(нужно войти в ilex.by в Chrome под тем же профилем) либо страница не успела "
                "загрузиться."
            )
        with perf_stage("ilex_search_wait"):
            await inp.click()
            await inp.fill(query)
            await page.wait_for_timeout(1500)

            btn = await page.query_selector("button.search-button")
            if btn:
                await btn.click()
            else:
                await inp.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=15000)
            # Обычная и тематическая выдачи загружаются независимо.
            for event, timeout in ((extended_loaded, 5), (smart_entities_loaded, 5)):
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            if classifier_expected and not classifier_loaded.is_set():
                try:
                    await asyncio.wait_for(classifier_loaded.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        # Тематический классификатор содержит более точные прямые ссылки на НПА.
        for url, data in search_data.items():
            if "classifier/content" in url and isinstance(data, dict):
                results.extend(parse_ilex_classifier_results(data, max_results))

        # Парсим обычные результаты из перехваченного API.
        for url, data in search_data.items():
            if "search/extended" in url and isinstance(data, dict):
                hits = data.get("hits", [])
                for hit in hits:
                    infobank = hit.get("infoBank", {}).get("value", "")
                    num = hit.get("numberInInfoBank")
                    name = hit.get("name", "").replace("<em>", "").replace("</em>", "")
                    snippet = hit.get("snippet", "").replace("<em>", "").replace("</em>", "")
                    if infobank and num:
                        doc_url = f"https://ilex-private.ilex.by/view-document/{infobank}/{num}/"
                        add_unique_ilex_result(results, {
                            "title": name,
                            "url": doc_url,
                            "snippet": snippet,
                            "source": "обычная выдача",
                        }, max_results)
                    if len(results) >= max_results:
                        break
                break

        # Fallback: парсим ссылки со страницы.
        if not results:
            links = await page.query_selector_all("a[href*='view-document']")
            seen = set()
            for link in links[:max_results]:
                href = await link.get_attribute("href")
                text = (await link.inner_text()).strip()
                if href and href not in seen:
                    seen.add(href)
                    full = href if href.startswith("http") else f"https://ilex-private.ilex.by{href}"
                    add_unique_ilex_result(results, {
                        "title": text[:120],
                        "url": full,
                        "snippet": "",
                        "source": "страница результатов",
                    }, max_results)

    return results


def parse_ilex_classifier_link(value: str) -> tuple[str, int, str | None] | None:
    """Разбирает внутреннюю ссылку ilex вида Б=BELAW_Д=13142_М=100012."""
    match = re.search(r"Б=([^_]+)_Д=(\d+)(?:_М=(\d+))?", value or "")
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


def canonical_ilex_document_url(url: str) -> str:
    """Убирает поисковый хвост и якорь, чтобы дедуплицировать один документ."""
    match = re.search(r"(https?://[^/]+/view-document/[^/]+/\d+/)", url)
    return match.group(1) if match else url.split("#", 1)[0].split("?", 1)[0]


def canonical_section_locator(locator: str) -> str:
    """Нормализует ссылку на норму для машинной проверки полноты."""
    kind, section_id = parse_section_locator(locator)
    if kind == "article":
        return f"статья {section_id}"
    if kind == "point":
        return f"пункт {section_id}"
    return section_id


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
        r"(?:, стр\. \d+)?\*\*\n"
        r"(.*?)(?=^\s*---\s*$|\Z)",
        result,
    ):
        label = "статья" if match.group(1) == "Статья" else "пункт"
        section_id = normalize_section_id(match.group(2))
        evidence["exact_section_texts"][
            f"{label} {section_id}"
        ] = match.group(3).strip()
    for locator in locators:
        try:
            canonical = canonical_section_locator(locator)
        except ValueError:
            continue
        kind, section_id = parse_section_locator(locator)
        label = "Статья" if kind == "article" else "Пункт"
        if kind == "auto":
            pattern = rf"\*\*(?:Статья|Пункт) {re.escape(section_id)}(?:,|\*)"
        else:
            pattern = rf"\*\*{label} {re.escape(section_id)}(?:,|\*)"
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
            candidate_text = data.get("text", "")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
) -> dict:
    entry_force = re.search(
        r"(?i)\bвступил[оа]?\s+в\s+силу\s+"
        r"(\d{1,2}\s+[а-яё]+\s+\d{4}\s+года|\d{2}\.\d{2}\.\d{4})",
        text[:8000],
    )
    return {
        "title": ilex_document_heading(text),
        "revision": revision,
        "entry_into_force": entry_force.group(1) if entry_force else None,
        "requires_related_review": document_requires_related_review(text),
        "future_change_markers": extract_future_change_markers(
            text, current_year=current_year
        ),
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


def add_unique_ilex_result(results: list[dict], result: dict, max_results: int) -> None:
    if len(results) >= max_results:
        return
    canonical = canonical_ilex_document_url(result["url"])
    if any(canonical_ilex_document_url(item["url"]) == canonical for item in results):
        return
    results.append(result)


def parse_ilex_classifier_results(data: dict, max_results: int = 10) -> list[dict]:
    """Преобразует строки тематической таблицы ilex в уникальные документы."""
    grouped: dict[tuple[str, int], dict] = {}
    for row in data.get("content", []):
        if not isinstance(row, dict):
            continue
        document_ref = parse_ilex_classifier_link(row.get("link_0", ""))
        if document_ref is None:
            continue
        infobank, number, segment = document_ref
        key = (infobank, number)
        item = grouped.setdefault(key, {
            "title": str(row.get("0", "")).strip(),
            "url": (
                f"https://ilex-private.ilex.by/view-document/{infobank}/{number}/"
                + (f"#M{segment}" if segment else "")
            ),
            "snippets": [],
        })
        details = [str(row.get(column, "")).strip() for column in ("1", "2", "3")]
        snippet = " — ".join(value for value in details if value)
        if snippet and snippet not in item["snippets"]:
            item["snippets"].append(snippet)

    results = []
    for item in grouped.values():
        results.append({
            "title": item["title"],
            "url": item["url"],
            "snippet": "; ".join(item["snippets"]),
            "source": "тематический классификатор ilex",
        })
        if len(results) >= max_results:
            break
    return results


def is_ilex_url(url: str) -> bool:
    return "ilex.by" in url


def url_to_ilex_cache_path(url: str) -> Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return ILEX_CACHE_DIR / f"{key}.json"


def extract_ilex_revision(title: str) -> str | None:
    match = re.search(r'\(ред\.\s*от\s*(\d{2}\.\d{2}\.\d{4})\)', title)
    return match.group(1) if match else None


async def get_ilex_title(url: str) -> str:
    """
    Быстро получает title страницы документа ilex.by (без клика по экспорту в Word) —
    используется только для проверки актуальности редакции перед решением, брать ли кеш.
    """
    async with ILEX_BROWSER.page() as page:
        with perf_stage("ilex_revision_page_load"):
            await page.goto(
                url, wait_until="domcontentloaded", timeout=30000
            )
        title = ""
        for _ in range(10):
            title = await page.title()
            if title:
                break
            await page.wait_for_timeout(300)
        return title


_ANSICPG_RE = re.compile(rb"\\ansicpg(\d+)")


def _rtf_codepage_name(raw_bytes: bytes) -> str:
    match = _ANSICPG_RE.search(raw_bytes)
    if not match:
        return "cp1252"
    return f"cp{match.group(1).decode('ascii')}"


def rtf_to_plain_text(rtf_path: Path) -> str:
    """
    Конвертирует RTF в текст. На macOS использует встроенный textutil — он даёт
    полный и корректно структурированный текст. Библиотека striprtf (кросс-платформенный
    фолбэк) на больших документах с таблицами теряет значительную часть содержимого.

    Экспорт ilex.by иногда кладёт кириллицу как сырые байты кодовой страницы,
    заявленной в \\ansicpg (обычно 1251), а не как \\'XX-escape. Декодирование
    таким utf-8 с errors="ignore" молча стирает всю кириллицу вместо ошибки —
    поэтому striprtf-фолбэк декодирует по кодовой странице, указанной в самом
    RTF-заголовке.
    """
    import platform
    import subprocess

    if platform.system() == "Darwin":
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(rtf_path)],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="ignore")

    from striprtf.striprtf import rtf_to_text
    raw_bytes = rtf_path.read_bytes()
    codepage = _rtf_codepage_name(raw_bytes)
    try:
        raw = raw_bytes.decode(codepage, errors="replace")
    except LookupError:
        raw = raw_bytes.decode("utf-8", errors="ignore")
    return rtf_to_text(raw)


async def get_ilex_document_content(url: str) -> tuple[str, str | None]:
    """
    Открывает документ ilex.by через headless Chrome и возвращает (текст, дата_редакции).
    Использует кнопку «Экспорт в Word» вместо чтения текста из DOM: у ilex большие документы
    рендерятся с виртуальным скроллом (в DOM всегда только видимая часть), поэтому прямое
    чтение #documentContent обрезает документ до нескольких первых экранов.
    """
    import shutil
    import tempfile

    export_dir = Path(tempfile.mkdtemp())
    try:
        async with ILEX_BROWSER.page() as page:
            with perf_stage("ilex_document_page_load"):
                await page.goto(url, wait_until="networkidle", timeout=30000)
            title = await page.title()

            export_btn = await page.query_selector(".export-word-button")
            if export_btn:
                with perf_stage("ilex_word_export_download"):
                    async with page.expect_download(timeout=90000) as download_info:
                        await export_btn.click()
                    download = await download_info.value
                    rtf_path = export_dir / "export.rtf"
                    await download.save_as(rtf_path)
                with perf_stage("rtf_to_text"):
                    text = rtf_to_plain_text(rtf_path)
            else:
                content_el = await page.query_selector("#documentContent")
                text = await content_el.inner_text() if content_el else await page.inner_text("body")
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)

    revision = extract_ilex_revision(title)
    return text, revision


async def fetch_ilex_pages(url: str, bypass_cache: bool = False) -> tuple[list[str] | str, str]:
    """
    Возвращает (список страниц | строка с ошибкой, статус кеша) для документа ilex.by.
    Перед использованием кеша проверяет актуальность через дату редакции в title страницы
    (лёгкая загрузка без клика по экспорту — быстрее полного скачивания в разы). Если у
    документа нет даты редакции в title (не все типы документов на ilex её содержат) —
    кеш считается доверенным без проверки, аналогично поведению для pravo.by без карточки.
    """
    cache_path = url_to_ilex_cache_path(url)
    forced_refresh = bypass_cache
    revision_changed = False

    if cache_path.exists() and not bypass_cache:
        with perf_stage("ilex_cache_read"):
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_revision = data.get("revision")
        current_revision = None
        try:
            current_revision = extract_ilex_revision(await get_ilex_title(url))
        except Exception:
            pass
        if current_revision and current_revision != cached_revision:
            bypass_cache = True
            revision_changed = True
        else:
            return [data["text"]], "cached"

    try:
        with perf_stage("ilex_document_fetch_total"):
            text, revision = await get_ilex_document_content(url)
    except Exception as e:
        return f"Ошибка загрузки документа: {e}", "error"

    if not text.strip():
        return "Документ загружен, но текст пуст.", "error"

    was_updated = cache_path.exists()
    with perf_stage("ilex_index_and_cache_write"):
        cache_path.write_text(json.dumps({
            "url": url,
            "text": text,
            "structure_index": build_structural_index([text]),
            "revision": revision,
            "cached_at": datetime.now().isoformat(),
        }, ensure_ascii=False), encoding="utf-8")

    if not was_updated:
        return [text], "downloaded"
    if revision_changed:
        return [text], "updated"
    return [text], "refreshed" if forced_refresh else "updated"


def ilex_cache_status_note(status: str) -> str:
    if status == "cached":
        return "_[из кеша, редакция актуальна]_\n\n"
    if status == "downloaded":
        return "_[загружено впервые]_\n\n"
    if status == "updated":
        return "_[⚠️ обнаружена новая редакция — кеш обновлён]_\n\n"
    if status == "refreshed":
        return "_[кеш принудительно обновлён по запросу]_\n\n"
    return ""


async def fetch_authenticated_page(url: str) -> str:
    """Скачивает страницу через реальный Chrome с профилем пользователя (headless)."""
    if is_ilex_url(url):
        # Документы ilex.by рендерятся с виртуальным скроллом — прямое чтение DOM обрезает
        # большие документы до нескольких первых экранов. get_ilex_document_content уже решает
        # это через экспорт в Word (см. поиск проблемы у search_ilex_document).
        text, _ = await get_ilex_document_content(url)
        return text

    async with ILEX_BROWSER.page() as page:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        text = await page.inner_text("body")
        return text


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
                "Ищет документы на ilex.by по текстовому запросу. "
                "Возвращает список найденных документов с заголовками и ссылками. "
                "Используй когда нужно найти НПА или статью по теме, а прямой ссылки нет. "
                "Запросы формулируй короткими и по теме («исчисление среднего заработка»), "
                "а не длинными формальными реквизитами акта («постановление Минтруда №47 "
                "об исчислении среднего заработка») — поиск ilex смысловой/полнотекстовый "
                "и на длинные запросы с номером постановления и органом часто не находит ничего, "
                "хотя тот же смысл коротким запросом находится сразу. "
                "После получения результатов используй get_ilex_sections, если номер нормы известен; "
                "search_ilex_document — если номер неизвестен; crawl_authenticated — только когда "
                "действительно нужен весь текст целиком."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос (например: 'статья 169 трудовой кодекс')"},
                    "max_results": {"type": "integer", "description": "Максимум результатов (по умолчанию 10)", "default": 10}
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
                "Текст кешируется на диск; актуальность редакции проверяется автоматически при "
                "каждом обращении. Не устанавливай bypass_cache=true без прямой просьбы пользователя "
                "или подтверждённого повреждения кеша: это запускает дорогой повторный экспорт документа. "
                "Внутри инструмент скачивает документ через кнопку «Экспорт в Word» на странице "
                "ilex.by и конвертирует RTF в текст — это происходит на стороне сервера и не "
                "требует от тебя никаких действий с файлами, но гарантирует полный текст документа "
                "(а не обрезанный DOM, как при прямом чтении страницы у больших документов)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL документа на ilex.by (view-document/...)"},
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
                "Можно получить несколько норм одного документа одним вызовом; реквизиты кеша и "
                "редакции выводятся один раз."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL документа ilex.by"},
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Например: ['статья 18', 'статья 261-3', 'пункт 21.4']"
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
                "возвращает заголовок, редакцию, дату вступления в силу и универсально ищет "
                "связанные протоколы/изменяющие документы среди ранее полученных BELAW и "
                "через поиск ILEX. Используй для каждого применимого документа в сложном "
                "или многодокументном вопросе. Найденные связанные документы необходимо "
                "получить либо явно оценить через validate_legal_research."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL первичного BELAW-документа"},
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
                "Скрапит страницу через реальный Chrome headless, используя активную сессию пользователя. "
                "Используй для ilex.by и других сайтов где требуется авторизация. "
                "Chrome открываться не будет — работает в фоне. Для документов ilex.by автоматически "
                "использует тот же механизм получения полного текста (экспорт в Word), что и "
                "search_ilex_document, но без поиска фрагментов — возвращает весь текст целиком."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL страницы"}
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
    max_results = arguments.get("max_results", 10)
    try:
        with perf_stage("ilex_search_total"):
            results = await search_ilex(query, max_results)
    except Exception as e:
        return [types.TextContent(type="text", text=f"Ошибка поиска: {e}")]
    if not results:
        return [types.TextContent(type="text", text=f"По запросу «{query}» ничего не найдено на ilex.by")]
    lines = [f"Найдено результатов: {len(results)}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"**{i}. {r['title']}**")
        lines.append(f"   {r['url']}")
        if r.get("source"):
            lines.append(f"   Источник результата: {r['source']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet'][:200]}")
        lines.append("")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def do_search_ilex_document(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    query = arguments["query"]
    max_results = arguments.get("max_results", MAX_FRAGMENTS)
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_ilex_pages(url, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = ilex_cache_status_note(status)
    with perf_stage("document_search"):
        result = search_with_structural_preference(
            pages, query, max_results=max_results, max_chars=max_chars
        )
    evidence = _research_evidence(url)
    evidence["document_searched"] = True
    evidence["revision_checked"] = status in {
        "cached", "downloaded", "updated", "refreshed"
    }
    locators = explicit_locators_from_query(query)
    if locators:
        record_exact_ilex_sections(url, locators, result, status)
    return [types.TextContent(type="text", text=note + result)]


async def do_get_ilex_sections(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    sections = arguments["sections"]
    max_chars = arguments.get("max_chars", MAX_RESPONSE_CHARS)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_ilex_pages(url, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = ilex_cache_status_note(status)
    with perf_stage("section_index_read_and_extract"):
        index = cached_structural_index(url_to_ilex_cache_path(url), pages)
        result = extract_structured_sections(
            pages, sections, max_chars=max_chars, structure_index=index
        )
    record_exact_ilex_sections(url, sections, result, status)
    return [types.TextContent(type="text", text=note + result)]


async def do_inspect_ilex_document(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    search_related = arguments.get("search_related", True)
    canonical_url = canonical_ilex_document_url(url)
    existing_evidence = _fresh_evidence(canonical_url)
    cache_path = url_to_ilex_cache_path(url)
    if (
        existing_evidence
        and existing_evidence["revision_checked"]
        and cache_path.exists()
    ):
        try:
            cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
            pages, status = [cached_data["text"]], "cached"
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pages, status = await fetch_ilex_pages(url, False)
    else:
        pages, status = await fetch_ilex_pages(url, False)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]

    text = "\n\n".join(pages)
    revision = None
    try:
        cache_data = json.loads(
            url_to_ilex_cache_path(url).read_text(encoding="utf-8")
        )
        revision = cache_data.get("revision")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    metadata = ilex_document_metadata(text, revision)
    candidates = (
        discover_related_cached_ilex_documents(url, text)
        if metadata["requires_related_review"] else []
    )
    search_error = None
    live_search_performed = False
    if search_related and metadata["requires_related_review"] and not candidates:
        live_search_performed = True
        try:
            results = await search_ilex(related_search_query(text), 10)
            for result in results:
                candidate_url = canonical_ilex_document_url(result.get("url", ""))
                candidate_title = result.get("title", "")
                if (
                    "/BELAW/" not in candidate_url
                    or candidate_url == canonical_ilex_document_url(url)
                    or not _RELATION_MARKER_RE.search(candidate_title)
                ):
                    continue
                score = related_document_score(text, candidate_title)
                if score < 0.35:
                    continue
                candidates.append({
                    "url": candidate_url,
                    "title": candidate_title,
                    "score": round(score, 3),
                    "source": "поиск связанных первичных документов ILEX",
                })
        except Exception as exc:
            search_error = str(exc)

    unique_candidates = {}
    for candidate in candidates:
        unique_candidates.setdefault(
            canonical_ilex_document_url(candidate["url"]), candidate
        )
    candidates = list(unique_candidates.values())

    evidence = _research_evidence(url)
    evidence["revision_checked"] = status in {
        "cached", "downloaded", "updated", "refreshed"
    }
    evidence["related_inspected"] = bool(
        not metadata["requires_related_review"]
        or candidates
        or (live_search_performed and not search_error)
    )
    evidence["related_candidates"] = candidates
    evidence["document_title"] = metadata["title"]

    lines = [
        ilex_cache_status_note(status).strip(),
        f"Документ: {metadata['title'] or '(заголовок не распознан)'}",
        f"URL: {canonical_ilex_document_url(url)}",
        f"Редакция в заголовке ILEX: {metadata['revision'] or 'не указана'}",
        f"Вступление в силу: {metadata['entry_into_force'] or 'не найдено в заголовочной части'}",
    ]
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
            "\n⚠️ Проверка связанных документов ограничена локальным индексом "
            "и не завершена. Повтори вызов с search_related=true."
        )
    elif metadata["requires_related_review"]:
        source = "локальный индекс и поиск ILEX" if live_search_performed else "локальный индекс"
        lines.append(f"\nСвязанные документы не обнаружены ({source}).")
    else:
        lines.append("\nОтдельная проверка связанных актов для этого вида документа не требуется.")
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
    try:
        text = await fetch_authenticated_page(url)
        if is_ilex_url(url):
            evidence = _research_evidence(url)
            evidence["full_text_loaded"] = True
        return [types.TextContent(type="text", text=text or "(пустая страница)")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Ошибка: {e}")]


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
        await ILEX_BROWSER.close()

if __name__ == "__main__":
    asyncio.run(main())
