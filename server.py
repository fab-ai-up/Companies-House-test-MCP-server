"""
companies-house-mcp: an MCP server wrapping the UK Companies House
Public Data API (https://developer-specs.company-information.service.gov.uk).

Auth: Companies House uses HTTP Basic auth with your API key as the
username and an empty password. Get a free key at
https://developer.company-information.service.gov.uk/signin

Set it as an environment variable before running:
    export COMPANIES_HOUSE_API_KEY="your-key-here"

Run with:
    .venv/bin/python server.py
"""

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

API_BASE = "https://api.company-information.service.gov.uk"
DOCUMENT_API_BASE = "https://document-api.company-information.service.gov.uk"
API_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY")

# Default location to save downloaded documents. Override by passing an
# explicit output_path to download_document, or set this env var.
DEFAULT_DOWNLOAD_DIR = os.environ.get("CH_DOWNLOAD_DIR", "./downloads")

mcp = FastMCP("companies-house")


def _pad_company_number(number: str) -> str:
    """Companies House company numbers are 8 chars; older ones need
    zero-padding (e.g. "123456" -> "00123456"). Leaves prefixed formats
    like SC/NI/OC alone if already 8 chars, and numeric-only otherwise.
    """
    number = number.strip().upper()
    if number.isdigit():
        return number.zfill(8)
    return number


async def _get_document_content(document_id: str, accept: str) -> tuple[bytes, str, dict] | dict:
    """Fetch raw document bytes from the Document API. Returns
    (content_bytes, content_type, headers) on success, or an error dict.
    """
    if not API_KEY:
        return {
            "error": "COMPANIES_HOUSE_API_KEY environment variable is not set. "
            "Get a free key at https://developer.company-information.service.gov.uk/signin "
            "and export it before running this server."
        }

    async with httpx.AsyncClient(
        timeout=60.0, auth=(API_KEY, ""), follow_redirects=True
    ) as client:
        response = await client.get(
            f"{DOCUMENT_API_BASE}/document/{document_id}/content",
            headers={"Accept": accept},
        )

        if response.status_code == 404:
            return {"error": f"Document not found: {document_id}"}
        if response.status_code == 401:
            return {"error": "Unauthorized — check that your API key is valid."}
        if response.status_code == 429:
            return {"error": "Rate limited (600 requests / 5 minutes across all endpoints). Try again shortly."}

        response.raise_for_status()
        return response.content, response.headers.get("content-type", accept), dict(response.headers)


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not API_KEY:
        return {
            "error": "COMPANIES_HOUSE_API_KEY environment variable is not set. "
            "Get a free key at https://developer.company-information.service.gov.uk/signin "
            "and export it before running this server."
        }

    async with httpx.AsyncClient(timeout=20.0, auth=(API_KEY, "")) as client:
        response = await client.get(f"{API_BASE}{path}", params=params or {})

        if response.status_code == 404:
            return {"error": f"Not found: {path}"}
        if response.status_code == 401:
            return {"error": "Unauthorized — check that your API key is valid."}
        if response.status_code == 429:
            return {"error": "Rate limited (600 requests / 5 minutes across all endpoints). Try again shortly."}

        response.raise_for_status()

        if not response.content:
            return {"note": "Empty response (endpoint returned no content)."}
        return response.json()


# ---------------------------------------------------------------------------
# Company profile & registered office
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_company_profile(company_number: str) -> dict:
    """Get full company profile: name, status, incorporation date, SIC codes,
    accounts/confirmation statement due dates, registered office, etc.

    Args:
        company_number: Companies House company number, e.g. "15246704".
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}")


@mcp.tool()
async def get_registered_office_address(company_number: str) -> dict:
    """Get a company's registered office address.

    Args:
        company_number: Companies House company number.
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/registered-office-address")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_all(query: str, items_per_page: int = 20, start_index: int = 0) -> dict:
    """Search across companies, officers, and disqualified officers at once.

    Args:
        query: Free-text search term (e.g. a company or person name).
        items_per_page: Max results to return (default 20).
        start_index: Pagination offset (default 0).
    """
    return await _get(
        "/search",
        {"q": query, "items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def search_companies(query: str, items_per_page: int = 20, start_index: int = 0) -> dict:
    """Search for companies by name.

    Args:
        query: Company name or partial name to search for.
        items_per_page: Max results to return (default 20).
        start_index: Pagination offset (default 0).
    """
    return await _get(
        "/search/companies",
        {"q": query, "items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def search_companies_alphabetically(query: str, search_above: str = "", search_below: str = "", size: int = 20) -> dict:
    """Search for companies alphabetically near a given name — useful for
    browsing companies with similar names.

    Args:
        query: Company name to search near.
        search_above: Optional cursor to page upward from.
        search_below: Optional cursor to page downward from.
        size: Number of results to return (default 20).
    """
    params: dict[str, Any] = {"q": query, "size": size}
    if search_above:
        params["search_above"] = search_above
    if search_below:
        params["search_below"] = search_below
    return await _get("/alphabetical-search/companies", params)


@mcp.tool()
async def search_dissolved_companies(query: str, search_type: str = "alphabetical") -> dict:
    """Search for dissolved (no longer active) companies by name.

    Args:
        query: Company name to search for.
        search_type: "alphabetical", "best-match", or "previous-name-dissolved".
    """
    return await _get("/dissolved-search/companies", {"q": query, "search_type": search_type})


@mcp.tool()
async def advanced_company_search(
    company_name: str = "",
    company_status: str = "",
    sic_codes: str = "",
    incorporated_from: str = "",
    incorporated_to: str = "",
    location: str = "",
    items_per_page: int = 20,
    start_index: int = 0,
) -> dict:
    """Advanced company search with structured filters (status, SIC code,
    incorporation date range, location), instead of free-text only.

    Args:
        company_name: Filter by company name (optional).
        company_status: e.g. "active", "dissolved", "liquidation" (optional).
        sic_codes: Comma-separated SIC codes to filter by (optional).
        incorporated_from: ISO date, earliest incorporation date (optional).
        incorporated_to: ISO date, latest incorporation date (optional).
        location: Location text filter (optional).
        items_per_page: Max results to return (default 20).
        start_index: Pagination offset (default 0).
    """
    params: dict[str, Any] = {"size": items_per_page, "start_index": start_index}
    if company_name:
        params["company_name_includes"] = company_name
    if company_status:
        params["company_status"] = company_status
    if sic_codes:
        params["sic_codes"] = sic_codes
    if incorporated_from:
        params["incorporated_from"] = incorporated_from
    if incorporated_to:
        params["incorporated_to"] = incorporated_to
    if location:
        params["location"] = location
    return await _get("/advanced-search/companies", params)


@mcp.tool()
async def search_officers(query: str, items_per_page: int = 20, start_index: int = 0) -> dict:
    """Search for company officers (directors, secretaries) by name.

    Args:
        query: Officer name or partial name to search for.
        items_per_page: Max results to return (default 20).
        start_index: Pagination offset (default 0).
    """
    return await _get(
        "/search/officers",
        {"q": query, "items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def search_disqualified_officers(query: str, items_per_page: int = 20, start_index: int = 0) -> dict:
    """Search for disqualified company officers by name.

    Args:
        query: Officer name to search for.
        items_per_page: Max results to return (default 20).
        start_index: Pagination offset (default 0).
    """
    return await _get(
        "/search/disqualified-officers",
        {"q": query, "items_per_page": items_per_page, "start_index": start_index},
    )


# ---------------------------------------------------------------------------
# Officers & appointments
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_company_officers(company_number: str, items_per_page: int = 35, start_index: int = 0) -> dict:
    """List a company's officers (directors, secretaries), current and past.

    Args:
        company_number: Companies House company number.
        items_per_page: Max results to return (default 35).
        start_index: Pagination offset (default 0).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/officers",
        {"items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def get_officer_appointment(company_number: str, appointment_id: str) -> dict:
    """Get details of a specific officer appointment at a company.

    Args:
        company_number: Companies House company number.
        appointment_id: Appointment ID (from get_company_officers results).
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/appointments/{appointment_id}")


@mcp.tool()
async def get_officer_appointments(officer_id: str, items_per_page: int = 35, start_index: int = 0) -> dict:
    """List all company appointments (directorships) held by a specific
    officer across every company, identified by their Companies House
    officer ID (found in search_officers or get_company_officers results).

    Args:
        officer_id: Companies House officer ID.
        items_per_page: Max results to return (default 35).
        start_index: Pagination offset (default 0).
    """
    return await _get(
        f"/officers/{officer_id}/appointments",
        {"items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def get_natural_officer_disqualifications(officer_id: str) -> dict:
    """Get disqualification details for a natural-person (individual) officer.

    Args:
        officer_id: Companies House officer ID.
    """
    return await _get(f"/disqualified-officers/natural/{officer_id}")


@mcp.tool()
async def get_corporate_officer_disqualifications(officer_id: str) -> dict:
    """Get disqualification details for a corporate officer.

    Args:
        officer_id: Companies House officer ID.
    """
    return await _get(f"/disqualified-officers/corporate/{officer_id}")


# ---------------------------------------------------------------------------
# Filing history
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_filing_history(
    company_number: str,
    category: str = "",
    items_per_page: int = 25,
    start_index: int = 0,
) -> dict:
    """List a company's filing history (accounts, confirmation statements,
    officer changes, incorporation, etc). Use get_filing_history_item for
    full detail (and document links) on a specific filing.

    Args:
        company_number: Companies House company number.
        category: Optional filter, e.g. "accounts", "confirmation-statement",
            "officers", "capital", "charges", "incorporation".
        items_per_page: Max results to return (default 25).
        start_index: Pagination offset (default 0).
    """
    number = _pad_company_number(company_number)
    params: dict[str, Any] = {"items_per_page": items_per_page, "start_index": start_index}
    if category:
        params["category"] = category
    return await _get(f"/company/{number}/filing-history", params)


@mcp.tool()
async def get_filing_history_item(company_number: str, transaction_id: str) -> dict:
    """Get full detail for a single filing history item, including a link
    to the underlying document (PDF/XBRL), by transaction ID. The document
    identifier you need for get_document_metadata / download_document is
    embedded in this result's links.document_metadata field — it's the
    last path segment of that URL.

    Args:
        company_number: Companies House company number.
        transaction_id: Filing transaction ID (from get_filing_history results).
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/filing-history/{transaction_id}")


@mcp.tool()
async def get_document_metadata(document_id: str) -> dict:
    """Get metadata for a filed document — page count, available formats
    (e.g. application/pdf, application/xhtml+xml), and content length —
    without downloading the document itself.

    Args:
        document_id: Document ID. Extract this from a filing history item's
            links.document_metadata URL (the last path segment), obtained
            via get_filing_history_item.
    """
    if not API_KEY:
        return {
            "error": "COMPANIES_HOUSE_API_KEY environment variable is not set. "
            "Get a free key at https://developer.company-information.service.gov.uk/signin "
            "and export it before running this server."
        }
    async with httpx.AsyncClient(timeout=20.0, auth=(API_KEY, "")) as client:
        response = await client.get(f"{DOCUMENT_API_BASE}/document/{document_id}")
        if response.status_code == 404:
            return {"error": f"Document not found: {document_id}"}
        if response.status_code == 401:
            return {"error": "Unauthorized — check that your API key is valid."}
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def download_document(
    document_id: str,
    output_path: str = "",
    format: str = "pdf",
) -> dict:
    """Download a filed document (e.g. a set of accounts) to disk and
    return the local file path. Use get_filing_history_item first to find
    a document_id, and optionally get_document_metadata to check available
    formats and size before downloading.

    Args:
        document_id: Document ID (see get_document_metadata for how to obtain it).
        output_path: Local file path to save to. If empty, saves to
            "./downloads/{document_id}.{ext}" (directory auto-created).
        format: One of "pdf", "xhtml", or "json" (json returns structured
            iXBRL data where available, not a rendered document).
    """
    accept_map = {
        "pdf": "application/pdf",
        "xhtml": "application/xhtml+xml",
        "json": "application/json",
    }
    ext_map = {"pdf": "pdf", "xhtml": "html", "json": "json"}

    if format not in accept_map:
        return {"error": f"Unsupported format '{format}'. Use one of: {list(accept_map)}"}

    result = await _get_document_content(document_id, accept_map[format])
    if isinstance(result, dict):
        return result  # error dict

    content, content_type, _headers = result

    if not output_path:
        os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)
        output_path = os.path.join(DEFAULT_DOWNLOAD_DIR, f"{document_id}.{ext_map[format]}")
    else:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    mode = "wb" if format in ("pdf",) else "w"
    if mode == "wb":
        with open(output_path, "wb") as f:
            f.write(content)
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content.decode("utf-8", errors="replace"))

    return {
        "saved_to": os.path.abspath(output_path),
        "content_type": content_type,
        "size_bytes": len(content),
    }


@mcp.tool()
async def read_document_pages(
    document_id: str,
    max_pages: int = 1,
    start_page: int = 1,
    dpi: int = 100,
) -> list:
    """Fetch a filed PDF document and return its pages as images, so the
    model can read them directly in this conversation (rather than just
    saving the file to disk). Use this instead of download_document when
    you want to actually see/analyze the document's content — e.g. reading
    figures from a set of accounts.

    IMPORTANT: MCP tool results have a hard 1MB size limit in some clients
    (e.g. Claude Desktop), and this applies to the whole result — base64
    encoding alone adds ~33% overhead on top of raw image bytes. To stay
    safely under that, this tool defaults to ONE page per call. If a
    rendered page would still be too large at the requested dpi, it is
    automatically re-rendered at progressively lower resolution until it
    fits, and the response text tells you what resolution was actually used.

    For a multi-page document, call this repeatedly with increasing
    start_page (check get_document_metadata first for total page count).

    Args:
        document_id: Document ID (see get_document_metadata for how to obtain it).
        max_pages: Maximum number of pages to render and return (default 1;
            raise cautiously — each extra page adds ~200-400KB, close to
            the 1MB total budget).
        start_page: 1-indexed page number to start from (default 1).
        dpi: Preferred render resolution (default 100). Automatically
            reduced per-page if needed to fit the size budget.
    """
    if fitz is None:
        return [{"error": "PyMuPDF is not installed. Run: pip install pymupdf"}]

    result = await _get_document_content(document_id, "application/pdf")
    if isinstance(result, dict):
        return [result]  # error dict

    content, _content_type, _headers = result

    try:
        pdf = fitz.open(stream=content, filetype="pdf")
    except Exception as e:
        return [{"error": f"Could not open document as PDF: {e}"}]

    total_pages = pdf.page_count
    start_idx = max(0, start_page - 1)
    end_idx = min(total_pages, start_idx + max_pages)

    if start_idx >= total_pages:
        pdf.close()
        return [{"error": f"start_page {start_page} is beyond document length ({total_pages} pages)."}]

    # Base64 adds ~33% overhead; leave headroom below the 1MB wire limit,
    # and split remaining budget evenly across however many pages we render.
    TOTAL_BYTE_BUDGET = int(0.7 * 1024 * 1024)  # ~0.7MB raw -> ~0.93MB base64
    n_pages_requested = end_idx - start_idx
    per_page_budget = TOTAL_BYTE_BUDGET // max(1, n_pages_requested)

    images = []
    warnings = []

    for page_num in range(start_idx, end_idx):
        page = pdf.load_page(page_num)
        current_dpi = dpi
        png_bytes = b""

        # Try requested dpi, then step down until it fits the per-page budget.
        for attempt_dpi in [current_dpi, 75, 50, 36]:
            zoom = attempt_dpi / 72.0
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            png_bytes = pixmap.tobytes("png")
            if len(png_bytes) <= per_page_budget:
                current_dpi = attempt_dpi
                break
        else:
            warnings.append(
                f"Page {page_num + 1}: could not shrink under budget even at "
                f"36 dpi ({len(png_bytes)/1024:.0f}KB) — may still fail to render."
            )

        if current_dpi != dpi:
            warnings.append(
                f"Page {page_num + 1} rendered at {current_dpi} dpi instead of "
                f"{dpi} to fit the size limit."
            )

        images.append(Image(data=png_bytes, format="png"))

    pdf.close()

    note = f"Rendered pages {start_idx + 1}-{end_idx} of {total_pages} total."
    if end_idx < total_pages:
        note += f" Call again with start_page={end_idx + 1} to continue."
    if warnings:
        note += " " + " ".join(warnings)

    return [note] + images


# ---------------------------------------------------------------------------
# Charges, insolvency, exemptions, registers, UK establishments
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_charges(company_number: str, items_per_page: int = 25, start_index: int = 0) -> dict:
    """List mortgages/charges registered against a company's assets.

    Args:
        company_number: Companies House company number.
        items_per_page: Max results to return (default 25).
        start_index: Pagination offset (default 0).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/charges",
        {"items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def get_charge(company_number: str, charge_id: str) -> dict:
    """Get details of a specific charge/mortgage against a company.

    Args:
        company_number: Companies House company number.
        charge_id: Charge ID (from get_charges results).
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/charges/{charge_id}")


@mcp.tool()
async def get_insolvency(company_number: str) -> dict:
    """Get insolvency information (administration, liquidation, etc) for a company.

    Args:
        company_number: Companies House company number.
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/insolvency")


@mcp.tool()
async def get_exemptions(company_number: str) -> dict:
    """Get a company's exemptions from certain disclosure requirements
    (e.g. PSC register exemptions).

    Args:
        company_number: Companies House company number.
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/exemptions")


@mcp.tool()
async def get_registers(company_number: str) -> dict:
    """Get information on where a company keeps its statutory registers
    (e.g. at Companies House vs. a third-party address).

    Args:
        company_number: Companies House company number.
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/registers")


@mcp.tool()
async def get_uk_establishments(company_number: str) -> dict:
    """List UK establishments (branches) registered for an overseas company.

    Args:
        company_number: Companies House company number.
    """
    number = _pad_company_number(company_number)
    return await _get(f"/company/{number}/uk-establishments")


# ---------------------------------------------------------------------------
# Persons with significant control (PSC)
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_psc(company_number: str, items_per_page: int = 25, start_index: int = 0) -> dict:
    """List persons with significant control (PSCs) — the people or
    entities who ultimately own or control a company.

    Args:
        company_number: Companies House company number.
        items_per_page: Max results to return (default 25).
        start_index: Pagination offset (default 0).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control",
        {"items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def list_psc_statements(company_number: str, items_per_page: int = 25, start_index: int = 0) -> dict:
    """List PSC statements for a company (e.g. statements that no PSC
    exists, or that one hasn't yet been identified).

    Args:
        company_number: Companies House company number.
        items_per_page: Max results to return (default 25).
        start_index: Pagination offset (default 0).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control-statements",
        {"items_per_page": items_per_page, "start_index": start_index},
    )


@mcp.tool()
async def get_psc_individual(company_number: str, notification_id: str) -> dict:
    """Get details of an individual person with significant control.

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID (from list_psc results).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/individual/{notification_id}"
    )


@mcp.tool()
async def get_psc_corporate_entity(company_number: str, notification_id: str) -> dict:
    """Get details of a corporate entity with significant control.

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID (from list_psc results).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/corporate-entity/{notification_id}"
    )


@mcp.tool()
async def get_psc_legal_person(company_number: str, notification_id: str) -> dict:
    """Get details of a legal person with significant control.

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID (from list_psc results).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/legal-person/{notification_id}"
    )


@mcp.tool()
async def get_psc_individual_beneficial_owner(company_number: str, notification_id: str) -> dict:
    """Get details of an individual beneficial owner (used for certain
    overseas-entity registrations).

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/individual-beneficial-owner/{notification_id}"
    )


@mcp.tool()
async def get_psc_corporate_entity_beneficial_owner(company_number: str, notification_id: str) -> dict:
    """Get details of a corporate entity beneficial owner.

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/corporate-entity-beneficial-owner/{notification_id}"
    )


@mcp.tool()
async def get_psc_legal_person_beneficial_owner(company_number: str, notification_id: str) -> dict:
    """Get details of a legal person beneficial owner.

    Args:
        company_number: Companies House company number.
        notification_id: PSC notification ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/legal-person-beneficial-owner/{notification_id}"
    )


@mcp.tool()
async def get_psc_super_secure(company_number: str, super_secure_id: str) -> dict:
    """Get a 'super secure' PSC record (identity protected for safety reasons).

    Args:
        company_number: Companies House company number.
        super_secure_id: Super-secure PSC ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/super-secure/{super_secure_id}"
    )


@mcp.tool()
async def get_psc_super_secure_beneficial_owner(company_number: str, super_secure_id: str) -> dict:
    """Get a 'super secure' beneficial owner record (identity protected).

    Args:
        company_number: Companies House company number.
        super_secure_id: Super-secure beneficial owner ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/super-secure-beneficial-owner/{super_secure_id}"
    )


@mcp.tool()
async def get_psc_statement(company_number: str, statement_id: str) -> dict:
    """Get details of a specific PSC statement.

    Args:
        company_number: Companies House company number.
        statement_id: PSC statement ID (from list_psc_statements results).
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control-statements/{statement_id}"
    )


@mcp.tool()
async def list_psc_notifications(company_number: str, psc_id: str) -> dict:
    """List notification history for a specific PSC (changes over time).

    Args:
        company_number: Companies House company number.
        psc_id: PSC ID.
    """
    number = _pad_company_number(company_number)
    return await _get(
        f"/company/{number}/persons-with-significant-control/{psc_id}/notifications"
    )


if __name__ == "__main__":
    # For local desktop use (Claude Desktop launching via subprocess), swap to:
    #     mcp.run(transport="stdio")
    # For remote hosting (Railway, Fly, etc.), use Streamable HTTP:
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http")
