#!/usr/bin/env python3
"""Built-in MCP Server: Web Scraper"""
import json
import sys
import urllib.request
import re
from html.parser import HTMLParser


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []
        self._skip = False
        self._skip_tags = {"script", "style", "nav", "footer", "header"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True
        if tag == "a":
            href = dict(attrs).get("href", "")
            self.links.append({"href": href, "text": ""})

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.text_parts.append(text)
                if self.links and not self.links[-1]["text"]:
                    self.links[-1]["text"] = text


def handle_request(method, params):
    url = params.get("url", "")
    if not url:
        return {"error": "URL is required"}

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AgenticPlatform/1.0"
        })
        # Validate URL scheme to prevent file:// or other dangerous schemes
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Unsupported URL scheme: {parsed.scheme}"}
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Failed to fetch URL: {str(e)}"}

    if method == "fetch_page":
        return {"html": html[:50000], "url": url, "size": len(html)}

    elif method == "extract_text":
        extractor = TextExtractor()
        extractor.feed(html)
        text = "\n".join(extractor.text_parts)
        return {"text": text[:20000], "url": url, "length": len(text)}

    elif method == "extract_links":
        extractor = TextExtractor()
        extractor.feed(html)
        # Resolve relative URLs
        from urllib.parse import urljoin
        links = []
        for link in extractor.links:
            href = link["href"]
            if href and not href.startswith("#") and not href.startswith("javascript:"):
                full_url = urljoin(url, href)
                links.append({"text": link["text"], "href": full_url})
        return {"links": links[:200], "count": len(links), "url": url}

    return {"error": f"Unknown method: {method}"}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            method_name = req.get("method", "")
            params = req.get("params", {})
            result = handle_request(method_name, params)
            print(json.dumps({"id": req.get("id"), "result": result}))
            sys.stdout.flush()
        except json.JSONDecodeError:
            pass
