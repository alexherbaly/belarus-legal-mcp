import asyncio
import io
import hashlib
import json
import math
import re
from datetime import datetime, timedelta
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
ILEX_CACHE_TTL_HOURS = 24

CONTEXT_PARAGRAPHS = 1
MAX_FRAGMENTS = 15

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
    words = re.findall(r'[а-яёa-z]+', text.lower())
    return [normalize(w) for w in words if len(w) > 2]


def search_in_pages(pages: list[str], query: str, context: int = CONTEXT_PARAGRAPHS, max_results: int = MAX_FRAGMENTS) -> str:
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
        paragraphs = [p.strip() for p in re.split(r'\n{2,}', page_text) if p.strip()]
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

    scored = []
    for page_num, (paragraphs, para_tokens_list) in enumerate(pages_paragraphs, 1):
        for i, tokens in enumerate(para_tokens_list):
            matched = keyword_set & set(tokens)
            if not matched:
                continue
            score = sum(idf[kw] for kw in matched)
            start = max(0, i - context)
            end = min(len(paragraphs), i + context + 1)
            fragment = "\n\n".join(paragraphs[start:end])
            scored.append((score, page_num, fragment))

    if not scored:
        return f"По запросу «{query}» ничего не найдено в документе."

    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]
    top.sort(key=lambda x: x[1])

    header = f"Найдено совпадений: {len(scored)}, показано топ {len(top)} по релевантности\n\n---\n\n"
    parts = [f"**[Стр. {page_num}]**\n{fragment}" for _, page_num, fragment in top]
    return header + "\n\n---\n\n".join(parts)


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
    """Ищет документы на ilex.by через поисковую строку. Возвращает список {title, url, snippet}."""
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

            # Перехватываем ответ search/extended
            search_data = {}
            async def capture(response):
                if "search/extended" in response.url or "search/autocomplete" in response.url:
                    try:
                        search_data[response.url] = await response.json()
                    except Exception:
                        pass
            page.on("response", capture)

            await page.goto("https://ilex-private.ilex.by/home", wait_until="networkidle", timeout=30000)

            inp = await page.query_selector("input.search-input")
            await inp.click()
            await inp.fill(query)
            await page.wait_for_timeout(1500)

            btn = await page.query_selector("button.search-button")
            if btn:
                await btn.click()
            else:
                await inp.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(1000)

            # Парсим результаты из перехваченного API
            for url, data in search_data.items():
                if "search/extended" in url and isinstance(data, dict):
                    hits = data.get("hits", [])
                    for hit in hits[:max_results]:
                        infobank = hit.get("infoBank", {}).get("value", "")
                        num = hit.get("numberInInfoBank")
                        name = hit.get("name", "").replace("<em>", "").replace("</em>", "")
                        snippet = hit.get("snippet", "").replace("<em>", "").replace("</em>", "")
                        if infobank and num:
                            doc_url = f"https://ilex-private.ilex.by/view-document/{infobank}/{num}/"
                            results.append({"title": name, "url": doc_url, "snippet": snippet})
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
                        results.append({"title": text[:120], "url": full, "snippet": ""})

            await ctx.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return results


def is_ilex_url(url: str) -> bool:
    return "ilex.by" in url


def url_to_ilex_cache_path(url: str) -> Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return ILEX_CACHE_DIR / f"{key}.json"


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
                async with page.expect_download(timeout=30000) as download_info:
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

    revision_match = re.search(r'\(ред\.\s*от\s*(\d{2}\.\d{2}\.\d{4})\)', title)
    revision = revision_match.group(1) if revision_match else None
    return text, revision


async def fetch_ilex_pages(url: str, bypass_cache: bool = False) -> tuple[list[str] | str, str]:
    """
    Возвращает (список страниц | строка с ошибкой, статус кеша) для документа ilex.by.
    Кеш валиден ILEX_CACHE_TTL_HOURS часов (у ilex нет открытого API для дешёвой проверки актуальности).
    """
    cache_path = url_to_ilex_cache_path(url)

    if cache_path.exists() and not bypass_cache:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at < timedelta(hours=ILEX_CACHE_TTL_HOURS):
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
        return f"_[из кеша, проверено менее {ILEX_CACHE_TTL_HOURS}ч назад]_\n\n"
    if status == "downloaded":
        return "_[загружено впервые]_\n\n"
    if status == "updated":
        return "_[кеш истёк (>24ч) — документ перезагружен]_\n\n"
    return ""


async def fetch_authenticated_page(url: str) -> str:
    """Скачивает страницу через реальный Chrome с профилем пользователя (headless)."""
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
            content_el = await page.query_selector("#documentContent") if is_ilex_url(url) else None
            text = await content_el.inner_text() if content_el else await page.inner_text("body")
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
                "После получения результатов открывай нужный документ через crawl_authenticated."
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
                "Текст кешируется на 24 часа (bypass_cache для принудительного обновления). "
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
                    "max_results": {"type": "integer", "description": "Максимум фрагментов в ответе (по умолчанию 15)", "default": 15},
                    "bypass_cache": {"type": "boolean", "description": "Принудительно перезагрузить документ, игнорируя кеш", "default": False}
                },
                "required": ["url", "query"]
            }
        ),
        types.Tool(
            name="crawl_authenticated",
            description=(
                "Скрапит страницу через реальный Chrome headless, используя активную сессию пользователя. "
                "Используй для ilex.by и других сайтов где требуется авторизация. "
                "Chrome открываться не будет — работает в фоне."
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
                    "max_results": {"type": "integer", "description": "Максимум фрагментов в ответе (по умолчанию 15)", "default": 15},
                    "bypass_cache": {"type": "boolean", "description": "Принудительно перекачать, игнорируя кеш", "default": False}
                },
                "required": ["url", "query"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "crawl":
        return await do_crawl(arguments)
    elif name == "search_ilex":
        return await do_search_ilex(arguments)
    elif name == "search_ilex_document":
        return await do_search_ilex_document(arguments)
    elif name == "crawl_authenticated":
        return await do_crawl_authenticated(arguments)
    elif name == "download_pdf":
        return await do_download_pdf(arguments)
    elif name == "search_pdf":
        return await do_search_pdf(arguments)
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
        if r["snippet"]:
            lines.append(f"   {r['snippet'][:200]}")
        lines.append("")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def do_search_ilex_document(arguments: dict) -> list[types.TextContent]:
    url = arguments["url"]
    query = arguments["query"]
    max_results = arguments.get("max_results", MAX_FRAGMENTS)
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_ilex_pages(url, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = ilex_cache_status_note(status)
    result = search_in_pages(pages, query, max_results=max_results)
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
    bypass_cache = arguments.get("bypass_cache", False)
    pages, status = await fetch_pdf_pages(url, referer, bypass_cache)
    if isinstance(pages, str):
        return [types.TextContent(type="text", text=pages)]
    note = cache_status_note(status)
    result = search_in_pages(pages, query, max_results=max_results)
    return [types.TextContent(type="text", text=note + result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
