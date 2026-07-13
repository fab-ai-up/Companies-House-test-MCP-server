from anthropic import Anthropic
client = Anthropic()
resp = client.beta.messages.create(
    model="claude-opus-4-7",
    max_tokens=1024,
    mcp_servers=[{
        "type": "url",
        "url": "https://companies-house-test-mcp-server-production.up.railway.app",
        "name": "companies-house-fabrizio",
    }],
    messages=[{"role": "user", "content": "Look up Companies House company 15246704"}],
    extra_headers={"anthropic-beta": "mcp-client-2025-04-04"},
)
print(resp.content)