"""Генератор поисковых ссылок 2GIS по городам x категориям для Parser2GIS.

Пример:
  python -m importer.build_2gis_urls --cities moscow,spb --queries "бизнес-консалтинг,бизнес-тренер"

Печатает готовые ссылки построчно — их можно сразу передать в parser-2gis:
  parser-2gis -i $(python -m importer.build_2gis_urls --cities moscow --queries "бизнес-консалтинг") -o data/out.csv -f csv

Список city-слагов 2GIS: смотрится в адресной строке при открытии города на 2gis.ru
(например https://2gis.ru/moscow -> "moscow", https://2gis.ru/novosibirsk -> "novosibirsk").
"""
from __future__ import annotations

import argparse
from urllib.parse import quote


def build_urls(cities: list[str], queries: list[str]) -> list[str]:
    urls = []
    for city in cities:
        city = city.strip()
        if not city:
            continue
        for query in queries:
            query = query.strip()
            if not query:
                continue
            urls.append(f"https://2gis.ru/{city}/search/{quote(query)}")
    return urls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", required=True, help="Слаги городов через запятую, напр. moscow,spb")
    parser.add_argument("--queries", required=True, help="Категории/запросы через запятую")
    args = parser.parse_args()

    cities = args.cities.split(",")
    queries = args.queries.split(",")
    for url in build_urls(cities, queries):
        print(url)


if __name__ == "__main__":
    main()
