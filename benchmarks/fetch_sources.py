"""Download the two documented development sources and verify their bytes."""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path


SOURCES = [
    {
        "name": "pedestrians",
        "url": "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi",
        "sha256": "45cddc9490be69345cbdab64ca583be65987e864ca408038e648db99e10516cf",
    },
    {
        "name": "megamind",
        "url": "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/Megamind.avi",
        "sha256": "0057387cb7e75c8fd1663b62cfdc51fa53f527795d0fe3c1fea2fd159d3130b5",
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    root.mkdir(parents=True, exist_ok=True)
    for source in SOURCES:
        path = root / f"{source['name']}.avi"
        if path.exists():
            data = path.read_bytes()
        else:
            with urllib.request.urlopen(source["url"], timeout=60) as response:
                data = response.read()
        if hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError(f"source checksum changed: {source['name']}")
        if not path.exists():
            path.write_bytes(data)
        print(f"verified {path}")
    (root / "sources.json").write_text(json.dumps(SOURCES, indent=2))


if __name__ == "__main__":
    main()
