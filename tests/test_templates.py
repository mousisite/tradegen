"""Every template must compile, and every url_for() in them must name a real route.

Jinja compiles a template the first time it is rendered, so a typo in a page you
did not happen to open stays invisible until a user opens it. This walks the
templates directory instead of the route list, which does not depend on anyone
remembering to add a page here.
"""
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: F401  (before web, so it makes no cookie key in data/)
import web

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates")


def test_every_template_compiles():
    env = web.app.jinja_env
    names = sorted(env.list_templates())
    assert names, "no templates found"
    for name in names:
        env.get_template(name)          # raises TemplateSyntaxError on a bad one
    return len(names)


def test_every_url_for_names_a_real_endpoint():
    """url_for('typo') only fails when that branch renders, which may be never."""
    endpoints = set(web.app.view_functions)
    bad = []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        text = open(os.path.join(TEMPLATES, name), encoding="utf-8").read()
        for m in re.finditer(r"url_for\(\s*(.{0,40}?)\s*[,)]", text):
            arg = m.group(1)
            quoted = re.match(r"^(['\"])([A-Za-z_][A-Za-z0-9_]*)\1$", arg)
            if not quoted:
                # Not a plain string literal: either a variable, which we cannot
                # resolve here, or mangled quoting, which we can and must catch.
                bad.append("%s: url_for(%s) is not a quoted endpoint name"
                           % (name, arg))
                continue
            if quoted.group(2) not in endpoints:
                bad.append("%s: url_for('%s') has no such route"
                           % (name, quoted.group(2)))
    assert not bad, "\n".join(bad)


def test_no_stray_jinja_delimiters():
    """An unclosed {{ or {% renders as literal text rather than failing loudly."""
    bad = []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        text = open(os.path.join(TEMPLATES, name), encoding="utf-8").read()
        for open_tok, close_tok in (("{{", "}}"), ("{%", "%}")):
            if text.count(open_tok) != text.count(close_tok):
                bad.append("%s: %d %s but %d %s"
                           % (name, text.count(open_tok), open_tok,
                              text.count(close_tok), close_tok))
    assert not bad, "\n".join(bad)


STYLE = os.path.join(os.path.dirname(TEMPLATES), "static", "style.css")


def _luminance(hex_colour):
    out = []
    for i in (1, 3, 5):
        c = int(hex_colour[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _themes():
    """The colour tokens of the light theme and of the dark one, from the CSS."""
    css = open(STYLE, encoding="utf-8").read()
    token = re.compile(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;")
    light_block = css[css.index(":root {"):css.index("@media (prefers-color-scheme: dark)")]
    dark_start = css.index('[data-theme="light"])', css.index("prefers-color-scheme: dark"))
    dark_block = css[dark_start:css.index("}", dark_start)]
    light = dict(token.findall(light_block))
    dark = dict(light, **dict(token.findall(dark_block)))
    return css, {"light": light, "dark": dark}


def test_text_colours_clear_aa_in_both_themes():
    """Small text needs 4.5:1, on every ground it is set on.

    Ratios were once solved against the card surface only, which left the
    BUY and WAIT chips, the active nav item, the plain-words box and every
    primary button in the dark theme below the line.
    """
    css, themes = _themes()
    grounds = ("--paper", "--surface", "--sunken")
    bad = []
    for name, t in themes.items():
        for ink, washes in (("--ink", ("--accent-wash", "--hold-wash")),
                            ("--ink-2", ("--accent-wash", "--hold-wash")),
                            ("--ink-3", ("--accent-wash", "--hold-wash")),
                            ("--accent-text", ("--accent-wash",)),
                            ("--gain", ("--gain-wash",)),
                            ("--loss", ("--loss-wash",)),
                            ("--hold", ("--hold-wash",))):
            for ground in grounds + washes:
                ratio = contrast(t[ink], t[ground])
                if ratio < 4.5:
                    bad.append("%s: %s %s on %s %s is %.2f"
                               % (name, ink, t[ink], ground, t[ground], ratio))
        # Every primary button carries a white label.
        if contrast("#ffffff", t["--accent-btn"]) < 4.5:
            bad.append("%s: white on --accent-btn %s is %.2f"
                       % (name, t["--accent-btn"],
                          contrast("#ffffff", t["--accent-btn"])))
    for rule in (r"\.btn:hover\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})",
                 r"\.btn\.danger\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})",
                 r"\.btn\.danger:hover\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})"):
        found = re.search(rule, css)
        if not found:
            bad.append("no rule matching %s" % rule)
        elif contrast("#ffffff", found.group(1)) < 4.5:
            bad.append("white on %s is %.2f" % (found.group(1),
                                               contrast("#ffffff", found.group(1))))
    assert not bad, "\n".join(bad)


def test_shared_pieces_are_styled_everywhere():
    """A class styled only inside one page's container renders as plain text
    everywhere else it is used."""
    css = open(STYLE, encoding="utf-8").read()
    for rule in (r"(^|\n)\.notice\s*\{", r"(^|\n)\.count\s*\{", r"(^|\n)\.skip\s*\{",
                 r"(^|\n)p\.lead\s*\{"):
        assert re.search(rule, css), "no top-level rule for %s" % rule
    assert "display: none" not in re.search(
        r"\.drop input\[type=file\]\s*\{[^}]*\}", css).group(0), \
        "the screenshot box is hidden from the keyboard"


def test_every_page_has_a_main_landmark_and_a_skip_link():
    base = open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8").read()
    body = base[base.index("<body>"):]
    assert body.count('<main id="main">') == 1 and body.count("</main>") == 1
    skip = body.find('<a class="skip" href="#main">')
    assert 0 <= skip < body.find("<header"), "the skip link must come first"
    assert body.find('<main id="main">') < body.find("{% block content %}") \
        < body.find("</main>"), "every page's content must sit inside <main>"


def test_strategy_names_read_the_same_everywhere():
    """One name per strategy: 'MACD momentum' here, 'Macd momentum' there."""
    label = web.app.jinja_env.filters["strategy"]
    assert label("macd_momentum") == "MACD momentum"
    assert label("zscore_reversion") == "Z-score reversion"
    assert label("adx_di_cross") == "ADX DI cross"
    bad = []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        text = open(os.path.join(TEMPLATES, name), encoding="utf-8").read()
        for m in re.finditer(r"(\w+\[?'?(name|strategy)'?\]?)\.replace\('_',\s*' '\)\|capitalize",
                             text):
            bad.append("%s: %s" % (name, m.group(0)))
    assert not bad, "\n".join(bad)


def _drawn(name, **values):
    """A template drawn as the site draws it, with whitespace squeezed."""
    from flask import render_template
    with web.app.test_request_context("/"):
        page = render_template(name, **values)
    return re.sub(r"\s+", " ", page)


def _strategy_page(tested, name="rsi_reversion"):
    from bot import public, strategies
    summary = {"tested": tested, "verdict": 0, "kept": 0.05, "se": 0.1, "paid": tested,
               "n": 100, "separate": 60.0, "wins": 40, "side": "", "base": None,
               "short_base": None, "long_share": 0.5, "index": None, "indexes": ()}
    return _drawn("backtest.html", strategy=name, label=public.strategy_label(name),
                  asked=public.strategy_asked(name), asked_of=str,
                  info=strategies.BY_NAME[name],
                  results=[], rare=None, course=False, intraday=False, summary=summary, kin=[],
                  method=web._house_method(), strategy_slug=str, short=str, slug=str)


def test_one_of_a_thing_is_not_called_several():
    one, three = _strategy_page(1), _strategy_page(3)
    assert "prices of 1 stock or coin, after costs" in one
    assert "1 stocks and coins" not in one and "1 popular stocks" not in one, one[:3000]
    assert "On the 1 stock or coin it has enough trades on" in one and "of the 1." not in one
    # Only those it has enough trades on are counted, not every one tested.
    assert "Across the 3 stocks and coins it has enough trades on" in three
    assert "It kept money on 3 of the 3." in three


def test_a_strategy_pages_title_and_description_fit_a_search_result():
    from bot import public, strategies
    for s in strategies.REGISTRY:
        page = _strategy_page(48, s.name)
        title = html.unescape(re.search("<title>(.*?)</title>", page).group(1))
        said = html.unescape(re.search('name="description" content="(.*?)"', page).group(1))
        assert title == "Does %s work? · Orenth" % public.strategy_asked(s.name), title
        assert len(title) < 60 and len(said) < 156, (title, len(said), said)
        assert said.startswith(public.strategy_label(s.name)), said


def _strategies_page(rows=(), symbol=None, regime="all"):
    return _drawn("strategies.html", rows=list(rows), regime=regime, rank_by="gross",
                  symbol=symbol, seen=["AAPL"], minimum=40)


def test_an_empty_choice_does_not_say_nothing_was_ever_measured():
    assert "Nothing measured yet." in _strategies_page()
    for picked in ({"symbol": "AAPL"}, {"regime": "ranging"}):
        page = _strategies_page(**picked)
        assert "Nothing measured yet." not in page
        assert "No strategy has 40 measured setups for that choice yet." in page


def test_a_result_of_nothing_is_not_drawn_as_a_loss():
    row = {"strategy": "rsi_reversion", "family": "reversion", "n": 50, "hit_rate": 0.4,
           "gross_exp": 0.0001, "net_exp": -0.0002, "symbols": 2}
    page = _strategies_page([row])
    cells = re.findall(r'<td data-label="(?:Raw|Kept)" class="r fig ([^"]*)"> ([^<]*)</td>', page)
    assert cells == [("", "0.000R"), ("", "0.000R")], cells


def test_what_orenth_is_stays_the_same_on_every_page():
    """A stock page's words, its call among them, once described the company
    and the app to search engines as well as the stock."""
    import json
    page = _drawn("base.html", page_blurb="Orenth's check on X: AVOID.")
    graph = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',
                                 page).group(1))["@graph"]
    assert 'content="Orenth&#39;s check on X: AVOID."' in page
    assert [g["description"][:30] for g in graph] == ["Free stock and crypto research"] * 2, graph


if __name__ == "__main__":
    n = test_every_template_compiles()
    print("   OK   %d templates compile" % n)
    test_every_url_for_names_a_real_endpoint()
    print("   OK   every url_for names a real route")
    test_no_stray_jinja_delimiters()
    print("   OK   jinja delimiters balanced")
    test_text_colours_clear_aa_in_both_themes()
    print("   OK   text colours clear 4.5:1 on every ground, light and dark")
    test_shared_pieces_are_styled_everywhere()
    print("   OK   notices, counts and the skip link are styled wherever they appear")
    test_every_page_has_a_main_landmark_and_a_skip_link()
    print("   OK   every page has a main landmark and a skip link")
    test_strategy_names_read_the_same_everywhere()
    print("   OK   strategy names come from one label")
    test_one_of_a_thing_is_not_called_several()
    print("   OK   a strategy tested on one stock says so")
    test_a_strategy_pages_title_and_description_fit_a_search_result()
    print("   OK   every strategy page's title and description fit a search result")
    test_an_empty_choice_does_not_say_nothing_was_ever_measured()
    print("   OK   an empty choice on Strategies says it is the choice")
    test_a_result_of_nothing_is_not_drawn_as_a_loss()
    print("   OK   a result that rounds to nothing has no colour")
    test_what_orenth_is_stays_the_same_on_every_page()
    print("   OK   search engines read the same description of Orenth everywhere")
    print("TEMPLATES OK")
