"""Every template must compile, and every url_for() in them must name a real route.

Jinja compiles a template the first time it is rendered, so a typo in a page you
did not happen to open stays invisible until a user opens it. This walks the
templates directory instead of the route list, which does not depend on anyone
remembering to add a page here.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


if __name__ == "__main__":
    n = test_every_template_compiles()
    print("   OK   %d templates compile" % n)
    test_every_url_for_names_a_real_endpoint()
    print("   OK   every url_for names a real route")
    test_no_stray_jinja_delimiters()
    print("   OK   jinja delimiters balanced")
    print("TEMPLATES OK")
