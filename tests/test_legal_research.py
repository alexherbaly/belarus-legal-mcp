import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import server


AGREEMENT_URL = "https://ilex-private.ilex.by/view-document/BELAW/13142/"
PROTOCOL_URL = "https://ilex-private.ilex.by/view-document/BELAW/74991/"
TAX_CODE_URL = "https://ilex-private.ilex.by/view-document/BELAW/198143/"

AGREEMENT_TEXT = """
СОГЛАШЕНИЕ МЕЖДУ ПРАВИТЕЛЬСТВОМ РЕСПУБЛИКИ БЕЛАРУСЬ И
ПРАВИТЕЛЬСТВОМ РОССИЙСКОЙ ФЕДЕРАЦИИ ОБ ИЗБЕЖАНИИ
ДВОЙНОГО НАЛОГООБЛОЖЕНИЯ И ПРЕДОТВРАЩЕНИИ УКЛОНЕНИЯ ОТ
УПЛАТЫ НАЛОГОВ В ОТНОШЕНИИ НАЛОГОВ НА ДОХОДЫ И ИМУЩЕСТВО

Вступило в силу 21 января 1997 года
"""

PROTOCOL_TEXT = """
ПРОТОКОЛ К СОГЛАШЕНИЮ МЕЖДУ ПРАВИТЕЛЬСТВОМ
РЕСПУБЛИКИ БЕЛАРУСЬ И ПРАВИТЕЛЬСТВОМ РОССИЙСКОЙ ФЕДЕРАЦИИ
ОБ ИЗБЕЖАНИИ ДВОЙНОГО НАЛОГООБЛОЖЕНИЯ И ПРЕДОТВРАЩЕНИИ
УКЛОНЕНИЯ ОТ УПЛАТЫ НАЛОГОВ В ОТНОШЕНИИ НАЛОГОВ НА ДОХОДЫ
И ИМУЩЕСТВО ОТ 21 АПРЕЛЯ 1995 ГОДА

Вступил в силу 31 мая 2007 года
"""


class LegalResearchCompletenessTests(unittest.TestCase):
    def setUp(self):
        server._LEGAL_RESEARCH_EVIDENCE.clear()

    def test_extracts_document_heading_and_status(self):
        metadata = server.ilex_document_metadata(AGREEMENT_TEXT)

        self.assertIn("РОССИЙСКОЙ ФЕДЕРАЦИИ", metadata["title"])
        self.assertEqual(metadata["entry_into_force"], "21 января 1997 года")
        self.assertTrue(metadata["requires_related_review"])

    def test_surfaces_future_change_marker(self):
        text = """
ЗАКОН РЕСПУБЛИКИ БЕЛАРУСЬ

Изменения вступают в силу с 1 января 2027 года.
"""

        metadata = server.ilex_document_metadata(text, current_year=2026)

        self.assertEqual(len(metadata["future_change_markers"]), 1)
        self.assertIn("2027", metadata["future_change_markers"][0])

    def test_discovers_protocol_without_hardcoded_country_registry(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "server.ILEX_CACHE_DIR", Path(temp_dir)
        ):
            cache = Path(temp_dir) / "protocol.json"
            cache.write_text(json.dumps({
                "url": PROTOCOL_URL,
                "text": PROTOCOL_TEXT,
            }, ensure_ascii=False), encoding="utf-8")

            candidates = server.discover_related_cached_ilex_documents(
                AGREEMENT_URL, AGREEMENT_TEXT
            )

        self.assertEqual([item["url"] for item in candidates], [PROTOCOL_URL])

    def test_does_not_record_section_omitted_by_budget(self):
        result = (
            "Извлечено структурных элементов: 1. "
            "Не помещены в лимит: Статья 20.\n\n"
            "---\n\n**Статья 14**\nТекст"
        )

        server.record_exact_ilex_sections(
            AGREEMENT_URL,
            ["статья 14", "статья 20"],
            result,
            "cached",
        )

        evidence = server._research_evidence(AGREEMENT_URL)
        self.assertIn("статья 14", evidence["exact_sections"])
        self.assertNotIn("статья 20", evidence["exact_sections"])

    def test_blocks_answer_when_related_protocol_was_not_assessed(self):
        agreement = server._research_evidence(AGREEMENT_URL)
        agreement["revision_checked"] = True
        agreement["related_inspected"] = True
        agreement["exact_sections"] = {"статья 14", "статья 20"}
        agreement["related_candidates"] = [{
            "url": PROTOCOL_URL,
            "title": "Протокол к соглашению",
        }]

        result = server.validate_legal_research_state([{
            "url": AGREEMENT_URL,
            "sections": ["статья 14", "статья 20"],
        }])

        self.assertFalse(result["complete"])
        self.assertTrue(any(PROTOCOL_URL in gap for gap in result["gaps"]))

    def test_cross_border_tax_bundle_passes_only_when_all_norms_exist(self):
        tax_code = server._research_evidence(TAX_CODE_URL)
        tax_code["revision_checked"] = True
        tax_code["related_inspected"] = True
        tax_code["exact_sections"] = {
            "статья 197", "статья 216", "статья 223", "статья 224"
        }

        agreement = server._research_evidence(AGREEMENT_URL)
        agreement["revision_checked"] = True
        agreement["related_inspected"] = True
        agreement["exact_sections"] = {"статья 14", "статья 20"}
        agreement["related_candidates"] = [{
            "url": PROTOCOL_URL,
            "title": "Протокол к соглашению",
        }]

        protocol = server._research_evidence(PROTOCOL_URL)
        protocol["revision_checked"] = True
        protocol["related_inspected"] = True
        protocol["exact_sections"] = {"статья 1"}

        result = server.validate_legal_research_state([
            {
                "url": TAX_CODE_URL,
                "sections": [
                    "статья 197", "статья 216", "статья 223", "статья 224"
                ],
            },
            {
                "url": AGREEMENT_URL,
                "sections": ["статья 14", "статья 20"],
            },
            {
                "url": PROTOCOL_URL,
                "sections": ["статья 1"],
            },
        ])

        self.assertTrue(result["complete"], result["gaps"])

    def test_inspector_reuses_revision_checked_cache(self):
        evidence = server._research_evidence(AGREEMENT_URL)
        evidence["revision_checked"] = True

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "server.ILEX_CACHE_DIR", Path(temp_dir)
        ), patch(
            "server.fetch_ilex_pages", new_callable=AsyncMock
        ) as fetch:
            cache_path = server.url_to_ilex_cache_path(AGREEMENT_URL)
            cache_path.write_text(json.dumps({
                "url": AGREEMENT_URL,
                "text": AGREEMENT_TEXT,
                "revision": None,
            }, ensure_ascii=False), encoding="utf-8")

            result = asyncio.run(server.do_inspect_ilex_document({
                "url": AGREEMENT_URL,
                "search_related": False,
            }))

        fetch.assert_not_awaited()
        self.assertIn("21 января 1997 года", result[0].text)


if __name__ == "__main__":
    unittest.main()
