#!/usr/bin/env python3
"""Собирает цены с atbmarket.com и пишет prices.json.

Запускается в GitHub Actions раз в день (см. prices.yml).
Переменная окружения ATB_CITY (например "kharkiv", "odesa") выбирает город;
пусто = цены по умолчанию. Цены на сайте зависят от города.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://www.atbmarket.com"
CITY = os.environ.get("ATB_CITY", "").strip().strip("/")
# Все продуктовые разделы каталога АТБ (без алкоголя, химии, одежды и т.п.)
CATEGORIES = [
    # Овочі та фрукти
    "446-gorikhi-sukhofrukti", "298-gribi", "390-zelen", "289-ovochi",
    "468-solinnya-salati", "288-frukti-yagodi",
    # Бакалія
    "341-boroshno", "444-buterbrodni-masla-zakuski-pasti", "dzem-pasta-med",
    "371-diabetichna-ta-dietichna-produktsiya", "iza-svidkogo-prigotuvanna", "395-krupi",
    "masni-ta-ribni-konservi", "431-mayonez", "348-makaronni-virobi",
    "ovoceva-ta-fruktova-konservacia", "olia-ta-ocet", "305-pripravi-ta-marinadi",
    "365-sousi-ketchupi", "378-sukhi-snidanki", "306-tovari-dlya-vipichki", "312-tsukor",
    # Молочні продукти та яйця
    "399-vershki", "422-dityacha-molochna-produktsiya", "zgusene-moloko", "349-yogurti",
    "316-kislomolochni-napoi", "414-maslo-i-margarin", "398-moloko", "382-molochni-deserti",
    "379-sir-kislomolochniy", "317-sirki-glazurovani", "380-sirki-dityachi", "329-smetana",
    "381-yaytsya-kuryachi-perepelini",
    # Напої безалкогольні
    "364-yenergetichni", "kvas", "mineralna-i-pitna-voda", "307-napoi", "324-soki-nektari",
    # М'ясо, сири, ковбаси
    "421-m-yasni-napivfabrikati", "344-m-yaso-okholodzhene",
    "402-siri-m-yaki", "439-siri-plavleni", "433-siri-tverdi",
    "408-inshi-m-yasni-virobi", "kovbasa", "narizki-ta-delikatesi", "377-sosiski-sardel-ki",
    # Кондитерські вироби
    "327-batonchiki", "biskviti-ta-deserti", "338-vafli", "490-drazhe", "301-zhuval-ni-gumki",
    "409-krekeri", "kruasani-ta-zefir", "pasti-sokoladno-gorihovi", "pecivo-praniki",
    "300-skhidni-solodoshchi", "351-torti-i-tistechka", "321-tsukerki", "303-shokolad",
    # Риба і морепродукти
    "ikra-ta-ribna-pasta", "406-krabovi-palichki", "moreprodukti", "440-preservi", "riba",
    "441-ribna-narizka",
    # Хлібобулочні вироби
    "333-baton", "326-bulochni-listkovi-virobi", "482-garyacha-vipichka", "397-lavash",
    "suska-ta-suhari", "331-khlib", "347-khlibtsi",
    # Заморожені продукти
    "356-vareniki-pel-meni-mlintsi", "334-morozivo", "457-napivfabrikati",
    "452-ovochi-ta-frukti-svizhozamorozheni", "430-tisto-vipichka-pitsa",
    # Кава, чай, снеки, дитяче харчування, кулінарія
    "286-kava-kakao", "318-chay", "sneki", "cipsi",
    "367-voda", "418-dityache-pechivo", "386-kashi", "366-molochni-sumishi", "340-pyure",
    "368-soki-chay", "503-buterbrodi-burgeri", "416-vasabi-imbir-otset", "464-nori-i-panirovka",
]
MAX_PAGES = 30
DELAY = 1.2  # пауза между запросами, не нагружаем сайт
DEBUG = os.environ.get("DEBUG") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9",
}

PRODUCT_HREF = re.compile(r"/product/([^/?#]+)")
PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*грн\s*/\s*(?:шт|кг)")
CARD_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[AА][TТ][БB]")  # цена по карте АТБ
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл)\b", re.I)
FAT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


# На сайте рубли и копейки лежат в разных тегах: "57. 90" -> "57.90"
SPLIT_NUM_RE = re.compile(r"(\d)\s*([.,])\s*(\d)")


def fix_numbers(text):
    return SPLIT_NUM_RE.sub(r"\1.\3", text)


def num(s):
    return float(s.replace(",", "."))


def slug_of(href):
    m = PRODUCT_HREF.search(href)
    return m.group(1) if m else None


def card_for(link, max_depth=6):
    """Самый большой предок ссылки, внутри которого только один товар."""
    node = link
    for _ in range(max_depth):
        parent = node.parent
        if parent is None or parent.name in ("body", "html"):
            break
        slugs = {slug_of(a["href"]) for a in parent.find_all("a", href=PRODUCT_HREF)}
        if len(slugs) > 1:
            break
        node = parent
    return node


def weight_g(name):
    m = WEIGHT_RE.search(name)
    if not m:
        return None
    value, unit = num(m.group(1)), m.group(2).lower()
    return value * {"кг": 1000, "г": 1, "л": 1000, "мл": 1}[unit]


def fat_percent(name):
    m = FAT_RE.search(name)
    return num(m.group(1)) if m else None


def tags_for(name):
    n = name.lower()
    tags = []
    if re.search(r"без\s+дріжджів|бездріжджов", n):
        tags.append("no_yeast")
    if re.search(r"власному\s+соку|своєму\s+соку|\bв/с\b|\bс/с\b", n):
        tags.append("own_juice")
    return tags


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    by_slug = {}
    for a in soup.find_all("a", href=True):
        s = slug_of(a["href"])
        if s:
            by_slug.setdefault(s, []).append(a)

    items = {}
    for slug, links in by_slug.items():
        name = next((t for t in (a.get_text(" ", strip=True) for a in links) if t), "")
        if not name:
            continue
        text = fix_numbers(card_for(links[0]).get_text(" ", strip=True))
        if "Немає в наявності" in text:
            continue
        m = PRICE_RE.search(text)
        if not m:
            continue
        card = CARD_RE.search(text)
        items[slug] = {
            "name": name,
            "price": num(m.group(1)),
            "card_price": num(card.group(1)) if card else None,
            "weight_g": weight_g(name),
            "fat": fat_percent(name),
            "tags": tags_for(name),
        }
    return items


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


def describe(html):
    """Диагностика: что за страница пришла."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "-"
    n = html.count("/product/")
    print(f"  {len(html)} bytes, title={title[:90]!r}, '/product/' x{n}")
    first = soup.find("a", href=PRODUCT_HREF)
    if first is None:
        print("  no product links. Page text:", soup.get_text(" ", strip=True)[:300])
    else:
        card = card_for(first)
        print("  first card text:", card.get_text(" ", strip=True)[:300])
        print("  first card html:", str(card)[:500].replace("\n", " "))


def scrape_category(cat):
    prefix = f"{BASE}/{CITY}/" if CITY else f"{BASE}/"
    found = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"{prefix}catalog/{cat}" + (f"?page={page}" if page > 1 else "")
        try:
            html = fetch(url)
            if DEBUG and page == 1:
                describe(html)
            items = parse_page(html)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {url}: {e}", file=sys.stderr)
            break
        new = {k: v for k, v in items.items() if k not in found}
        print(f"  page {page}: {len(items)} items, {len(new)} new")
        if not new:
            break
        found.update(new)
        time.sleep(DELAY)
    return found


def main():
    all_items = {}
    for cat in CATEGORIES:
        print(f"Category {cat}")
        all_items.update(scrape_category(cat))
        time.sleep(DELAY)

    if not all_items:
        print("Nothing scraped — prices.json left untouched", file=sys.stderr)
        sys.exit(1)

    data = {
        "store": "АТБ",
        "city": CITY or "default",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "products": list(all_items.values()),
    }
    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved {len(all_items)} products")


if __name__ == "__main__":
    main()
