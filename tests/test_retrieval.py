import unittest

from server import (
    MAX_FRAGMENTS,
    MAX_RESPONSE_CHARS,
    build_structural_index,
    cache_status_note,
    explicit_locators_from_query,
    extract_structured_sections,
    ilex_cache_status_note,
    parse_section_locator,
    search_in_pages,
    search_with_structural_preference,
)


SAMPLE_DOCUMENT = [
    """КОДЕКС

Статья 18. Форма трудового договора

Трудовой договор заключается в письменной форме.

Каждая страница подписывается работником и нанимателем.

Статья 19. Начало действия трудового договора

Трудовой договор действует со дня его подписания.""",
    """Статья 261‑3. Срок действия контракта

Конкретный срок определяется по соглашению сторон.

Продление контракта осуществляется по соглашению сторон.

Статья 262. Иная норма

Текст иной нормы.

21.3. Приказы о приеме на работу.

Хранятся 75 лет.

21.4. Приказы о предоставлении трудовых отпусков.

21.4.1. Документы о переносе отпуска.

Хранятся вместе с приказом.

Хранятся 3 года.

21.5. Иные приказы.

Хранятся 5 лет."""
]


class StructuredRetrievalTests(unittest.TestCase):
    def test_defaults_are_token_bounded(self):
        self.assertEqual(MAX_FRAGMENTS, 5)
        self.assertEqual(MAX_RESPONSE_CHARS, 12000)

    def test_forced_refresh_is_not_reported_as_new_revision(self):
        self.assertIn("принудительно обновлён", cache_status_note("refreshed"))
        self.assertIn(
            "принудительно обновлён", ilex_cache_status_note("refreshed")
        )
        self.assertNotIn(
            "новая редакция", ilex_cache_status_note("refreshed")
        )

    def test_extracts_multiple_articles_in_one_response(self):
        result = extract_structured_sections(
            SAMPLE_DOCUMENT, ["статья 18", "статья 261-3"]
        )

        self.assertIn("Статья 18", result)
        self.assertIn("Трудовой договор заключается", result)
        self.assertIn("Статья 261-3", result)
        self.assertIn("Продление контракта осуществляется", result)
        self.assertNotIn("Статья 19. Начало действия", result)
        self.assertNotIn("Статья 262. Иная норма", result)

    def test_extracts_numbered_point_without_neighbors(self):
        result = extract_structured_sections(SAMPLE_DOCUMENT, ["пункт 21.4"])

        self.assertIn("Пункт 21.4", result)
        self.assertIn("предоставлении трудовых отпусков", result)
        self.assertIn("Документы о переносе отпуска", result)
        self.assertNotIn("Приказы о приеме", result)
        self.assertNotIn("Иные приказы", result)

    def test_point_stops_before_hyphenated_sibling(self):
        pages = [
            """1. Основное правило.

Продолжение основного правила.

1-1. Самостоятельное специальное правило.

2. Следующее правило."""
        ]

        result = extract_structured_sections(pages, ["пункт 1"])

        self.assertIn("Продолжение основного правила", result)
        self.assertNotIn("Самостоятельное специальное правило", result)

    def test_recognizes_explicit_locators_and_unicode_dash(self):
        locators = explicit_locators_from_query(
            "Найди статью 18, пункт 21.4 и ст. 261‑3"
        )

        self.assertEqual(
            locators, ["статья 18", "пункт 21.4", "статья 261-3"]
        )

    def test_budget_omits_later_sections_without_cutting_first(self):
        result = extract_structured_sections(
            SAMPLE_DOCUMENT,
            ["статья 18", "статья 261-3"],
            max_chars=80,
        )

        self.assertIn("Трудовой договор заключается", result)
        self.assertIn("Не помещены в лимит: Статья 261-3", result)
        self.assertNotIn("Продление контракта осуществляется", result)

    def test_explicit_article_uses_structured_retrieval(self):
        result = search_with_structural_preference(
            SAMPLE_DOCUMENT,
            "Статья 18. Форма трудового договора",
        )

        self.assertIn("Извлечено структурных элементов: 1", result)
        self.assertNotIn("Статья 19. Начало действия", result)

    def test_unrecognized_structure_falls_back_to_search(self):
        pages = ["Раздел без заголовков статей\n\nСтатья упомянута в тексте."]
        result = search_with_structural_preference(
            pages,
            "статья 999",
            max_results=1,
        )

        self.assertIn("Найдено совпадений:", result)

    def test_search_respects_soft_character_budget(self):
        pages = [
            "редкий термин один\n\nконтекст один",
            "редкий термин два\n\nконтекст два",
            "редкий термин три\n\nконтекст три",
        ]
        result = search_in_pages(
            pages,
            "редкий термин",
            context=0,
            max_results=3,
            max_chars=25,
        )

        self.assertIn("пропущено по лимиту размера", result)
        self.assertLess(result.count("редкий термин"), 3)

    def test_ambiguous_point_number_returns_selectable_candidates(self):
        pages = [
            """Статья 1. Первая

1. Первый пункт.

Статья 2. Вторая

1. Другой первый пункт."""
        ]
        result = extract_structured_sections(pages, ["пункт 1"])

        self.assertIn("Неоднозначные номера", result)
        self.assertIn("пункт 1#1", result)
        self.assertIn("пункт 1#2", result)
        self.assertIn("Первый пункт", result)
        self.assertIn("Другой первый пункт", result)

    def test_selects_specific_ambiguous_point_by_occurrence(self):
        pages = [
            """ПРИКАЗЫВАЮ:

1. Установить перечень государств.

2. Утвердить Положение.

ПОЛОЖЕНИЕ

1. Настоящее Положение определяет порядок.

2. Для целей Положения используются термины."""
        ]

        first = extract_structured_sections(pages, ["пункт 1#1"])
        second = extract_structured_sections(pages, ["пункт 1#2"])

        self.assertIn("**Пункт 1#1**", first)
        self.assertIn("Установить перечень", first)
        self.assertNotIn("Настоящее Положение", first)
        self.assertIn("**Пункт 1#2**", second)
        self.assertIn("Настоящее Положение", second)
        self.assertNotIn("Установить перечень", second)

    def test_parses_occurrence_selector_from_explicit_locator(self):
        self.assertEqual(
            parse_section_locator("пункт 1#2"),
            ("point", "1", 2),
        )
        self.assertEqual(
            explicit_locators_from_query("получи пункт 1#2 и статью 4"),
            ["пункт 1#2", "статья 4"],
        )

    def test_prefers_full_article_over_table_of_contents_entry(self):
        pages = [
            """ОГЛАВЛЕНИЕ
Статья 35. Компетенция общего собрания
Статья 36. Проведение общего собрания

ОСНОВНОЙ ТЕКСТ
Статья 35. Компетенция общего собрания

К компетенции общего собрания относятся важные вопросы.

Полный текст статьи продолжается.

Статья 36. Проведение общего собрания

Текст следующей статьи."""
        ]
        result = extract_structured_sections(pages, ["статья 35"])

        self.assertIn("К компетенции общего собрания", result)
        self.assertIn("Полный текст статьи продолжается", result)

    def test_uses_prebuilt_structural_index(self):
        index = build_structural_index(SAMPLE_DOCUMENT)

        result = extract_structured_sections(
            SAMPLE_DOCUMENT,
            ["статья 18", "пункт 21.4"],
            structure_index=index,
        )

        self.assertIn("Трудовой договор заключается", result)
        self.assertIn("Документы о переносе отпуска", result)


if __name__ == "__main__":
    unittest.main()
