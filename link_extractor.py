"""
Later can be changed to a decent structure to have 'locks', 'attempts' and other stuff to handle multiple scrapers working on it
"""
import json
from pathlib import Path

from places import ShoppingMallList


def extract_links(
    malls_path: str | Path = "malls.json",
    out_path: str | Path = "links.json",
    ) -> int:
    malls = ShoppingMallList.from_json_file(malls_path)
    links: list[dict[str,str | None]] = [{"place_id": mall.place_id, "name": mall.name, "website": mall.website} for mall in malls if mall.website]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(links, f, indent=2, ensure_ascii=False)
    return len(links)


if __name__ == "__main__":
    n = extract_links()
    print(f"wrote {n} links to links.json")
