"""Pikabu без сети: разбор общей ленты xmlfeeds.php (RSS 2.0, без рейтинга и признаков поста)."""
import pytest

from scraper import reject_reason
from scraper.pikabu import parse_feed

LONG = "<p><p>" + "Сосед опять припарковался на моём месте. " * 25 + "</p></p><p><p>Но я всё уладил.</p></p>"
FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>pikabu.ru</title>
<item><title>Длинная история</title><link>https://pikabu.ru/story/dlinnaya_istoriya_111222</link>
<guid>https://pikabu.ru/story/dlinnaya_istoriya_111222</guid>
<description><![CDATA[{LONG}]]></description>
<pubDate>Sun, 13 Sep 2026 10:00:00 +0300</pubDate></item>
<item><title>Просто мем</title><link>https://pikabu.ru/story/prosto_mem_333444</link>
<guid>https://pikabu.ru/story/prosto_mem_333444</guid>
<description><![CDATA[<p><img src="x.jpg"></p>]]></description>
<pubDate>Sun, 13 Sep 2026 09:00:00 +0300</pubDate></item>
<item><title>NSFW находка</title><link>https://pikabu.ru/story/nahodka_555666</link>
<guid>https://pikabu.ru/story/nahodka_555666</guid>
<description><![CDATA[{LONG}]]></description>
<pubDate>Sun, 13 Sep 2026 08:00:00 +0300</pubDate></item>
</channel></rss>""".encode()


def test_parse_feed():
    posts = parse_feed(FEED)
    assert [p["id"] for p in posts] == ["pikabu:111222", "pikabu:333444", "pikabu:555666"]
    first = posts[0]
    assert first["source"] == "pikabu"
    assert first["title"] == "Длинная история"
    assert first["text"].endswith("Но я всё уладил.")
    assert first["url"] == "https://pikabu.ru/story/dlinnaya_istoriya_111222"
    assert [p["score"] for p in posts] == [3, 2, 1]              # место в топе — рейтинга в ленте нет
    assert posts[2]["over_18"] and not posts[0]["over_18"]
    assert all(p["is_self"] and not p["stickied"] for p in posts)


@pytest.mark.parametrize("idx, reason", [(0, None), (1, "нет текста"), (2, "nsfw")])
def test_reject_reason(dev_cfg, idx, reason):
    assert reject_reason(parse_feed(FEED)[idx], dev_cfg["scraper"]) == reason


def test_id_fallback_without_trailing_number():
    posts = parse_feed(
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>x</title><link>https://pikabu.ru/story/no-id-here</link>"
        b"<description><![CDATA[text]]></description></item>"
        b"</channel></rss>"
    )
    assert posts[0]["id"] == "pikabu:https://pikabu.ru/story/no-id-here"
