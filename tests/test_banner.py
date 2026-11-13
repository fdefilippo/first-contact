import base64
import quopri
import unittest

from firstcontact.banner import (
    DEFAULT_RESUMED_WARNING,
    DEFAULT_WARNING,
    build_banner_body,
    build_html_banner,
    is_protected,
    preview,
    render_warning,
    sanitize_text,
)


class SanitizeTest(unittest.TestCase):
    def test_control_characters_and_spaces(self):
        self.assertEqual(sanitize_text("a\r\nb\tc"), "a b c")
        self.assertEqual(sanitize_text("  tanti   spazi  "), "tanti spazi")

    def test_truncation(self):
        value = sanitize_text("x" * 200, maxlen=20)
        self.assertEqual(len(value), 20)
        self.assertTrue(value.endswith("..."))

    def test_empty(self):
        self.assertEqual(sanitize_text(None), "")
        self.assertEqual(sanitize_text(""), "")


class RenderTest(unittest.TestCase):
    def test_placeholders(self):
        text = render_warning(
            "Da {sender} a {recipient} ({recipients}) il {first_seen} via {client}",
            sender="mario@example.com",
            recipients=["anna@example.it", "bruno@example.it"],
            first_seen="2023-11-14T22:13:20Z",
            client="203.0.113.9",
        )
        self.assertIn("mario@example.com", text)
        self.assertIn("anna@example.it", text)
        self.assertIn("bruno@example.it", text)
        self.assertIn("2023-11-14T22:13:20Z", text)
        self.assertIn("203.0.113.9", text)

    def test_default_template_mentions_attachments_and_links(self):
        text = render_warning(
            DEFAULT_WARNING, sender="mario@example.com",
            recipients=["anna@example.it"], first_seen="adesso",
        )
        self.assertIn("allegati", text.lower())
        self.assertIn("link", text.lower())

    def test_wrapping_is_78_columns(self):
        text = render_warning(
            " ".join(["parola"] * 40), sender="a@b.it",
            recipients=[], first_seen="adesso",
        )
        self.assertGreater(len(text.splitlines()), 1)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 78)

    def test_empty_template_uses_default(self):
        text = render_warning("", sender="a@b.it", recipients=["c@d.it"], first_seen="x")
        self.assertIn("ATTENZIONE", text)

    def test_braces_are_safe(self):
        text = render_warning("{} {sender} {altro}", sender="a@b.it",
                              recipients=["c@d.it"], first_seen="x")
        self.assertIn("{altro}", text)

    def test_resumed_placeholders(self):
        text = render_warning(
            DEFAULT_RESUMED_WARNING,
            sender="mario@example.com",
            recipients=["anna@example.it"],
            first_seen="2026-11-11T10:00:00Z",
            days=120,
            last_seen="2026-07-14T10:00:00Z",
        )
        self.assertIn("mario@example.com", text)
        self.assertIn("120 giorni", text)
        self.assertNotIn("{days}", text)
        self.assertNotIn("{last_seen}", text)
        self.assertIn("identita", text)

    def test_missing_days_are_rendered_empty(self):
        text = render_warning(
            "assenza: {days} ({last_seen})",
            sender="a@b.it",
            recipients=[],
            first_seen="x",
        )
        self.assertIn("assenza:  ()", text)


class BuildTest(unittest.TestCase):
    def test_plain_body_keeps_its_content_type(self):
        rewrite = build_banner_body(
            b"Ciao, come stai?\n",
            "ATTENZIONE: primo contatto",
            content_type="text/plain; charset=utf-8",
            content_transfer_encoding="7bit",
        )
        self.assertIsNotNone(rewrite)
        self.assertEqual(rewrite.parts, 1)
        body = rewrite.body
        self.assertIn(b"ATTENZIONE: primo contatto", body)
        self.assertTrue(body.endswith(b"Ciao, come stai?\n"))
        # nessuna nuova parte MIME e nessun header nel corpo
        self.assertNotIn(b"multipart/", body)
        self.assertNotIn(b"Content-Type:", body)

    def test_original_body_is_preserved_byte_for_byte(self):
        original = b"\x00\xffbinario\r\nsenza newline finale"
        rewrite = build_banner_body(original, "avviso")
        self.assertTrue(rewrite.body.endswith(original))

    def test_html_body_gets_the_box_inside_body(self):
        original = b'<html><body class="x"><p>Ciao</p></body></html>'
        rewrite = build_banner_body(
            original, "ATTENZIONE", content_type="text/html; charset=utf-8"
        )
        body = rewrite.body
        self.assertIn(b'<body class="x">', body)
        self.assertIn(b"background-color:#ffff00", body)
        self.assertIn(b"color:#000000", body)
        self.assertIn(b"border:2px dashed #000000", body)
        self.assertIn(b"<p>Ciao</p>", body)
        # il box e' dentro il body, non un nuovo documento
        self.assertNotIn(b"<!DOCTYPE", body)
        self.assertLess(body.index(b"<body"), body.index(b"background-color:#ffff00"))

    def test_html_without_body_tag_is_prepended(self):
        rewrite = build_banner_body(b"<p>Ciao</p>", "avviso", content_type="text/html")
        self.assertTrue(rewrite.body.startswith(b"<table"))
        self.assertTrue(rewrite.body.endswith(b"<p>Ciao</p>"))

    def test_multipart_alternative_gets_both_parts(self):
        original = (
            b"--alt\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"testo semplice\r\n"
            b"--alt\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n\r\n"
            b"<html><body><p>html</p></body></html>\r\n"
            b"--alt--\r\n"
        )
        rewrite = build_banner_body(
            original,
            "ATTENZIONE",
            content_type='multipart/alternative; boundary="alt"',
        )
        body = rewrite.body
        self.assertEqual(rewrite.parts, 2)
        self.assertIn(b"testo semplice", body)
        self.assertIn(b"<p>html</p>", body)
        self.assertIn(b"background-color:#ffff00", body)
        self.assertIn(b"- - -", body)
        # stessa struttura: stessi boundary, nessun multipart/mixed aggiunto
        self.assertEqual(body.count(b"--alt"), original.count(b"--alt"))
        self.assertNotIn(b"multipart/mixed", body)

    def test_multipart_mixed_leaves_attachment_untouched(self):
        attachment = b"R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        original = (
            b"--mix\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"corpo\r\n"
            b"--mix\r\n"
            b"Content-Type: image/gif\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="x.gif"\r\n\r\n'
            + attachment
            + b"\r\n--mix--\r\n"
        )
        rewrite = build_banner_body(
            original,
            "ATTENZIONE",
            content_type='multipart/mixed; boundary="mix"',
        )
        self.assertEqual(rewrite.parts, 1)
        self.assertIn(b"ATTENZIONE", rewrite.body)
        self.assertIn(attachment, rewrite.body)
        self.assertIn(b'filename="x.gif"', rewrite.body)
        self.assertIn(b"--mix--", rewrite.body)

    def test_text_attachment_is_not_modified(self):
        original = (
            b"--mix\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b'Content-Disposition: attachment; filename="note.txt"\r\n\r\n'
            b"allegato\r\n"
            b"--mix--\r\n"
        )
        self.assertIsNone(
            build_banner_body(
                original,
                "avviso",
                content_type='multipart/mixed; boundary="mix"',
            )
        )

    def test_base64_part_keeps_its_encoding(self):
        original = base64.encodebytes(b"Ciao")
        rewrite = build_banner_body(
            original,
            "avviso",
            content_type="text/plain; charset=utf-8",
            content_transfer_encoding="base64",
        )
        decoded = base64.b64decode(rewrite.body)
        self.assertIn(b"avviso", decoded)
        self.assertTrue(decoded.endswith(b"Ciao"))

    def test_quoted_printable_part_keeps_its_encoding(self):
        original = quopri.encodestring(b"Ciao = \xe8")
        rewrite = build_banner_body(
            original,
            "avviso",
            content_type="text/plain; charset=utf-8",
            content_transfer_encoding="quoted-printable",
        )
        self.assertIn(b"=E8", rewrite.body.upper())
        decoded = quopri.decodestring(rewrite.body)
        self.assertTrue(decoded.endswith(b"Ciao = \xe8"))

    def _assert_crlf_only(self, data: bytes) -> None:
        """Ogni fine riga e' CRLF: niente CR doppi, niente LF nudi."""
        self.assertNotIn(b"\r\r", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_quoted_printable_crlf_soft_breaks_stay_valid(self):
        # Come arriva da Postfix via milter: righe CRLF e soft break "=\r\n".
        html_part = (
            b'<html><body><p>Versione web <a href="https://example.org/nl/link?'
            + b"c=1bvtu&amp;d=ctd&amp;h=" + b"x" * 60
            + b'">link</a></p><p>Inoltra</p></body></html>\n'
        )
        original = quopri.encodestring(html_part).replace(b"\n", b"\r\n")
        self.assertNotIn(b"\r\r", original)
        self.assertIn(b"=\r\n", original)
        rewrite = build_banner_body(
            original,
            "ATTENZIONE: primo messaggio da "
            "bounce-back.mnid2m.4364.1441726.13229.0.2409.c2577067325._@mn1.newsabbonati.it",
            content_type="text/html; charset=utf-8",
            content_transfer_encoding="quoted-printable",
        )
        self._assert_crlf_only(rewrite.body)
        for line in rewrite.body.split(b"\r\n"):
            self.assertLessEqual(len(line), 76)
        decoded = quopri.decodestring(rewrite.body)
        self.assertIn(b"mn1.newsabbonati.it", decoded)
        self.assertNotIn(b"newsabbonati=", decoded)
        self.assertIn(html_part.rstrip(b"\n").split(b"<body>")[1], decoded)

    def test_base64_crlf_lines_stay_crlf(self):
        original = base64.encodebytes(b"Ciao " * 40).replace(b"\n", b"\r\n")
        rewrite = build_banner_body(
            original,
            "avviso",
            content_type="text/plain; charset=utf-8",
            content_transfer_encoding="base64",
        )
        self._assert_crlf_only(rewrite.body)

    def test_8bit_crlf_body_gets_crlf_banner(self):
        original = b"riga uno\r\nriga due\r\n"
        rewrite = build_banner_body(
            original,
            "prima riga\nseconda riga",
            content_type="text/plain; charset=utf-8",
            content_transfer_encoding="8bit",
        )
        self._assert_crlf_only(rewrite.body)
        self.assertTrue(rewrite.body.endswith(original))

    def test_signed_message_is_not_rewritten(self):
        self.assertIsNone(
            build_banner_body(b"firma", "avviso", content_type="multipart/signed")
        )
        self.assertIsNone(
            build_banner_body(b"cifrato", "avviso", content_type="multipart/encrypted")
        )
        # ...ma solo se skip_protected e' attivo: con una struttura valida il
        # testo viene riscritto quando il controllo e' disabilitato
        signed = (
            b"--sig\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"testo firmato\r\n"
            b"--sig\r\n"
            b"Content-Type: application/pgp-signature\r\n\r\n"
            b"firma\r\n"
            b"--sig--\r\n"
        )
        self.assertIsNotNone(
            build_banner_body(
                signed,
                "avviso",
                content_type='multipart/signed; boundary="sig"',
                skip_protected=False,
            )
        )

    def test_empty_warning_returns_none(self):
        self.assertIsNone(build_banner_body(b"x", "   "))

    def test_no_text_part_returns_none(self):
        original = (
            b"--mix\r\n"
            b"Content-Type: application/pdf\r\n\r\n"
            b"%PDF-1.4\r\n"
            b"--mix--\r\n"
        )
        self.assertIsNone(
            build_banner_body(
                original,
                "avviso",
                content_type='multipart/mixed; boundary="mix"',
            )
        )

    def test_html_box_disabled_skips_html_parts(self):
        self.assertIsNone(
            build_banner_body(
                b"<p>Ciao</p>", "avviso", content_type="text/html", html_box=False
            )
        )

    def test_html_escapes_special_characters(self):
        rendered = build_html_banner('Mittente <script>alert("x")</script> & altri')
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&amp; altri", rendered)

    def test_preview(self):
        rewrite = build_banner_body(b"originale\n", "riga uno\nriga due")
        lines = preview(rewrite, limit=4)
        self.assertEqual(len(lines), 4)
        self.assertIn("- - -", lines[0])
        self.assertIn("riga uno", lines[1])

    def test_is_protected(self):
        self.assertTrue(is_protected("multipart/signed; boundary=x"))
        self.assertTrue(is_protected("MULTIPART/ENCRYPTED"))
        self.assertFalse(is_protected("multipart/mixed"))
        self.assertFalse(is_protected(None))


if __name__ == "__main__":
    unittest.main()
