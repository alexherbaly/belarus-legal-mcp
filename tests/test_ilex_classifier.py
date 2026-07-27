import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from server import (
    canonical_ilex_document_url,
    ilex_search_cache_path,
    load_ilex_search_cache,
    parse_ilex_classifier_link,
    parse_ilex_classifier_results,
    save_ilex_search_cache,
)


class IlexClassifierTests(unittest.TestCase):
    def test_parses_internal_classifier_link(self):
        self.assertEqual(
            parse_ilex_classifier_link("Б=BELAW_Д=13142_М=100012"),
            ("BELAW", 13142, "100012"),
        )

    def test_groups_multiple_articles_of_same_agreement(self):
        data = {
            "content": [
                {
                    "0": "Соглашение между Республикой Беларусь и Российской Федерацией",
                    "1": "Россия (РФ)",
                    "2": "Доходы от недвижимого имущества",
                    "3": "ст. 6",
                    "link_0": "Б=BELAW_Д=13142_М=100012",
                },
                {
                    "0": "Соглашение между Республикой Беларусь и Российской Федерацией",
                    "1": "Россия (РФ)",
                    "2": "Прибыль от предпринимательской деятельности",
                    "3": "ст. 7",
                    "link_0": "Б=BELAW_Д=13142_М=100012",
                },
            ]
        }

        results = parse_ilex_classifier_results(data)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["url"],
            "https://ilex-private.ilex.by/view-document/BELAW/13142/#M100012",
        )
        self.assertIn("ст. 6", results[0]["snippet"])
        self.assertIn("ст. 7", results[0]["snippet"])
        self.assertEqual(results[0]["source"], "тематический классификатор ilex")

    def test_canonicalizes_search_and_segment_variants(self):
        self.assertEqual(
            canonical_ilex_document_url(
                "https://ilex-private.ilex.by/view-document/BELAW/13142/query?searchKey=x#M100012"
            ),
            "https://ilex-private.ilex.by/view-document/BELAW/13142/",
        )

    def test_search_cache_reuses_only_fresh_positive_results(self):
        results = [{
            "title": "Трудовой кодекс",
            "url": "https://ilex-private.ilex.by/view-document/BELAW/219268/",
            "snippet": "",
            "source": "обычная выдача",
        }]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "server.ILEX_SEARCH_CACHE_DIR", Path(temp_dir)
        ), patch("server.ILEX_SEARCH_CACHE_TTL_SECONDS", 60):
            save_ilex_search_cache("статья 32 трудового кодекса", 5, results)

            self.assertEqual(
                load_ilex_search_cache("статья 32 трудового кодекса", 5),
                results,
            )
            self.assertNotIn(
                "статья 32 трудового кодекса",
                ilex_search_cache_path(
                    "статья 32 трудового кодекса"
                ).read_text(encoding="utf-8"),
            )
            self.assertIsNone(
                load_ilex_search_cache(
                    "статья 32 трудового кодекса",
                    5,
                    now=time.time() + 61,
                )
            )


if __name__ == "__main__":
    unittest.main()
