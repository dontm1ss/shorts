"""Парсер форумов без сети: RSS-лента Reddit в её реальном формате (Atom, тело — экранированный HTML)."""
import html

import pytest

from scraper import html_to_text, load_processed, mark_processed, parse_feed, pending_posts, reject_reason, save_post


def entry(post_id, title, body_html, sub="pettyrevenge"):
    content = (f"<!-- SC_OFF --><div class=\"md\">{body_html}</div><!-- SC_ON --> &#32; submitted by "
               f"&#32; <a href=\"https://www.reddit.com/user/x\"> /u/x </a> <br/> <span>[link]</span>")
    return f"""<entry><author><name>/u/x</name></author>
<category term="{sub}" label="r/{sub}"/>
<content type="html">{html.escape(content)}</content>
<id>t3_{post_id}</id>
<link href="https://www.reddit.com/r/{sub}/comments/{post_id}/t/"/>
<published>2026-09-10T10:00:00+00:00</published>
<title>{html.escape(title)}</title></entry>"""


LONG = "<p>" + "My neighbor parked in my spot again and again. " * 25 + "</p><p>Then I got creative &amp; won.</p>"
FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
{entry("aaa111", "Neighbor & the parking spot", LONG)}
{entry("bbb222", "Short one", "<p>Too short.</p>")}
{entry("ccc333", "[NSFW] Something", LONG)}
</feed>""".encode()


def test_parse_feed():
    posts = parse_feed(FEED, "pettyrevenge")
    assert [p["id"] for p in posts] == ["reddit:aaa111", "reddit:bbb222", "reddit:ccc333"]
    first = posts[0]
    assert first["title"] == "Neighbor & the parking spot"
    assert first["text"].endswith("Then I got creative & won.")
    assert "\n\n" in first["text"] and "submitted by" not in first["text"]
    assert first["url"].endswith("/comments/aaa111/t/")
    assert [p["score"] for p in posts] == [3, 2, 1]                  # место в топе
    assert posts[2]["over_18"] and not posts[0]["over_18"]


def test_html_to_text_lists_and_breaks():
    assert html_to_text("<p>a<br/>b</p><ul><li>x</li><li>y</li></ul>") == "a\n\nb\n- x\n- y"


@pytest.mark.parametrize("idx, reason", [(0, None), (1, "длина"), (2, "nsfw")])
def test_reject_reason(dev_cfg, idx, reason):
    assert reject_reason(parse_feed(FEED, "x")[idx], dev_cfg["scraper"]) == reason


def test_nsfw_allowed_when_disabled(dev_cfg):
    dev_cfg["scraper"]["exclude_nsfw"] = False
    assert reject_reason(parse_feed(FEED, "x")[2], dev_cfg["scraper"]) is None


def test_store_and_dedup(dev_cfg, tmp_path):
    dev_cfg["paths"]["processed_forum_ids"] = str(tmp_path / "processed.json")
    dev_cfg["scraper"]["posts_dir"] = str(tmp_path / "posts")
    posts = parse_feed(FEED, "x")
    for p in posts:
        save_post(dev_cfg, p)
    assert (tmp_path / "posts" / "reddit_aaa111.json").exists()
    assert [p.stem for p in pending_posts(dev_cfg)] == ["reddit_aaa111", "reddit_bbb222", "reddit_ccc333"]

    mark_processed(dev_cfg, "reddit:aaa111")
    mark_processed(dev_cfg, "reddit:aaa111")                          # повтор — без дублей
    assert load_processed(dev_cfg) == {"reddit:aaa111"}
    assert "reddit_aaa111" not in [p.stem for p in pending_posts(dev_cfg)]
