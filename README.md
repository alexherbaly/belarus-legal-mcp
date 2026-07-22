# belarus-legal-mcp

MCP-сервер для Claude Desktop, который добавляет инструменты для работы с белорусским законодательством:

- **pravo.by** — скачивание и поиск по PDF с автопроверкой актуальности редакции
- **ilex.by** — поиск и чтение документов через авторизованную сессию Chrome

## Инструменты

| Инструмент | Описание |
|-----------|---------|
| `crawl` | Скрапинг HTML-страницы → Markdown |
| `download_pdf` | Скачать PDF целиком (с кешем и проверкой редакции для pravo.by) |
| `search_pdf` | Найти релевантные фрагменты в PDF (~97% экономии контекста) |
| `search_ilex` | Поиск документов на ilex.by по запросу |
| `search_ilex_document` | Найти релевантные фрагменты в документе ilex.by по запросу (кеш 24ч, экономия контекста в 10-20 раз) |
| `crawl_authenticated` | Чтение страницы ilex.by через вашу сессию Chrome (headless) |

## Установка

### macOS

```bash
# 1. Создать окружение
python3 -m venv ~/.claude/mcp_servers/crawl4ai_env
~/.claude/mcp_servers/crawl4ai_env/bin/pip install crawl4ai mcp pypdf httpx pymorphy3 playwright striprtf
~/.claude/mcp_servers/crawl4ai_env/bin/playwright install chromium

# 2. Скопировать сервер
cp server.py ~/.claude/mcp_servers/crawl4ai_server.py
```

Добавить в `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "crawl4ai": {
      "command": "/Users/ИМЯ/.claude/mcp_servers/crawl4ai_env/bin/python",
      "args": ["/Users/ИМЯ/.claude/mcp_servers/crawl4ai_server.py"]
    }
  }
}
```

### Windows

```cmd
python -m venv %USERPROFILE%\.claude\mcp_servers\crawl4ai_env
%USERPROFILE%\.claude\mcp_servers\crawl4ai_env\Scripts\pip install crawl4ai mcp pypdf httpx pymorphy3 playwright striprtf
%USERPROFILE%\.claude\mcp_servers\crawl4ai_env\Scripts\playwright install chromium
```

Добавить в `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "crawl4ai": {
      "command": "C:\\Users\\ИМЯ\\.claude\\mcp_servers\\crawl4ai_env\\Scripts\\python.exe",
      "args": ["C:\\Users\\ИМЯ\\.claude\\mcp_servers\\crawl4ai_server.py"]
    }
  }
}
```

> Замените `ИМЯ` на ваше имя пользователя.

Перезапустить Claude Desktop.

## Использование

### Работа с pravo.by

```
Скачай документ: https://pravo.by/upload/docs/op/W21226212_1344459600.pdf
```

```
Найди в этом PDF всё про персональные данные:
url: https://pravo.by/upload/docs/op/W21226212_1344459600.pdf
query: персональные данные
```

Сервер автоматически:
- Кеширует PDF на диск (`~/.claude/mcp_servers/pdf_cache/`)
- При каждом обращении проверяет, не вышла ли новая редакция на pravo.by
- Уведомляет, если кеш был обновлён

### Работа с ilex.by

Нужна активная авторизованная сессия Chrome.

```
Найди на ilex: статья 169 трудовой кодекс
```

```
Открой документ: https://ilex-private.ilex.by/view-document/BELAW/219268/
```

Для точечного вопроса по конкретному документу — без загрузки полного текста:

```
Найди в этом документе про уведомление об отпуске:
url: https://ilex-private.ilex.by/view-document/BELAW/219268/
query: уведомление о начале отпуска
```

Chrome при этом визуально не открывается — всё работает headless. Текст документа кешируется на 24 часа (у ilex.by нет открытого API для дешёвой проверки актуальности редакции, поэтому используется TTL вместо точной сверки, как для pravo.by). Принудительно обновить: `bypass_cache: true`.

## Системный промпт

Системный промпт **не заменяет** MCP-сервер — без него инструменты просто не появятся у Claude. Промпт лишь указывает Claude *когда и как* их использовать (например, автоматически идти в ilex.by при неполном тексте на pravo.by). Нужны оба компонента.

Рекомендуется добавить в системный промпт Claude:

```
При вопросах о законодательстве Республики Беларусь всегда используй прямой
web_fetch на карточку документа на pravo.by (не полагайся на кэш веб-поиска).
Проверяй блок «Изменения и дополнения» вверху документа, чтобы не процитировать
норму, которая ещё не вступила в силу. Указывай в ответе дату/номер последнего
закона о внесении изменений. Если документ является PDF-файлом (ссылка
заканчивается на .pdf), скачивай его через инструмент mcp__crawl4ai__download_pdf
(или mcp__crawl4ai__search_pdf для точечного вопроса) с параметром referer
равным корневому домену сайта. Если на pravo.by не удаётся
получить полный консолидированный текст документа (PDF недоступен, текст обрезан
или отсутствует актуальная редакция) — сначала найди документ через
mcp__crawl4ai__search_ilex, затем прочитай нужный фрагмент через
mcp__crawl4ai__search_ilex_document (или mcp__crawl4ai__crawl_authenticated,
если нужен весь текст целиком).
```

## Как это устроено: ilex.by и виртуальный скроллинг

Большие документы на ilex.by рендерятся с виртуальным скроллом — в DOM браузера
всегда присутствует только видимая на экране часть, остальное подгружается и
выгружается по мере прокрутки. Из-за этого прямое чтение текста со страницы
(`page.inner_text()`) обрезает документ до нескольких первых экранов — для
документа на 1200+ пунктов можно получить лишь ~100 из них.

Решение: сервер кликает по кнопке «Экспорт в Word» на странице документа,
перехватывает скачивание получившегося `.rtf`-файла и конвертирует его в текст.
Это даёт полный документ вместо обрезанного фрагмента. На macOS конвертация
идёт через встроенный `textutil`; на других платформах — через библиотеку
`striprtf` (даёт менее точный результат на документах с крупными таблицами).

## Требования

- Python 3.10+
- Google Chrome (установленный, для `crawl_authenticated`, `search_ilex` и `search_ilex_document`)
- Подписка на ilex.by с активной сессией в Chrome (для ilex-инструментов)
- macOS — для наиболее точного извлечения текста документов ilex.by (`textutil`);
  на других ОС используется библиотека `striprtf` (`pip install striprtf`)
