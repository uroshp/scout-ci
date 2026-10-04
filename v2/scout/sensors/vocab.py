"""Product vocabulary per company (lower-case company key -> product names). Used by the sensor
seeding (news queries) and by the lifecycle audit (a search that names a company's product serves
the card's focus area, not its corporate scope). Curated, small, $0."""
PRODUCT_QUERIES = {
    "anthropic": ["Claude"], "openai": ["ChatGPT"], "google": ["Gemini", "DeepMind"], "mistral": ["Magistral"],
    "atlassian": ["Jira", "Confluence"], "aws": ["Bedrock"], "cognition": ["Devin"], "hubspot": ["Breeze"],
    "salesforce": ["Agentforce"], "cursor": ["Anysphere"], "perplexity": ["Comet"],
}
