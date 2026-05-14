"""thread-keeper: local MCP server that persists Claude's working memory
across conversations on this machine. The brief format is optimized for
Claude (token density, structural tags, opaque IDs) — not for human
readability.

Storage : ~/.threadkeeper/db.sqlite (SQLite + FTS5; embeddings optional)
Wire    : stdio MCP, registered in claude_desktop_config.json
"""
