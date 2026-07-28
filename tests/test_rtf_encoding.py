import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import _rtf_codepage_name, rtf_to_plain_text


def _cp1251_rtf(word: str) -> bytes:
    return (
        rb"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}\f0\fs24 "
        + word.encode("cp1251")
        + rb"\par}"
    )


class RtfEncodingTests(unittest.TestCase):
    def test_detects_declared_codepage(self):
        self.assertEqual(
            _rtf_codepage_name(rb"{\rtf1\ansi\ansicpg1251\deff0"), "cp1251"
        )

    def test_defaults_to_cp1252_without_declared_codepage(self):
        self.assertEqual(_rtf_codepage_name(rb"{\rtf1\ansi\deff0"), "cp1252")

    def test_recovers_cyrillic_from_raw_cp1251_bytes(self):
        # ilex.by иногда кладёт кириллицу как сырые байты \ansicpg-кодировки,
        # а не как \'XX-escape. utf-8 с errors="ignore" молча стирал такие
        # байты целиком — регрессия на BELAW/204956 (Указ Президента №40).
        rtf_bytes = _cp1251_rtf("Привет мир")
        with tempfile.TemporaryDirectory() as temp_dir:
            rtf_path = Path(temp_dir) / "export.rtf"
            rtf_path.write_bytes(rtf_bytes)

            with patch("platform.system", return_value="Linux"):
                text = rtf_to_plain_text(rtf_path)

        self.assertIn("Привет мир", text)


if __name__ == "__main__":
    unittest.main()
