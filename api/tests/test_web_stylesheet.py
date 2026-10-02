"""The server-rendered pages' stylesheet, and what it reaches for.

The pages under ``/web`` share one stylesheet, served as its own route. It
names the same typeface the React bundles are built with, and those font files
come from the registration bundle, which is public. These tests pin the part
that fails silently: a stylesheet whose font files do not resolve renders in
the fallback face and nothing reports it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("jwt")

from registration_bundle import built_dist  # noqa: E402
from test_web_surface import _StubIdp, make_web_app, sign_in, web_client  # noqa: E402

from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV  # noqa: E402
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV  # noqa: E402
from collab_hub_api.web.surface import (  # noqa: E402
    ADMIN_INVITATIONS_PATH,
    ADMIN_PANEL_DOCUMENT,
    LANDING_PATH,
    ORG_INVITATIONS_PATH,
    SIGNED_OUT_PATH,
    STYLE_ASSET_PATH,
    TERMS_PATH,
)

PUBLIC_BASE_URL = "https://web.test"


@pytest.fixture(autouse=True)
def membership_env(monkeypatch):
    monkeypatch.delenv("FRAMES_BEARER_ISSUER", raising=False)
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")


@pytest.fixture
def idp(monkeypatch):
    from collab_hub_api.frames import auth

    endpoint = _StubIdp()
    monkeypatch.setitem(auth.__dict__, "_jwks_clients", {})
    try:
        yield endpoint
    finally:
        endpoint.close()


def build_app(tmp_path, idp):
    return make_web_app(
        tmp_path,
        idp,
        web={"public_base_url": PUBLIC_BASE_URL, "admin_ui_dist": str(built_dist(tmp_path))},
    )


def referenced_urls(stylesheet: str) -> list[str]:
    """Every ``url(...)`` the stylesheet asks the browser to fetch."""

    return [url.strip("'\"") for url in re.findall(r"url\(([^)]+)\)", stylesheet)]


@pytest.mark.asyncio
async def test_every_font_the_stylesheet_names_resolves_without_a_session(tmp_path, idp: _StubIdp):
    """The sign-in and invitation pages are read before anyone has a session,
    so their typeface has to load for an anonymous browser too.

    Resolved the way a browser does, relative to the stylesheet's own address.
    """

    app = build_app(tmp_path, idp)

    async with web_client(app) as client:
        stylesheet = await client.get(STYLE_ASSET_PATH)
        urls = referenced_urls(stylesheet.text)
        fetched = {
            url: await client.get(urljoin(str(stylesheet.url), url)) for url in urls
        }

    assert stylesheet.status_code == 200
    assert any(url.endswith(".woff2") for url in urls), "the stylesheet names no font file"
    for url, response in fetched.items():
        assert response.status_code == 200, url
        assert response.headers["content-type"].startswith("font/"), url


@pytest.mark.asyncio
async def test_the_pages_let_the_browser_load_fonts_from_this_origin(tmp_path, idp: _StubIdp):
    """``font-src 'self'`` on the pages, and only ``'self'``: the typeface is
    served here, never fetched from a font host."""

    app = build_app(tmp_path, idp)

    async with web_client(app) as client:
        page = await client.get(TERMS_PATH)

    csp = page.headers["content-security-policy"]
    assert "font-src 'self'" in csp
    assert "fonts.googleapis.com" not in csp and "fonts.gstatic.com" not in csp


def nav_links(page: str) -> dict[str, str]:
    """The navigation's links, href by the ``aria-current`` value (or "")."""

    nav = re.search(r"<nav[^>]*>(.*?)</nav>", page, re.S)
    assert nav, "the page carries no navigation"
    links = re.findall(r'<a([^>]*)href="([^"]+)"', nav.group(1))
    return {href: (re.search(r'aria-current="([^"]+)"', attrs) or [None, ""])[1] for attrs, href in links}


@pytest.mark.asyncio
async def test_a_signed_in_page_carries_the_panels_shell(tmp_path, idp: _StubIdp):
    """The same frame as the admin panel: a header naming who is signed in
    with a way out, and a navigation down the side that reaches every
    destination of this surface. The page being read is marked as current,
    so the navigation says where you are as well as where you can go.
    """

    app = build_app(tmp_path, idp)

    # The lifespan installs the org store the operator grant below is written to.
    async with app.router.lifespan_context(app), web_client(app) as client:
        # An operator, so the operator page renders rather than refusing.
        app.state.org_store.set_platform_role(idp.sub)
        await sign_in(client, idp)
        landing = (await client.get(LANDING_PATH)).text
        operator = (await client.get(ADMIN_INVITATIONS_PATH)).text

    assert nav_links(landing) == {
        LANDING_PATH: "page",
        ORG_INVITATIONS_PATH: "",
        ADMIN_INVITATIONS_PATH: "",
        ADMIN_PANEL_DOCUMENT: "",
    }
    assert nav_links(operator)[ADMIN_INVITATIONS_PATH] == "page"
    header = re.search(r"<header[^>]*>(.*?)</header>", landing, re.S)
    assert header, "no header"
    assert "alice@example.com" in header.group(1)
    assert 'action="/web/signout"' in header.group(1)


def test_the_shell_honours_a_root_path() -> None:
    """Every link in the frame is app-relative: on a deployment mounted under a
    prefix, a link that escapes the prefix is a 404."""

    from collab_hub_api.web.pages import render_page

    page = render_page(
        title="t",
        body="<h1>t</h1>",
        root_path="/hub",
        identity_label="Alice Example",
        identity_email="alice@example.com",
        csrf_token="csrf",
        current_path=LANDING_PATH,
    )
    links = nav_links(page)
    assert links and all(href.startswith("/hub/") for href in links), links
    assert links[f"/hub{LANDING_PATH}"] == "page"
    assert 'action="/hub/web/signout"' in page


@pytest.mark.asyncio
async def test_an_anonymous_page_has_no_navigation(tmp_path, idp: _StubIdp):
    """Nothing to navigate as: the signed-out notice and the public documents
    stand alone, and offer no shell with a sign-out that would do nothing."""

    app = build_app(tmp_path, idp)

    async with web_client(app) as client:
        signed_out = (await client.get(SIGNED_OUT_PATH)).text
        terms = (await client.get(TERMS_PATH)).text

    for page in (signed_out, terms):
        assert "<nav" not in page
        assert "/web/signout" not in page
