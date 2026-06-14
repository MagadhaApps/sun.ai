#!/usr/bin/env python3
"""Built-in MCP Server: Web Scraper"""
import json
import sys
import re
import httpx
from html.parser import HTMLParser
from urllib.parse import urlparse


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
        # Validate URL scheme and hostname to prevent SSRF
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Unsupported URL scheme: {parsed.scheme}"}
        if not parsed.hostname:
            return {"error": "Invalid URL: no hostname"}
        # Block internal/private hosts
        import ipaddress
        try:
            addr = ipaddress.ip_address(parsed.hostname)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
                return {"error": "Access to internal/private hosts is not allowed"}
        except ValueError:
            pass  # Not an IP address, proceed with hostname

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AgenticPlatform/1.0"
            })
            resp.raise_for_status()
            html = resp.text
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
