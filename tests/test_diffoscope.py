import io
import zipfile
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest

from app.config import settings
from app.services.diffoscope import (
    DiffoscopeCache,
    DiffoscopeReport,
    fetch_diffoscope_report,
    rewrite_asset_paths,
)

ARTIFACT_HOST = "reports.s3.eu-west-1.amazonaws.com"
REPORT_KEY = "diffoscope_result-org.example.App.zip"
REPORT_URL = f"https://{ARTIFACT_HOST}/{REPORT_KEY}"


def create_test_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", b'<html><link href="common.css"></html>')
        zf.writestr("common.css", b"body { color: red; }")
        zf.writestr("icon.png", b"\x89PNG\r\n\x1a\n")  # PNG header bytes
    return buffer.getvalue()


class TestDiffoscopeCache:
    def test_get_nonexistent_key(self):
        cache = DiffoscopeCache(max_size=10)
        assert cache.get("nonexistent") is None

    def test_set_and_get(self):
        cache = DiffoscopeCache(max_size=10)
        report = DiffoscopeReport(html=b"html", css=b"css", icon=b"icon")
        cache.set("key1", report)
        assert cache.get("key1") == report

    def test_lru_eviction(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")
        report3 = DiffoscopeReport(html=b"html3", css=b"css3", icon=b"icon3")

        cache.set("key1", report1)
        cache.set("key2", report2)
        cache.set("key3", report3)

        assert cache.get("key1") is None
        assert cache.get("key2") == report2
        assert cache.get("key3") == report3

    def test_access_updates_order(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")
        report3 = DiffoscopeReport(html=b"html3", css=b"css3", icon=b"icon3")

        cache.set("key1", report1)
        cache.set("key2", report2)
        cache.get("key1")
        cache.set("key3", report3)

        assert cache.get("key1") == report1
        assert cache.get("key2") is None
        assert cache.get("key3") == report3

    def test_update_existing_key(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")

        cache.set("key1", report1)
        cache.set("key1", report2)

        assert cache.get("key1") == report2
        assert len(cache._cache) == 1


class TestFetchDiffoscopeReport:
    @pytest.fixture(autouse=True)
    def configure_host(self, monkeypatch):
        monkeypatch.setattr(settings, "diffoscope_artifact_host", ARTIFACT_HOST)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            f"http://{ARTIFACT_HOST}/{REPORT_KEY}",
            f"https://other.s3.eu-west-1.amazonaws.com/{REPORT_KEY}",
            f"https://{ARTIFACT_HOST}@other.example/{REPORT_KEY}",
            f"https://{ARTIFACT_HOST}:444/{REPORT_KEY}",
            f"https://{ARTIFACT_HOST}/archive/{REPORT_KEY}",
            f"https://{ARTIFACT_HOST}/unrelated.zip",
            REPORT_URL.replace("org.example", "org%2fexample"),
            REPORT_URL + "?versionId=123",
            REPORT_URL + "#part",
        ],
    )
    async def test_rejects_untrusted_stored_url_even_if_cached(self, url):
        cache = DiffoscopeCache()
        cache.set(url, DiffoscopeReport(b"html", b"", b""))
        with (
            patch("app.services.diffoscope._cache", cache),
            patch("httpx2.AsyncClient") as client,
        ):
            assert await fetch_diffoscope_report(url) is None
            client.assert_not_called()

    @pytest.mark.asyncio
    async def test_requires_configured_host(self, monkeypatch):
        monkeypatch.setattr(settings, "diffoscope_artifact_host", None)
        with patch("httpx2.AsyncClient") as client:
            assert await fetch_diffoscope_report(REPORT_URL) is None
            client.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_follow_redirect(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(
                302, headers={"Location": "https://other.example/report.zip"}
            )

        client_cls = httpx.AsyncClient
        with (
            patch("app.services.diffoscope._cache", DiffoscopeCache()),
            patch(
                "httpx2.AsyncClient",
                side_effect=lambda **kwargs: client_cls(
                    transport=httpx.MockTransport(handler), **kwargs
                ),
            ),
        ):
            assert await fetch_diffoscope_report(REPORT_URL) is None
        assert requests == [REPORT_URL]

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        zip_content = create_test_zip()

        mock_response = AsyncMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = lambda: None

        with (
            patch("app.services.diffoscope._cache", DiffoscopeCache()),
            patch("httpx2.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_client.return_value = mock_instance

            result = await fetch_diffoscope_report(REPORT_URL)

            assert result is not None
            assert b"common.css" in result.html
            assert b"color: red" in result.css
            assert result.icon.startswith(b"\x89PNG")

    @pytest.mark.asyncio
    async def test_fetch_uses_cache(self):
        zip_content = create_test_zip()

        mock_response = AsyncMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = lambda: None

        with (
            patch("app.services.diffoscope._cache", DiffoscopeCache()),
            patch("httpx2.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_client.return_value = mock_instance

            result1 = await fetch_diffoscope_report(REPORT_URL)
            result2 = await fetch_diffoscope_report(REPORT_URL)

            assert mock_instance.get.call_count == 1
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_fetch_http_error(self):
        with (
            patch("app.services.diffoscope._cache", DiffoscopeCache()),
            patch("httpx2.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = httpx.HTTPStatusError(
                "Not Found",
                request=AsyncMock(),
                response=AsyncMock(status_code=404),
            )
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_client.return_value = mock_instance

            result = await fetch_diffoscope_report(REPORT_URL)
            assert result is None

    @pytest.mark.asyncio
    async def test_fetch_invalid_zip(self):
        mock_response = AsyncMock()
        mock_response.content = b"not a zip file"
        mock_response.raise_for_status = lambda: None

        with (
            patch("app.services.diffoscope._cache", DiffoscopeCache()),
            patch("httpx2.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_client.return_value = mock_instance

            result = await fetch_diffoscope_report(REPORT_URL)
            assert result is None


class TestRewriteAssetPaths:
    def test_rewrite_css_path(self):
        html = b'<link href="common.css" rel="stylesheet">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'href="/diffoscope/abc123/common.css"' in result

    def test_rewrite_icon_path(self):
        html = b'<img src="icon.png" alt="icon">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'src="/diffoscope/abc123/icon.png"' in result

    def test_rewrite_multiple_paths(self):
        html = b'<link href="common.css"><img src="icon.png">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'href="/diffoscope/abc123/common.css"' in result
        assert b'src="/diffoscope/abc123/icon.png"' in result
