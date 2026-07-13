# companies-house-mcp

MCP server exposing the full UK Companies House Public Data API (~34 tools
covering company profiles, search, officers, filing history, charges,
insolvency, and persons with significant control).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install "mcp>=1.27,<2" httpx pymupdf
```

Get a free API key at https://developer.company-information.service.gov.uk/signin
(register an application in the Developer Hub, then copy the API key).

Export it as an environment variable — never hardcode it in the script:

```bash
export COMPANIES_HOUSE_API_KEY="your-key-here"
```

## Test it standalone

```bash
python test_client.py
```

Lists all tools, then calls `get_company_profile` and `search_companies`
against Wanderist Ltd (company number 15246704) as a live example.

## Tool groups

- **Profile**: `get_company_profile`, `get_registered_office_address`
- **Search**: `search_all`, `search_companies`, `search_companies_alphabetically`,
  `search_dissolved_companies`, `advanced_company_search`, `search_officers`,
  `search_disqualified_officers`
- **Officers**: `get_company_officers`, `get_officer_appointment`,
  `get_officer_appointments`, `get_natural_officer_disqualifications`,
  `get_corporate_officer_disqualifications`
- **Filing history & documents**: `get_filing_history`, `get_filing_history_item`,
  `get_document_metadata`, `download_document`, `read_document_pages`
- **Charges/insolvency/misc**: `get_charges`, `get_charge`, `get_insolvency`,
  `get_exemptions`, `get_registers`, `get_uk_establishments`
- **Persons with significant control (PSC)**: `list_psc`, `list_psc_statements`,
  `get_psc_individual`, `get_psc_corporate_entity`, `get_psc_legal_person`,
  and beneficial-owner / super-secure / statement / notification variants

## Downloading a filing document (e.g. a set of accounts)

Companies House splits this across two APIs: the main Public Data API gives you
filing metadata, and a separate Document API serves the actual file bytes.
The flow:

1. `get_filing_history(company_number, category="accounts")` — list filings,
   note the `transaction_id` of the one you want.
2. `get_filing_history_item(company_number, transaction_id)` — get the full
   filing record. Its `links.document_metadata` field is a URL; the last path
   segment is the `document_id`.
3. `get_document_metadata(document_id)` — (optional) check available formats
   and file size before downloading.
4. `download_document(document_id, format="pdf")` — downloads the file to
   `./downloads/{document_id}.pdf` (or a path you specify) and returns the
   local path, content type, and size.

Supported `format` values: `"pdf"`, `"xhtml"`, `"json"` (structured iXBRL data,
where available — not all filings have it).

## Letting the AI read a document directly (not just download it)

Use `read_document_pages(document_id)` instead of `download_document` when you
want the model itself to see the document's contents in this conversation —
e.g. reading figures out of a set of accounts.

It renders each PDF page to a PNG image and returns those as image content
blocks, rather than a raw PDF blob. This is deliberate: MCP clients (including
Claude Desktop / claude.ai) reliably display image content to the model, but
several currently reject raw non-image binary blobs (`application/pdf`
`EmbeddedResource`s) even though the protocol technically supports them. Images
work everywhere; PDF blobs don't, yet.

Pages are capped at `max_pages` per call (default 10) to avoid overwhelming
context on long filings — check `get_document_metadata` first for the total
page count, and pass a higher `start_page` to continue reading further pages.

Requires `pymupdf` (included in the setup command above).

### About the 1MB tool result limit

Claude Desktop and some other MCP clients enforce a **hard 1MB limit on the
entire tool result**, and base64 encoding adds ~33% overhead on top of raw
image bytes — so a page that looks small as a PNG can still push the result
over budget once encoded, especially across multiple pages in one call.

To handle this reliably, `read_document_pages`:
- **Defaults to 1 page per call** (call again with a higher `start_page` for more)
- **Automatically retries at lower DPI** (150 → 75 → 50 → 36) if a page doesn't
  fit a conservative internal budget, and tells you in its response text if
  it had to step down resolution
- Splits the byte budget evenly if you do request multiple pages at once via `max_pages`

If you still hit a size error, try explicitly passing a lower `dpi` (e.g. 50)
or `max_pages=1`.

## Notes

- Company numbers are auto zero-padded to 8 characters (e.g. `"123456"` ->
  `"00123456"`), matching Companies House's own convention.
- Rate limit is 600 requests / 5 minutes, shared across *all* endpoints —
  the server surfaces a clear error if you hit it.
- `get_filing_history_item` returns a link to document metadata; use
  `download_document` to actually fetch the PDF/XHTML/JSON content.

## Automated testing

Fast deterministic pytest suite (all mocked HTTP, no network, no API key):

```bash
pip install pytest pytest-asyncio respx
pytest tests/ -v
```

Nineteen tests covering: URL construction, company-number padding, HTTP
error handling (401/404/429/missing-key), search-param serialisation,
document rendering size-budget compliance, and tool-registration contract.
Runs in under a second — safe to put in CI.

## Evaluations

Different from unit tests: these grade whether an LLM can *use* the server
to actually complete real tasks end-to-end. Requires both API keys.

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
export COMPANIES_HOUSE_API_KEY="..."
python evals/eval_runner.py
```

`evals/eval_runner.py` spawns the server, hands its tool schemas to Claude
via the Anthropic API, runs an agentic loop (Claude picks tool → we execute
via MCP → feed result back → repeat), then grades each task:

- **Verifiable tasks** — checks the answer contains required substrings and
  that expected tools were actually called (e.g. "must include the
  director's surname", "must have called `get_officer_appointments`").
- **Judged tasks** — sends the open-ended answer to a second Claude call
  acting as an LLM-as-judge, scoring against a written rubric.

Tasks live in `evals/tasks.json`. Add more to expand coverage. Prints a
per-task PASS/FAIL and aggregate at the end. This is the pattern you'd
extend into a real eval harness — add more tasks, track scores over time,
compare across model versions or server iterations.

## Use with Claude Desktop

```json
{
  "mcpServers": {
    "companies-house": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "COMPANIES_HOUSE_API_KEY": "your-key-here"
      }
    }
  }
}
```
