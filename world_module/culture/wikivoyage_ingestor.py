"""Wikivoyage Ingestor — extract cultural evidence from Wiki dump for region_aware KB.

Reads the Wikivoyage XML dump, finds target city pages, extracts relevant
sections, and writes evidence_cards JSON for manual review → constraint creation.

Usage:
    python -m world_module.culture.wikivoyage_ingestor \
        --dump data/raw/wikivoyage/enwikivoyage/enwikivoyage-latest-pages-articles.xml.bz2 \
        --cities Dubai,Barcelona,Tokyo,Jerusalem,Singapore \
        --output outputs/benchmarks/region_aware/cultural_sources/wikivoyage/
"""

from __future__ import annotations

import bz2
import json
import re
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import iterparse

from world_module.culture.wikivoyage_source_pipeline import build_source_snapshot


class WikivoyageIngestor:
    """Extract cultural evidence cards from Wikivoyage XML dump by city."""

    TARGET_SECTIONS = [
        "Respect", "Eat", "Drink", "Stay safe",
        "Get around", "Understand", "Buy", "Sleep", "Cope",
        "Festivals and events",
    ]

    def __init__(self, dump_path: str, output_dir: str):
        self.dump_path = Path(dump_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def extract_all(self, cities: list[str], *, output_keys: dict[str, str] | None = None) -> dict[str, dict]:
        """Extract evidence for all target cities. Returns {city_name: evidence_dict}."""
        results = {}
        for city in cities:
            print(f"[WikivoyageIngestor] Extracting {city}...", flush=True)
            evidence = self.extract_city(city)
            if evidence:
                output_key = (output_keys or {}).get(city, city.lower())
                out_path = self.write_evidence(evidence, output_key)
                print(f"  → {out_path} ({len(evidence['evidence_cards'])} cards)", flush=True)
                results[city] = evidence
            else:
                print(f"  → NOT FOUND in dump", flush=True)
        return results

    def write_evidence(self, evidence: dict[str, Any], output_key: str) -> Path:
        """Persist evidence under a caller-owned canonical key.

        A Wikivoyage page title is source metadata, not an output identity.  In
        particular, titles containing spaces must not decide the file name of a
        generated city such as ``new_york_united_states``.
        """
        if not output_key or Path(output_key).name != output_key:
            raise ValueError("Evidence output key must be a simple canonical city ID")
        payload = dict(evidence)
        payload["source_city_id"] = payload.get("city_id", "")
        payload["city_id"] = output_key
        path = self.output_dir / f"{output_key}.json"
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
        return path

    def extract_city(self, city_name: str) -> dict | None:
        """Find a city page in the dump, parse sections, return evidence dict."""
        wikitext = self._find_page_wikitext(city_name)
        if not wikitext:
            # Try alternative names (case variations)
            for alt in [city_name.title(), city_name.upper(), city_name.lower()]:
                if alt != city_name:
                    wikitext = self._find_page_wikitext(alt)
                    if wikitext:
                        city_name = alt
                        break
        if not wikitext:
            return None

        sections = self._parse_wikitext_sections(wikitext)
        evidence_cards = []
        idx = 1
        for section_name, text in sections.items():
            if section_name not in self.TARGET_SECTIONS:
                continue
            # Split into paragraphs
            paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 30]
            for para in paragraphs:
                keywords = self._extract_keywords(para)
                evidence_cards.append({
                    "evidence_id": f"wv_{city_name.lower()}_{idx:03d}",
                    "section": section_name,
                    "text": para,
                    "keywords": keywords,
                    "used_for_constraints": [],
                })
                idx += 1

        return {
            "city_id": city_name.lower(),
            "source": "wikivoyage",
            "language": "en",
            "page_title": city_name,
            "dump_file": self.dump_path.name,
            "evidence_cards": evidence_cards,
        }

    def extract_city_source_snapshot(self, city_name: str, *, city_id: str | None = None) -> dict | None:
        """Return a raw source snapshot without changing legacy evidence output."""
        resolved = self.resolve_city_page(city_name)
        if resolved is None:
            return None
        page_title, wikitext = resolved
        return build_source_snapshot(
            city_id=city_id or city_name.casefold().replace(" ", "_"),
            page_title=page_title,
            language="en",
            dump_file=self.dump_path.name,
            raw_wikitext=wikitext,
        )

    def resolve_city_page(self, page_title: str) -> tuple[str, str] | None:
        """Resolve redirects and an unambiguous city link from a disambiguation page."""
        current = page_title.strip()
        visited: set[str] = set()
        for _ in range(4):
            key = current.casefold()
            if not current or key in visited:
                return None
            visited.add(key)
            wikitext = self._find_page_wikitext(current)
            if not wikitext:
                return None
            redirect = re.match(r"^\s*#redirect\s*\[\[([^\]|#]+)", wikitext, re.IGNORECASE)
            if redirect:
                current = redirect.group(1).strip()
                continue
            if not re.search(r"\{\{\s*(?:disamb|disambiguation)\b", wikitext, re.IGNORECASE):
                return current, wikitext
            candidates = self._city_candidates_from_disambiguation(wikitext)
            if len(candidates) != 1:
                raise ValueError(
                    f"Wikivoyage page {current!r} is a disambiguation page and does not identify "
                    f"one unique city page; candidates: {candidates}"
                )
            current = candidates[0]
        return None

    @staticmethod
    def _city_candidates_from_disambiguation(wikitext: str) -> list[str]:
        candidates: list[str] = []
        city_terms = re.compile(r"\b(?:city|town|village|municipality)\b", re.IGNORECASE)
        non_city_terms = re.compile(r"\b(?:state|province|county|region|district|island|country)\b", re.IGNORECASE)
        for line in wikitext.splitlines():
            if not line.lstrip().startswith("*") or not city_terms.search(line):
                continue
            description = line.split("-", 1)[1] if "-" in line else line
            if non_city_terms.search(description) and not city_terms.search(description):
                continue
            link = re.search(r"\[\[([^\]|#]+)", line)
            if link:
                title = link.group(1).strip()
                if title and title not in candidates:
                    candidates.append(title)
        return candidates

    def _find_page_wikitext(self, city_name: str) -> str | None:
        """Find a page by title in the XML dump. Handles MW export-0.11 namespace."""
        """Find a page by title in the XML dump. Handles MW export-0.11 namespace."""
        import xml.etree.ElementTree as ET
        opener = bz2.open if str(self.dump_path).endswith('.bz2') else open
        target = city_name.lower().strip()
        ns = '{http://www.mediawiki.org/xml/export-0.11/}'
        ns_alt = '{http://www.mediawiki.org/xml/export-0.10/}'

        with opener(str(self.dump_path), 'rb') as f:
            context = iterparse(f, events=('end',))
            for event, elem in context:
                tag_name = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_name == 'page':
                    # Try namespace-aware find first
                    title_elem = elem.find(f'{ns}title') or elem.find(f'{ns_alt}title') or elem.find('title')
                    if title_elem is None:
                        # Walk children to find <title> regardless of namespace
                        for child in elem.iter():
                            if child.tag.split('}')[-1] == 'title' and child.text:
                                title_elem = child
                                break
                    if title_elem is not None and title_elem.text:
                        page_title = title_elem.text.strip()
                        if page_title.lower() == target:
                            # Found the page — extract revision text
                            rev = elem.find(f'{ns}revision') or elem.find(f'{ns_alt}revision') or elem.find('revision')
                            if rev is None:
                                for child in elem.iter():
                                    if child.tag.split('}')[-1] == 'revision':
                                        rev = child
                                        break
                            if rev is not None:
                                text_elem = rev.find(f'{ns}text') or rev.find(f'{ns_alt}text') or rev.find('text')
                                if text_elem is None:
                                    for child in rev.iter():
                                        if child.tag.split('}')[-1] == 'text' and child.text:
                                            text_elem = child
                                            break
                                if text_elem is not None and text_elem.text:
                                    result = text_elem.text
                                    elem.clear()
                                    return result
                    elem.clear()
        return None

    @staticmethod
    def _parse_wikitext_sections(wikitext: str) -> dict[str, str]:
        """Parse MediaWiki section headings and their content."""
        sections = {}
        current_section = "_lead"
        current_text = []

        for line in wikitext.split('\n'):
            # Match == Section == or === Subsection ===
            m = re.match(r'^(={2,})\s*(.*?)\s*\1$', line)
            if m and not m.group(2).startswith('='):
                # Save previous section
                if current_text:
                    sections[current_section] = '\n'.join(current_text)
                    current_text = []

                # Extract section name (strip wikilinks)
                sec_name = re.sub(r'\[\[|\]\]', '', m.group(2)).strip()
                current_section = sec_name
            else:
                # Clean wikitext markup
                clean = line
                # Remove wikilinks: [[Target|text]] → text, [[text]] → text
                clean = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', clean)
                # Remove HTML tags
                clean = re.sub(r'<[^>]+>', '', clean)
                # Remove templates {{...}}
                clean = re.sub(r'\{\{[^}]*\}\}', '', clean)
                # Remove references <ref>...</ref>
                clean = re.sub(r'<ref[^>]*>.*?</ref>', '', clean, flags=re.DOTALL)
                # Remove bullet markers
                clean = clean.lstrip('*#:; ')

                if clean.strip():
                    current_text.append(clean.strip())

        if current_text:
            sections[current_section] = '\n'.join(current_text)

        return sections

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract relevant cultural keywords from a paragraph."""
        keywords = []
        culture_terms = [
            "respect", "dress", "religion", "custom", "tradition", "etiquette",
            "forbidden", "prohibited", "avoid", "offensive", "polite", "greeting",
            "dining", "restaurant", "cuisine", "meal", "dish", "local", "tip",
            "alcohol", "drink", "bar", "pub", "halal", "kosher", "vegetarian",
            "ramadan", "festival", "holiday", "celebration", "ceremony",
            "temple", "mosque", "church", "shrine", "shoes", "bow", "handshake",
            "bargain", "negotiate", "price", "market", "souk", "bazaar",
            "women", "covering", "headscarf", "hijab", "modesty", "public",
            "dangerous", "scam", "safety", "police", "emergency",
            "transport", "bus", "metro", "train", "taxi", "driving",
            "siesta", "opening hours", "closed", "sunday", "friday prayer",
            "photography", "camera", "photo", "permission",
        ]
        text_lower = text.lower()
        for term in culture_terms:
            if term in text_lower:
                keywords.append(term)
        return keywords[:15]


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Extract cultural evidence from Wikivoyage dump")
    parser.add_argument("--dump", required=True, help="Path to enwikivoyage-*-pages-articles.xml.bz2")
    parser.add_argument("--cities", default="Dubai,Barcelona,Tokyo,Jerusalem,Singapore",
                        help="Comma-separated target city names")
    parser.add_argument("--output", default="outputs/benchmarks/region_aware/cultural_sources/wikivoyage/",
                        help="Output directory for evidence JSON files")
    args = parser.parse_args()

    ingestor = WikivoyageIngestor(args.dump, args.output)
    results = ingestor.extract_all([c.strip() for c in args.cities.split(",")])
    total_cards = sum(len(v["evidence_cards"]) for v in results.values())
    print(f"\n[Done] {len(results)} cities, {total_cards} evidence cards")
