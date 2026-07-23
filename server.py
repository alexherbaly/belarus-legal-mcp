import asyncio
import io
import hashlib
import json
import math
import re
from bisect import bisect_right
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

CONTEXT_PARAGRAPHS = 1
MAX_FRAGMENTS = 5
MAX_PARAGRAPH_CHARS = 2000
MAX_RESPONSE_CHARS = 12000

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
    Статус кеша: 'cached', 'downloaded', 'updated', 'error'
    """
    import httpx
    from pypdf import PdfReader

    cache_path = url_to_cache_path(url)

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
        "cached_at": datetime.now().isoformat(),
        "last_revision": last_revision,
    }, ensure_ascii=False), encoding="utf-8")

    return pages, "updated" if was_updated else "downloaded"


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


def extract_structured_sections(
    pages: list[str],
    locators: list[str],
    max_chars: int = MAX_RESPONSE_CHARS,
) -> str:
    """
    Извлекает статьи/пункты целиком. Лимит мягкий: первая найденная норма никогда
    не обрезается; нормы, не поместившиеся после неё, перечисляются как пропущенные.
    """
    if not locators:
        return "Не указаны номера статей или пунктов."

    text, page_starts = _page_offsets(pages)
    article_spans = _section_spans(text, _ARTICLE_HEADING_RE)
    point_spans = _point_spans(text)
    span_maps = {
        "article": _span_index(article_spans),
        "point": _span_index(point_spans),
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
                    matched_kind = candidate
                    break
                ambiguous.append(locator)
                span = None
                matched_kind = None
                break
            if candidate_spans:
                span = candidate_spans[0]
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
    return ""


CHROME_PROFILE_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


async def search_ilex(query: str, max_results: int = 10) -> list[dict]:
    """
    Ищет документы на ilex.by через поисковую строку.

    Помимо обычной выдачи search/extended ilex показывает тематические блоки
    (например, таблицу «Избежание двойного налогообложения»). Их строки приходят
    отдельным запросом classifier/content и поэтому раньше были невидимы MCP.
    """
    import shutil
    import tempfile
    from playwright.async_api import async_playwright

    profile_src = CHROME_PROFILE_DIR / "Default"
    tmp_dir = Path(tempfile.mkdtemp())
    results = []
    try:
        shutil.copytree(
            profile_src, tmp_dir / "Default",
            ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"),
        )
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(tmp_dir),
                channel="chrome",
                headless=True,
                args=["--profile-directory=Default"],
            )
            page = await ctx.new_page()

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

            await page.goto("https://ilex-private.ilex.by/home", wait_until="networkidle", timeout=30000)

            inp = await page.query_selector("input.search-input")
            if inp is None:
                await ctx.close()
                raise RuntimeError(
                    "Поле поиска не найдено на странице ilex.by — вероятно, сессия не авторизована "
                    "(нужно войти в ilex.by в Chrome под тем же профилем) либо страница не успела "
                    "загрузиться."
                )
            await inp.click()
            await inp.fill(query)
            await page.wait_for_timeout(1500)

            btn = await page.query_selector("button.search-button")
            if btn:
                await btn.click()
            else:
                await inp.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=15000)
            # Обычная и тематическая выдачи загружаются независимо. Ждём фактические
            # API-ответы вместо фиксированной паузы, чтобы не пропустить более медленный
            # classifier/content и не замедлять запросы без тематического блока.
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

            # Тематический классификатор содержит более точные прямые ссылки на НПА,
            # поэтому ставим его результаты перед полнотекстовой выдачей.
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

            # Fallback: парсим ссылки со страницы
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

            await ctx.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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
    import shutil
    import tempfile
    from playwright.async_api import async_playwright

    profile_src = CHROME_PROFILE_DIR / "Default"
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(
            profile_src, tmp_dir / "Default",
            ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"),
        )
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(tmp_dir),
                channel="chrome",
                headless=True,
                args=["--profile-directory=Default"],
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = ""
            for _ in range(10):
                title = await page.title()
                if title:
                    break
                await page.wait_for_timeout(300)
            await ctx.close()
        return title
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def rtf_to_plain_text(rtf_path: Path) -> str:
    """
    Конвертирует RTF в текст. На macOS использует встроенный textutil — он даёт
    полный и корректно структурированный текст. Библиотека striprtf (кросс-платформенный
    фолбэк) на больших документах с таблицами теряет значительную часть содержимого.
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
    raw = rtf_path.read_text(encoding="utf-8", errors="ignore")
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
    from playwright.async_api import async_playwright

    profile_src = CHROME_PROFILE_DIR / "Default"
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(
            profile_src, tmp_dir / "Default",
            ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"),
        )
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(tmp_dir),
                channel="chrome",
                headless=True,
                args=["--profile-directory=Default"],
                accept_downloads=True,
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            title = await page.title()

            export_btn = await page.query_selector(".export-word-button")
            if export_btn:
                async with page.expect_download(timeout=90000) as download_info:
                    await export_btn.click()
                download = await download_info.value
                rtf_path = tmp_dir / "export.rtf"
                await download.save_as(rtf_path)
                await ctx.close()
                text = rtf_to_plain_text(rtf_path)
            else:
                content_el = await page.query_selector("#documentContent")
                text = await content_el.inner_text() if content_el else await page.inner_text("body")
                await ctx.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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

    if cache_path.exists() and not bypass_cache:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_revision = data.get("revision")
        current_revision = None
        try:
            current_revision = extract_ilex_revision(await get_ilex_title(url))
        except Exception:
            pass
        if current_revision and current_revision != cached_revision:
            bypass_cache = True
        else:
            return [data["text"]], "cached"

    try:
        text, revision = await get_ilex_document_content(url)
    except Exception as e:
        return f"Ошибка загрузки документа: {e}", "error"

    if not text.strip():
        return "Документ загружен, но текст пуст.", "error"

    was_updated = cache_path.exists()
    cache_path.write_text(json.dumps({
        "url": url,
        "text": text,
        "revision": revision,
        "cached_at": datetime.now().isoformat(),
    }, ensure_ascii=False), encoding="utf-8")

    return [text], "updated" if was_updated else "downloaded"


def ilex_cache_status_note(status: str) -> str:
    if status == "cached":
        return "_[из кеша, редакция актуальна]_\n\n"
    if status == "downloaded":
        return "_[загружено впервые]_\n\n"
    if status == "updated":
        return "_[⚠️ обнаружена новая редакция — кеш обновлён]_\n\n"
    return ""


async def fetch_authenticated_page(url: str) -> str:
    """Скачивает страницу через реальный Chrome с профилем пользователя (headless)."""
    if is_ilex_url(url):
        # Документы ilex.by рендерятся с виртуальным скроллом — прямое чтение DOM обрезает
        # большие документы до нескольких первых экранов. get_ilex_document_content уже решает
        # это через экспорт в Word (см. поиск проблемы у search_ilex_document).
        text, _ = await get_ilex_document_content(url)
        return text

    import shutil
    import tempfile
    from playwright.async_api import async_playwright

    profile_src = CHROME_PROFILE_DIR / "Default"
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(
            profile_src, tmp_dir / "Default",
            ignore=shutil.ignore_patterns("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"),
        )
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(tmp_dir),
                channel="chrome",
                headless=True,
                args=["--profile-directory=Default"],
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            text = await page.inner_text("body")
            await ctx.close()
        return text
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
                "После получения результатов читай нужный документ через search_ilex_document "
                "(точечный вопрос) или crawl_authenticated (весь текст целиком)."
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
                "Текст кешируется на диск; актуальность редакции проверяется автоматически при "
                "каждом обращении (bypass_cache для принудительного обновления). "
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
                    "bypass_cache": {"type": "boolean", "description": "Принудительно перезагрузить документ, игнорируя кеш", "default": False}
                },
                "required": ["url", "query"]
            }
        ),
        types.Tool(
            name="get_ilex_sections",
            description=(
                "Возвращает точный полный текст указанных статей или пунктов документа ilex.by. "
                "Используй вместо тематического поиска, когда номера структурных элементов известны. "
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
                    "bypass_cache": {"type": "boolean", "default": False}
                },
                "required": ["url", "sections"]
            }
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
                "Используй вместо download_pdf когда нужен ответ на конкретный вопрос по документу — "
                "экономит контекст в 10-20 раз. PDF кешируется; для pravo.by автоматически проверяет "
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
                    "bypass_cache": {"type": "boolean", "description": "Принудительно перекачать, игнорируя кеш", "default": False}
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


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
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
    elif name == "crawl_authenticated":
        return await do_crawl_authenticated(arguments)
    elif name == "download_pdf":
        return await do_download_pdf(arguments)
    elif name == "search_pdf":
        return await do_search_pdf(arguments)
    elif name == "get_pdf_sections":
        return await do_get_pdf_sections(arguments)
    raise ValueError(f"Unknown tool: {name}")


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
    result = search_with_structural_preference(
        pages, query, max_results=max_results, max_chars=max_chars
    )
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
    result = extract_structured_sections(pages, sections, max_chars=max_chars)
    return [types.TextContent(type="text", text=note + result)]


async def do_crawl_authenticated(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    try:
        text = await fetch_authenticated_page(url)
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
    result = extract_structured_sections(pages, sections, max_chars=max_chars)
    return [types.TextContent(type="text", text=note + result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
