import unittest

from bili_agent.library import cover_url


class CoverUrlTests(unittest.TestCase):
    def test_original_cdn_cover_can_be_loaded_over_https(self):
        target = 'https://i0.hdslb.com/bfs/archive/cover.jpg?size=original'
        for source in (target, target.replace('https:', 'http:'),
                       target.replace('https:', ''), target.replace('.com/', '.com:80/').replace('https:', 'http:')):
            with self.subTest(source=source):
                self.assertEqual(cover_url(source), target)

    def test_upgrade_does_not_accept_untrusted_or_credentialed_urls(self):
        for source in ('http://i0.hdslb.com.evil.test/cover.jpg', 'http://evilhdslb.com/cover.jpg',
                       'http://user:secret@i0.hdslb.com/cover.jpg', 'http://i0.hdslb.com:8080/cover.jpg',
                       'https://i0.hdslb.com:80/cover.jpg', 'http://i0.hdslb.com:bad/cover.jpg',
                       'file:///cover.jpg', None):
            with self.subTest(source=source):
                self.assertEqual(cover_url(source), '')
