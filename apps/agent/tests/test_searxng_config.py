import agent_gemini_live as agl


def test_searxng_url_from_environment(monkeypatch):
    custom_url = "https://search.internal.net"
    monkeypatch.setenv("SEARXNG_URL", custom_url)
    assert agl.get_searxng_url() == custom_url


def test_searxng_url_default_fallback(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert agl.get_searxng_url() == "http://searxng:8080"
