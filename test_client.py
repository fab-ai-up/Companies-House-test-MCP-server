import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"Loaded {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  - {t.name}")

            print("\n--- get_company_profile('15246704') [Wanderist Ltd] ---")
            result = await session.call_tool(
                "get_company_profile", {"company_number": "15246704"}
            )
            print(result.content[0].text)

            print("\n--- search_companies('Wanderist') ---")
            result = await session.call_tool(
                "search_companies", {"query": "Wanderist"}
            )
            print(result.content[0].text[:1500])

            # Example document flow (needs a real transaction_id from your
            # own get_filing_history call — this is illustrative only):
            print("\n--- get_filing_history('15246704', category='accounts') ---")
            result = await session.call_tool(
                "get_filing_history", {"company_number": "15246704", "category": "accounts"}
            )
            print(result.content[0].text[:1000])
            print(
                "\nTo download a document: take a transaction_id from the list above, "
                "call get_filing_history_item to get its document_metadata link, "
                "extract the document_id from that URL, then call download_document."
            )


if __name__ == "__main__":
    asyncio.run(main())
