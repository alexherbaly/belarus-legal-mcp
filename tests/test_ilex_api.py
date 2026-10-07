import asyncio
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

import server


# Пример ответа documents/{infobank}/{docId} из документации API ilex.
DOCUMENT_RESPONSE = {
    "segments": [
        {"100001": "ПОСТАНОВЛЕНИЕ МИНИСТЕРСТВА АРХИТЕКТУРЫ И СТРОИТЕЛЬСТВА РЕСПУБЛИКИ БЕЛАРУСЬ\n24 июня 2019 г. N 39\n\n"},
        {"100002": "ОБ ИЗМЕНЕНИИ ПОСТАНОВЛЕНИЯ МИНИСТЕРСТВА АРХИТЕКТУРЫ И СТРОИТЕЛЬСТВА РЕСПУБЛИКИ БЕЛАРУСЬ ОТ 7 ФЕВРАЛЯ 2019 Г. N 9\n\n"},
        {"100004": "1. Пункт 1 постановления изложить в следующей редакции:\n"},
        {"100005": "\"1. Установить размер одного человеко-часа.\".\n"},
        {"100006": "2. Настоящее постановление вступает в силу после его официального опубликования.\n\n"},
        {"100007": "Министр Д.М.Микуленок\n\n\n\n"},
    ],
    "properties": {
        "takeEffectDate": "25.07.2019",
        "controlType": "1",
        "infoBank": "BELAW",
        "edition": "d",
        "terminateEffectDate": "31.10.2021",
        "sourcePublication": "Национальный правовой Интернет-портал Республики Беларусь, 24.07.2019, 8/34355",
        "infoType": "a",
        "status": "+",
        "editionDate": "",
        "firstEdition": "184728",
        "numberInInfobank": "184728",
        "note": (
            "Начало действия документа - 25.07.2019.\n"
            "В соответствии с {СС_НУЛ_М=100006}пунктом 2{КСС} данный документ вступил в силу.\n"
            "Документ утратил силу в связи с принятием "
            "{СС_НУЛ_Б=BELAW_Д=175777_М=100011}постановления{КСС} от 01.10.2021 N 85."
        ),
        "kinds": "Постановление",
        "dates": "24.06.2019",
        "name": "Постановление Министерства архитектуры и строительства Республики Беларусь от 24.06.2019 N 39",
    },
}

SEARCH_RESPONSE = {
    "category": "lawyer",
    "query": "увольнение работника по статье 42",
    "documents": [
        {
            "title": "Трудовой <em>кодекс</em> Республики\nБеларусь",
            "infoBank": "BELAW",
            "number": 2637,
            "documentUrl": "https://ilex.by/view-document/BELAW/2637/#100042",
            "status": "Действующая редакция",
            "segments": [{"id": "100042", "text": "Статья 42. Расторжение трудового договора"}],
        },
        {
            "title": "Дубликат той же редакции",
            "infoBank": "BELAW",
            "number": 2637,
            "documentUrl": "https://ilex.by/view-document/BELAW/2637/",
            "status": "Действующая редакция",
            "segments": [],
        },
    ],
}


async def _no_sleep(_seconds):
    return None


class FakeIlexApi:
    """Минимальная имитация backend-client для httpx.MockTransport."""

    def __init__(self):
        self.calls = []
        self.tokens_issued = 0
        self.valid_tokens = set()
        self.document = json.loads(json.dumps(DOCUMENT_RESPONSE))
        self.history = {"BELAW": []}
        self.history_status = 200
        self.rate_limited_requests = 0
        self.guard_blocks_search = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/backend-client/api/v1")
        self.calls.append((request.method, path))
        if self.rate_limited_requests:
            self.rate_limited_requests -= 1
            return httpx.Response(509)
        if path == "/authenticate":
            body = json.loads(request.content)
            if body != {"username": "user@example.com", "password": "secret"}:
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.tokens_issued += 1
            token = f"token-{self.tokens_issued}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"token": token})
        if request.headers.get("x-auth-token") not in self.valid_tokens:
            return httpx.Response(401)
        if path == "/search/documents" and self.guard_blocks_search:
            return httpx.Response(405, headers={"content-type": "text/html"}, text=(
                "<html><head><title>405 Not Allowed</title></head><body><center>"
                "<h1>405 Not Allowed</h1></center><hr><center>nginx</center></body></html>"
            ))
        if path == "/search/documents":
            return httpx.Response(200, json=SEARCH_RESPONSE)
        if path == "/documents/history":
            return httpx.Response(self.history_status, json=self.history)
        if path == "/documents/BELAW/184728":
            return httpx.Response(200, json=self.document)
        if path == "/documents/BELAW/777":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                "<!DOCTYPE html><html><head><title>403 Forbidden</title></head><body>"
                "<h1>403 Forbidden</h1><p>Access to this resource blocked by guard "
                "service.</p></body></html>"
            ))
        if path == "/documents/BELAW/403":
            return httpx.Response(403, json={"message": "Нет доступа по фиче"})
        return httpx.Response(404)


class IlexApiTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIlexApi()
        self.api = server.IlexApiClient("https://ilex.test/backend-client/api/v1")
        self.api._client = httpx.AsyncClient(
            base_url="https://ilex.test/backend-client/api/v1",
            transport=httpx.MockTransport(self.fake),
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patches = [
            patch("server.ILEX_API", self.api),
            patch("server.ILEX_CACHE_DIR", Path(self.temp_dir.name) / "docs"),
            patch("server.ILEX_SEARCH_CACHE_DIR", Path(self.temp_dir.name) / "search"),
            patch.dict("os.environ", {
                "ILEX_USERNAME": "user@example.com",
                "ILEX_PASSWORD": "secret",
            }),
        ]
        for item in self.patches:
            item.start()
        (Path(self.temp_dir.name) / "docs").mkdir()
        (Path(self.temp_dir.name) / "search").mkdir()
        server._ILEX_DOCUMENT_LOCKS.clear()
        sleep_patch = patch("server.asyncio.sleep", new=_no_sleep)
        sleep_patch.start()
        self.patches.append(sleep_patch)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()


class DocumentParsingTests(unittest.TestCase):
    def test_parses_document_refs_from_urls_and_shorthand(self):
        self.assertEqual(
            server.parse_ilex_document_ref(
                "https://ilex.by/view-document/BELAW/2637/#100042"
            ),
            ("BELAW", 2637),
        )
        self.assertEqual(server.parse_ilex_document_ref("BELAW/175777"), ("BELAW", 175777))
        self.assertEqual(server.parse_ilex_document_ref("[BELAW/175777]"), ("BELAW", 175777))
        self.assertIsNone(server.parse_ilex_document_ref("https://pravo.by/document/"))

    def test_canonical_url_unifies_public_and_private_hosts(self):
        self.assertEqual(
            server.canonical_ilex_document_url(
                "https://ilex.by/view-document/BELAW/13142/query?searchKey=x#M100012"
            ),
            "https://ilex-private.ilex.by/view-document/BELAW/13142/",
        )
        self.assertEqual(
            server.canonical_ilex_document_url("BELAW/13142"),
            "https://ilex-private.ilex.by/view-document/BELAW/13142/",
        )

    def test_cleans_link_markup_and_keeps_cross_document_refs(self):
        cleaned = server.clean_ilex_markup(DOCUMENT_RESPONSE["properties"]["note"])

        self.assertIn("с пунктом 2 данный", cleaned)
        self.assertIn("постановления [BELAW/175777]", cleaned)
        self.assertNotIn("{СС", cleaned)
        self.assertEqual(server.ilex_linked_documents(cleaned), [("BELAW", 175777)])

    def test_builds_paragraph_text_that_structural_index_understands(self):
        text, properties, segment_ids = server.parse_ilex_api_document(DOCUMENT_RESPONSE)

        self.assertIn("\n\n2. Настоящее постановление вступает в силу", text)
        self.assertEqual(segment_ids[0], "100001")
        self.assertEqual(properties["infoBank"], "BELAW")
        result = server.extract_structured_sections([text], ["пункт 2"])
        self.assertIn("**Пункт 2**", result)
        self.assertIn("после его официального опубликования", result)

    def test_cleans_html_note_from_live_api(self):
        note = (
            "<p>Начало действия редакции - 01.01.2026.</p>\n"
            "<p>Изменения, внесенные <document-link infobank=\"BELAW\" "
            "doc-id=\"219162\" segment-id=\"100016\" link-type=\"НУЛ\" "
            "class=\"readonly-element hidden-element\">Законом</document-link> "
            "от 08.07.2024 N 25-З, <document-link infobank=\"BELAW\" "
            "doc-id=\"219162\" segment-id=\"100271\">вступивших"
            "<document-additional-link infobank=\"BELAW\" doc-id=\"219162\" "
            "segment-id=\"100271\" id=\"undefined1\" class=\"hidden-element\">"
            "</document-additional-link></document-link> в силу &laquo;с 1 "
            "января 2026 года&raquo;.</p>"
        )

        cleaned = server.clean_ilex_markup(note)

        self.assertEqual(
            cleaned,
            "Начало действия редакции - 01.01.2026.\n\n"
            "Изменения, внесенные Законом [BELAW/219162] от 08.07.2024 N 25-З, "
            "вступивших [BELAW/219162] в силу «с 1 января 2026 года».",
        )
        self.assertEqual(server.ilex_linked_documents(cleaned), [("BELAW", 219162)])

    def test_keeps_comparison_signs_in_plain_norm_text(self):
        text = "если доход < 5 базовых величин и > 1 базовой величины"

        self.assertEqual(server.clean_ilex_markup(text), text)

    def test_uses_own_number_of_composite_segment_key(self):
        _, _, segment_ids = server.parse_ilex_api_document({
            "segments": [
                {"104267##104247,100001,104245": "Зарегистрировано в Национальном реестре"},
                {"104275": "Республики Беларусь 27 июля 1999 г. N 2/70"},
            ],
            "tablesSegments": [],
            "properties": {},
        })

        self.assertEqual(segment_ids, ["104267", "104275"])

    def test_joins_point_number_cell_with_its_text(self):
        text, _, segment_ids = server.parse_ilex_api_document({
            "segments": [
                {"100001": "55 лет"},
                {"100002": "21.4."},
                {"100003": "о предоставлении трудовых отпусков"},
                {"100004": "3 года"},
                {"100005": "21.5."},
                {"100006": "иные приказы"},
            ],
            "properties": {},
        })

        self.assertIn("21.4. о предоставлении трудовых отпусков", text)
        self.assertEqual(segment_ids, ["100001", "100002", "100004", "100005"])
        result = server.extract_structured_sections([text], ["пункт 21.4"])
        self.assertIn("3 года", result)
        self.assertNotIn("иные приказы", result)

    def test_detects_lost_amendment_notes_in_amended_document(self):
        stripped = "Статья 42. Норма\n\nТекст нормы, кроме утративших силу (утратил силу ранее)."
        annotated = (
            "Статья 42. Норма\n\n(в ред. Закона Республики Беларусь от 18.07.2019 N 219-З)"
            "\n\nТекст нормы."
        )
        excluded = "2. Норма.\n\n(п. 3 исключен. - Постановление Минтруда от 24.12.2013 N 128)"

        self.assertTrue(server.amendment_notes_missing(stripped, {"editionDate": "09.12.2025"}))
        self.assertFalse(server.amendment_notes_missing(stripped, {"editionDate": ""}))
        self.assertFalse(server.amendment_notes_missing(annotated, {"editionDate": "09.12.2025"}))
        self.assertFalse(server.amendment_notes_missing(excluded, {"editionDate": "24.12.2013"}))

    def test_merges_table_segments_by_segment_number(self):
        text, _, _ = server.parse_ilex_api_document({
            "segments": [{"100001": "Первый абзац"}, {"100003": "Третий абзац"}],
            "tablesSegments": [{"100002": "Ячейка таблицы"}],
            "properties": {},
        })

        self.assertEqual(text, "Первый абзац\n\nЯчейка таблицы\n\nТретий абзац")

    def test_warns_about_inapplicable_editions(self):
        warnings = server.ilex_document_warnings(
            DOCUMENT_RESPONSE["properties"], today=date(2026, 10, 5)
        )
        self.assertTrue(any("Утратил силу" in item for item in warnings))
        self.assertTrue(any("окончено 31.10.2021" in item for item in warnings))

        future = server.ilex_document_warnings(
            {"edition": "v _Редакция с изменениями, не вступившими в силу",
             "status": "-", "infoType": "a_Нормативный акт"},
            today=date(2026, 10, 5),
        )
        self.assertEqual(len(future), 1)
        self.assertIn("ещё не вступили в силу", future[0])

        current_until = server.ilex_document_warnings(
            {"edition": "d_Последняя редакция",
             "status": "-_Все акты, кроме утративших силу или не вступивших в силу",
             "infoType": "a_Нормативный акт", "terminateEffectDate": "30.06.2027",
             "controlType": "5_Тип=5. СОЗДАНА НОВАЯ РЕДАКЦИЯ С ИЗМЕНЕНИЯМИ, НЕ ВСТУПИВШИМИ В СИЛУ"},
            today=date(2026, 10, 5),
        )
        self.assertEqual(len(current_until), 1)
        self.assertIn("действует по 30.06.2027", current_until[0])

        technical_end_date = server.ilex_document_warnings(
            {"edition": "d", "status": "-", "infoType": "a",
             "terminateEffectDate": "31.12.2035"},
            today=date(2026, 10, 5),
        )
        self.assertEqual(technical_end_date, [])
        self.assertEqual(
            server.ilex_authorities_label(
                "Минстройархитектуры (МАиС)_Министерство архитектуры; "
                "· Международный акт ·_· Международный акт ·"
            ),
            "Министерство архитектуры; · Международный акт ·",
        )

        older = server.ilex_document_warnings(
            {"edition": "n", "firstEdition": "100", "infoBank": "BELAW"},
            today=date(2026, 10, 5),
        )
        self.assertIn("BELAW/100", older[0])

    def test_metadata_prefers_api_card(self):
        text, properties, _ = server.parse_ilex_api_document(DOCUMENT_RESPONSE)

        metadata = server.ilex_document_metadata(text, properties=properties)

        self.assertIn("ПОСТАНОВЛЕНИЕ", metadata["title"].upper())
        self.assertEqual(metadata["entry_into_force"], "25.07.2019")
        self.assertEqual(metadata["linked_documents"], [("BELAW", 175777)])
        self.assertFalse(metadata["requires_related_review"])

    def test_parses_search_results_without_duplicates(self):
        results = server.parse_ilex_search_results(SEARCH_RESPONSE)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Трудовой кодекс Республики Беларусь")
        self.assertEqual(
            results[0]["url"],
            "https://ilex-private.ilex.by/view-document/BELAW/2637/",
        )
        self.assertEqual(results[0]["status"], "Действующая редакция")
        self.assertEqual(results[0]["segment_id"], "100042")

    def test_reads_dotenv_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text(
                "# comment\nexport ILEX_USERNAME=user@example.com\n"
                "ILEX_PASSWORD='p=ss word'\n",
                encoding="utf-8",
            )
            values = server.load_dotenv_values(path)

        self.assertEqual(values["ILEX_USERNAME"], "user@example.com")
        self.assertEqual(values["ILEX_PASSWORD"], "p=ss word")


class ApiClientTests(IlexApiTestCase):
    def test_sends_ilex_api_client_user_agent(self):
        seen = []
        api = server.IlexApiClient("https://ilex.test/backend-client/api/v1")
        transport = httpx.MockTransport(
            lambda request: (seen.append(request.headers.get("user-agent")),
                             httpx.Response(200, json={"token": "t"}))[1]
        )
        original = httpx.AsyncClient

        def client_with_transport(**kwargs):
            return original(transport=transport, **kwargs)

        with patch("httpx.AsyncClient", side_effect=client_with_transport):
            asyncio.run(api._authenticate(None))
            asyncio.run(api.close())

        self.assertEqual(seen, ["ilex-api-client/1.0"])

    def test_authenticates_once_and_reuses_token(self):
        asyncio.run(self.api.search_documents("статья 42"))
        asyncio.run(self.api.search_documents("статья 43"))

        self.assertEqual(self.fake.tokens_issued, 1)
        self.assertEqual(
            [call for call in self.fake.calls if call[1] == "/authenticate"],
            [("POST", "/authenticate")],
        )

    def test_reauthenticates_once_after_expired_token(self):
        asyncio.run(self.api.search_documents("статья 42"))
        self.fake.valid_tokens.clear()

        data = asyncio.run(self.api.search_documents("статья 42"))

        self.assertEqual(data["documents"][0]["number"], 2637)
        self.assertEqual(self.fake.tokens_issued, 2)

    def test_reports_missing_credentials(self):
        with patch.dict("os.environ", {"ILEX_USERNAME": "", "ILEX_PASSWORD": ""}), \
                patch("server.load_dotenv_values", return_value={}):
            with self.assertRaises(server.IlexApiError) as error:
                asyncio.run(self.api.search_documents("статья 42"))

        self.assertIn("ILEX_USERNAME", str(error.exception))

    def test_forbidden_document_is_reported_as_error(self):
        document, status = asyncio.run(server.fetch_ilex_document("BELAW/403"))

        self.assertEqual(status, "error")
        self.assertIn("нет доступа", document)

    def test_retries_after_rate_limit(self):
        self.fake.rate_limited_requests = 2

        data = asyncio.run(self.api.search_documents("статья 42"))

        self.assertEqual(data["documents"][0]["number"], 2637)

    def test_reports_persistent_rate_limit(self):
        self.fake.rate_limited_requests = 10

        with self.assertRaises(server.IlexApiError) as error:
            asyncio.run(self.api.search_documents("статья 42"))

        self.assertIn("лимит частоты", str(error.exception))

    def test_reports_guard_block_on_get_with_status_200(self):
        document, status = asyncio.run(server.fetch_ilex_document("BELAW/777"))

        self.assertEqual(status, "error")
        self.assertIn("заблокирован защитным фильтром ilex", document)
        self.assertIn("blocked by guard service", document)
        self.assertNotIn("<html", document.lower())

    def test_reports_guard_block_on_post_as_nginx_405(self):
        self.fake.guard_blocks_search = True

        with self.assertRaises(server.IlexApiError) as error:
            asyncio.run(self.api.search_documents("статья 42"))

        self.assertIn("заблокирован защитным фильтром ilex", str(error.exception))
        self.assertIn("405 Not Allowed", str(error.exception))

    def test_rejects_too_long_search_query_before_request(self):
        with self.assertRaises(server.IlexApiError):
            asyncio.run(server.search_ilex("налог " * 60))

        self.assertEqual(self.fake.calls, [])

    def test_search_results_are_cached(self):
        first = asyncio.run(server.search_ilex("увольнение работника", 5))
        second = asyncio.run(server.search_ilex("увольнение работника", 10))

        self.assertEqual(first, second)
        self.assertEqual(
            len([call for call in self.fake.calls if call[1] == "/search/documents"]),
            1,
        )


class RateLimiterTests(unittest.TestCase):
    def test_waits_for_window_instead_of_exceeding_limit(self):
        now = [0.0]
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        limiter = server.SlidingWindowRateLimiter(2, window=60.0, clock=lambda: now[0])

        async def run():
            with patch("server.asyncio.sleep", new=fake_sleep):
                for _ in range(3):
                    await limiter.acquire()
                    now[0] += 1.0

        asyncio.run(run())

        self.assertEqual(slept, [58.0])

    def test_rate_limit_retry_waits_for_next_window(self):
        response = httpx.Response(509)

        self.assertEqual(server._ilex_retry_delay(response, 0), 20.0)
        self.assertEqual(
            server._ilex_retry_delay(httpx.Response(509, headers={"Retry-After": "5"}), 0),
            5.0,
        )
        self.assertEqual(server._ilex_retry_delay(httpx.Response(503), 0), 2.0)


class DocumentCacheTests(IlexApiTestCase):
    URL = "https://ilex-private.ilex.by/view-document/BELAW/184728/"

    def _calls(self, path):
        return [call for call in self.fake.calls if call[1] == path]

    def _age_cache(self, seconds):
        cache_path = server.url_to_ilex_cache_path(self.URL)
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        data["checked_at"] = time.time() - seconds
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_recent_cache_is_used_without_network(self):
        _, first = asyncio.run(server.fetch_ilex_document(self.URL))
        document, second = asyncio.run(server.fetch_ilex_document("BELAW/184728"))

        self.assertEqual((first, second), ("downloaded", "cached"))
        self.assertEqual(len(self._calls("/documents/BELAW/184728")), 1)
        self.assertEqual(self._calls("/documents/history"), [])
        self.assertEqual(document["properties"]["name"][:13], "Постановление")

    def test_history_without_publication_confirms_cache(self):
        asyncio.run(server.fetch_ilex_document(self.URL))
        self._age_cache(server.ILEX_REVISION_CHECK_INTERVAL_SECONDS + 5)

        _, status = asyncio.run(server.fetch_ilex_document(self.URL))

        self.assertEqual(status, "cached")
        self.assertEqual(len(self._calls("/documents/history")), 1)
        self.assertEqual(len(self._calls("/documents/BELAW/184728")), 1)

    def test_week_old_cache_skips_history_and_reloads(self):
        asyncio.run(server.fetch_ilex_document(self.URL))
        self._age_cache(7 * 24 * 60 * 60)

        _, status = asyncio.run(server.fetch_ilex_document(self.URL))

        self.assertEqual(status, "cached")
        self.assertEqual(self._calls("/documents/history"), [])
        self.assertEqual(len(self._calls("/documents/BELAW/184728")), 2)

    def test_history_publication_reloads_changed_document(self):
        asyncio.run(server.fetch_ilex_document(self.URL))
        self._age_cache(server.ILEX_REVISION_CHECK_INTERVAL_SECONDS + 5)
        self.fake.history = {"BELAW": [184728]}
        self.fake.document["segments"].append({"100009": "3. Новый пункт."})

        document, status = asyncio.run(server.fetch_ilex_document(self.URL))

        self.assertEqual(status, "updated")
        self.assertIn("3. Новый пункт.", document["text"])

    def test_unavailable_history_reloads_instead_of_trusting_cache(self):
        asyncio.run(server.fetch_ilex_document(self.URL))
        self._age_cache(server.ILEX_REVISION_CHECK_INTERVAL_SECONDS + 5)
        self.fake.history_status = 500

        _, status = asyncio.run(server.fetch_ilex_document(self.URL))

        self.assertEqual(status, "cached")
        self.assertEqual(len(self._calls("/documents/BELAW/184728")), 2)

    def test_unavailable_api_returns_cache_marked_unverified(self):
        asyncio.run(server.fetch_ilex_document(self.URL))
        self._age_cache(server.ILEX_REVISION_CHECK_INTERVAL_SECONDS + 5)
        self.fake.rate_limited_requests = 100

        result = asyncio.run(server.do_get_ilex_sections({
            "url": self.URL,
            "sections": ["пункт 2"],
        }))[0].text

        self.assertIn("актуальность редакции НЕ проверена", result)
        evidence = server._research_evidence(self.URL)
        self.assertFalse(evidence["revision_checked"])

    def test_sections_response_includes_edition_warning(self):
        result = asyncio.run(server.do_get_ilex_sections({
            "url": self.URL,
            "sections": ["пункт 2"],
        }))[0].text

        self.assertIn("BELAW/184728", result)
        self.assertIn("Утратил силу", result)
        self.assertIn("редакционные примечания", result)
        self.assertIn("**Пункт 2**", result)

    def test_inspector_lists_documents_from_note(self):
        result = asyncio.run(server.do_inspect_ilex_document({
            "url": self.URL,
            "search_related": False,
        }))[0].text

        self.assertIn("Начало действия редакции: 25.07.2019", result)
        self.assertIn("view-document/BELAW/175777/", result)


if __name__ == "__main__":
    unittest.main()
