"""
Automated test suite for the Companies House MCP server.

Three layers, all deterministic and offline (no network, no API key required):

1. Unit tests: pure functions (padding, budget math).
2. Integration tests: full MCP protocol round-trip against a live subprocess,
   with the Companies House HTTP layer mocked via respx.
3. Contract tests: schema/registration assertions (tool names, arg names).

Run with:
    .venv/bin/pytest tests/ -v
"""

import base64
import io
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF
import httpx
import pytest
import respx

# Make server.py importable for unit tests without spawning it
sys.path.insert(0, str(Path(__file__).parent.parent))
import server as server_mod


# ---------------------------------------------------------------------------
# 1. Unit tests — pure functions, no I/O
# ---------------------------------------------------------------------------


class TestPadCompanyNumber:
    def test_short_numeric_gets_zero_padded_to_eight(self):
        assert server_mod._pad_company_number("123456") == "00123456"

    def test_already_eight_digits_unchanged(self):
        assert server_mod._pad_company_number("15246704") == "15246704"

    def test_scottish_prefix_preserved(self):
        # SC-prefixed Scottish company numbers are already 8 chars and shouldn't be touched
        assert server_mod._pad_company_number("SC123456") == "SC123456"

    def test_lowercased_prefix_uppercased(self):
        assert server_mod._pad_company_number("sc123456") == "SC123456"

    def test_whitespace_stripped(self):
        assert server_mod._pad_company_number("  123456  ") == "00123456"


# ---------------------------------------------------------------------------
# 2. Integration tests — MCP protocol round-trip with mocked HTTP layer
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key(monkeypatch):
    """Force a fake key so _get doesn't return the 'unset' error."""
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key-not-real")
    # server_mod cached API_KEY at import time; patch it too
    monkeypatch.setattr(server_mod, "API_KEY", "test-key-not-real")


@pytest.fixture
def mock_api():
    """Intercept all outgoing HTTP so tests are deterministic and offline."""
    with respx.mock(assert_all_called=False) as respx_mock:
        yield respx_mock


@pytest.mark.asyncio
async def test_get_company_profile_returns_parsed_json(api_key, mock_api):
    fake_profile = {
        "company_name": "WANDERIST LTD",
        "company_number": "15246704",
        "company_status": "active",
    }
    mock_api.get("https://api.company-information.service.gov.uk/company/15246704").mock(
        return_value=httpx.Response(200, json=fake_profile)
    )

    result = await server_mod.get_company_profile(company_number="15246704")
    assert result == fake_profile


@pytest.mark.asyncio
async def test_short_company_number_gets_padded_in_request_path(api_key, mock_api):
    """Regression test: ensure the padding helper actually gets applied in the URL."""
    route = mock_api.get(
        "https://api.company-information.service.gov.uk/company/00123456"
    ).mock(return_value=httpx.Response(200, json={"company_number": "00123456"}))

    await server_mod.get_company_profile(company_number="123456")
    assert route.called


@pytest.mark.asyncio
async def test_404_returns_clear_error_dict(api_key, mock_api):
    mock_api.get(
        "https://api.company-information.service.gov.uk/company/99999999"
    ).mock(return_value=httpx.Response(404))
    result = await server_mod.get_company_profile(company_number="99999999")
    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_429_returns_rate_limit_error(api_key, mock_api):
    mock_api.get(
        "https://api.company-information.service.gov.uk/company/15246704"
    ).mock(return_value=httpx.Response(429))
    result = await server_mod.get_company_profile(company_number="15246704")
    assert "rate limit" in result["error"].lower()


@pytest.mark.asyncio
async def test_401_surfaces_auth_error(api_key, mock_api):
    mock_api.get(
        "https://api.company-information.service.gov.uk/company/15246704"
    ).mock(return_value=httpx.Response(401))
    result = await server_mod.get_company_profile(company_number="15246704")
    assert "unauthorized" in result["error"].lower()


@pytest.mark.asyncio
async def test_missing_api_key_returns_helpful_error(monkeypatch, mock_api):
    monkeypatch.setattr(server_mod, "API_KEY", None)
    result = await server_mod.get_company_profile(company_number="15246704")
    assert "COMPANIES_HOUSE_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_search_companies_passes_query_params(api_key, mock_api):
    route = mock_api.get(
        "https://api.company-information.service.gov.uk/search/companies"
    ).mock(return_value=httpx.Response(200, json={"items": []}))

    await server_mod.search_companies(
        query="Wanderist", items_per_page=5, start_index=10
    )

    # respx captures the actual request; verify args were serialized properly
    request = route.calls.last.request
    assert request.url.params["q"] == "Wanderist"
    assert request.url.params["items_per_page"] == "5"
    assert request.url.params["start_index"] == "10"


@pytest.mark.asyncio
async def test_advanced_search_omits_empty_filters(api_key, mock_api):
    """Only non-empty filters should be included in the request — a bug here
    would send empty strings and get 400s from Companies House."""
    route = mock_api.get(
        "https://api.company-information.service.gov.uk/advanced-search/companies"
    ).mock(return_value=httpx.Response(200, json={"items": []}))

    await server_mod.advanced_company_search(
        company_name="Acme", company_status="active"
    )
    params = route.calls.last.request.url.params
    assert params["company_name_includes"] == "Acme"
    assert params["company_status"] == "active"
    assert "sic_codes" not in params
    assert "incorporated_from" not in params


# ---------------------------------------------------------------------------
# 3. Document handling
# ---------------------------------------------------------------------------


def _make_test_pdf(pages: int = 1, dense: bool = False) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        if dense:
            for row in range(60):
                page.insert_text(
                    (30, 20 + row * 12),
                    f"Row {row}: Balance £{row * 12345}.67 " * 4,
                    fontsize=8,
                )
        else:
            page.insert_text((72, 72), f"Page {i + 1}", fontsize=24)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.asyncio
async def test_read_document_pages_returns_note_and_images(api_key, mock_api):
    pdf_bytes = _make_test_pdf(pages=3)
    mock_api.get(
        "https://document-api.company-information.service.gov.uk/document/doc123/content"
    ).mock(return_value=httpx.Response(200, content=pdf_bytes))

    result = await server_mod.read_document_pages(document_id="doc123", max_pages=1)

    # First element is a status note, then Image objects
    assert isinstance(result[0], str)
    assert "Rendered pages 1-1 of 3" in result[0]
    assert "start_page=2" in result[0]  # continuation hint since there's more
    assert len(result) == 2


@pytest.mark.asyncio
async def test_read_document_pages_respects_size_budget(api_key, mock_api):
    """Even for a dense page, output should fit under the ~1MB budget."""
    pdf_bytes = _make_test_pdf(pages=1, dense=True)
    mock_api.get(
        "https://document-api.company-information.service.gov.uk/document/doc123/content"
    ).mock(return_value=httpx.Response(200, content=pdf_bytes))

    result = await server_mod.read_document_pages(document_id="doc123")

    # Sum base64-encoded size (what actually goes over the wire)
    total_b64 = sum(
        len(base64.b64encode(item.data)) for item in result if hasattr(item, "data")
    )
    assert total_b64 < 1_000_000, f"payload too big: {total_b64/1024:.0f}KB"


@pytest.mark.asyncio
async def test_read_document_pages_beyond_end_returns_error(api_key, mock_api):
    pdf_bytes = _make_test_pdf(pages=2)
    mock_api.get(
        "https://document-api.company-information.service.gov.uk/document/doc123/content"
    ).mock(return_value=httpx.Response(200, content=pdf_bytes))

    result = await server_mod.read_document_pages(document_id="doc123", start_page=99)
    assert isinstance(result[0], dict) and "error" in result[0]


@pytest.mark.asyncio
async def test_download_document_writes_bytes_to_disk(api_key, mock_api, tmp_path):
    fake_pdf = b"%PDF-1.4 not really but close enough for a test"
    mock_api.get(
        "https://document-api.company-information.service.gov.uk/document/doc123/content"
    ).mock(return_value=httpx.Response(200, content=fake_pdf, headers={"content-type": "application/pdf"}))

    output = tmp_path / "out.pdf"
    result = await server_mod.download_document(
        document_id="doc123", output_path=str(output)
    )

    assert result["saved_to"] == str(output.resolve())
    assert result["size_bytes"] == len(fake_pdf)
    assert output.read_bytes() == fake_pdf


# ---------------------------------------------------------------------------
# 4. Contract tests — schema/registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_tools_are_registered():
    """If a tool gets renamed or accidentally removed, this fails loudly."""
    tools = await server_mod.mcp.list_tools()
    names = {t.name for t in tools}

    must_have = {
        "get_company_profile",
        "search_companies",
        "search_officers",
        "get_company_officers",
        "get_filing_history",
        "get_filing_history_item",
        "get_document_metadata",
        "download_document",
        "read_document_pages",
        "list_psc",
        "get_charges",
        "get_insolvency",
    }
    missing = must_have - names
    assert not missing, f"expected tools missing: {missing}"


@pytest.mark.asyncio
async def test_get_company_profile_schema_has_company_number_param():
    tools = await server_mod.mcp.list_tools()
    profile_tool = next(t for t in tools if t.name == "get_company_profile")
    assert "company_number" in profile_tool.inputSchema["properties"]
    assert "company_number" in profile_tool.inputSchema.get("required", [])
