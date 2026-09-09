#!/usr/bin/env python3
"""Compile la page /system UNE FOIS, ici, au lieu de le faire dans chaque
téléphone à chaque visite.

Avant (jusqu'au 2026-09-09) : la page envoyait Babel standalone (2,9 Mo) qui
transpilait le JSX dans le navigateur, plus Tailwind « play » (398 Ko) qui
générait son CSS à la volée en scrutant le DOM. Plusieurs secondes de
chargement sur mobile, à chaque fois, pour un résultat toujours identique.

Maintenant :
  assets/system.jsx  ──Babel (node, hors ligne)──▶  api/static/js/system.js
  assets/system.jsx  ──Tailwind CLI 3.4.17─────────▶  api/static/css/system.css
  react / react-dom  ──téléchargés, SRI vérifié──▶  api/static/js/vendor/

Les deux sorties portent en tête l'empreinte SHA-256 de la SOURCE ; le
gardien `tests/test_system_build.py` refuse une source modifiée sans
recompilation. Usage :

    python scripts/build_system.py            # compile JS + CSS
    python scripts/build_system.py --vendor   # + (re)télécharge react/react-dom

Dépendances : node ≥ 18 ; réseau seulement pour le premier téléchargement
de Babel (mis en cache dans ~/.cache/predator) et pour `npx tailwindcss`.
Versions et empreintes ÉPINGLÉES ci-dessous — un CDN qui republie un chemin
casse la compilation bruyamment au lieu de glisser un autre octet.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SRC = RACINE / "assets" / "system.jsx"
OUT_JS = RACINE / "api" / "static" / "js" / "system.js"
OUT_CSS = RACINE / "api" / "static" / "css" / "system.css"
VENDOR = RACINE / "api" / "static" / "js" / "vendor"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "predator"

# Mêmes URL et empreintes SRI que celles que la page chargeait depuis unpkg
# (C4, 2026-08-27) — vérifiées AVANT écriture.
BABEL = ("https://unpkg.com/@babel/standalone@8.0.4/babel.min.js",
         "sha384-bdF7m0Y1IFKt9Q6xC8X9qkXn0OBriQWKyWwZKYsN05zF6P/g9OakjjL0G2Sd4pB4")
VENDORS = {
    "react-18.3.1.production.min.js": (
        "https://unpkg.com/react@18.3.1/umd/react.production.min.js",
        "sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"),
    "react-dom-18.3.1.production.min.js": (
        "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js",
        "sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1"),
}
TAILWIND_VERSION = "3.4.17"
HEADER = "/* system.js — GÉNÉRÉ par scripts/build_system.py, ne pas éditer. source-sha256: {h} */\n"
HEADER_CSS = "/* system.css — GÉNÉRÉ par scripts/build_system.py, ne pas éditer. source-sha256: {h} */\n"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sri_ok(data: bytes, sri: str) -> bool:
    algo, b64 = sri.split("-", 1)
    return base64.b64encode(hashlib.new(algo, data).digest()).decode() == b64


def fetch_verified(url: str, sri: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    if not sri_ok(data, sri):
        sys.exit(f"empreinte SRI FAUSSE pour {url} — le CDN a republié ce chemin, on n'écrit rien")
    return data


def babel_path() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "babel-standalone-8.0.4.min.js"
    if p.exists() and sri_ok(p.read_bytes(), BABEL[1]):
        return p
    print("téléchargement de Babel standalone 8.0.4 (une fois, cache local)…")
    p.write_bytes(fetch_verified(*BABEL))
    return p


NODE_COMPILE = r"""
const fs = require("fs");
const [,, babelPath, srcPath, outPath, header] = process.argv;
const Babel = require(babelPath);
const src = fs.readFileSync(srcPath, "utf8");
const out = Babel.transform(src, {
  presets: [["react", { runtime: "classic" }]],
  sourceType: "script",
  comments: false,
  compact: true,
  minified: true,
  filename: "system.jsx",
}).code;
fs.writeFileSync(outPath, header + out + "\n");
console.log("system.js :", out.length, "octets");
"""


def build_js(src: bytes) -> None:
    header = HEADER.format(h=sha256_hex(src))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(NODE_COMPILE)
        helper = f.name
    try:
        subprocess.run(["node", helper, str(babel_path()), str(SRC), str(OUT_JS), header], check=True)
    finally:
        os.unlink(helper)


def build_css(src: bytes) -> None:
    header = HEADER_CSS.format(h=sha256_hex(src))
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "tailwind.config.js"
        # Même réglage que l'ancien <script>tailwind.config = …</script> :
        # pas de preflight, predator.css garde la main sur les bases.
        cfg.write_text(f"module.exports = {{ content: [{json.dumps(str(SRC))}], corePlugins: {{ preflight: false }} }};\n")
        inp = Path(d) / "in.css"
        inp.write_text("@tailwind utilities;\n")
        out = Path(d) / "out.css"
        subprocess.run(["npx", "-y", f"tailwindcss@{TAILWIND_VERSION}", "-c", str(cfg), "-i", str(inp),
                        "-o", str(out), "--minify"], check=True)
        css = out.read_text(encoding="utf-8")
    OUT_CSS.write_text(header + css + ("\n" if not css.endswith("\n") else ""), encoding="utf-8")
    print("system.css :", len(css), "octets")


def vendor() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, (url, sri) in VENDORS.items():
        p = VENDOR / name
        if p.exists() and sri_ok(p.read_bytes(), sri):
            print("vendor ok :", name)
            continue
        p.write_bytes(fetch_verified(url, sri))
        print("vendor téléchargé :", name)


def main(argv: list[str]) -> None:
    if "--vendor" in argv:
        vendor()
    src = SRC.read_bytes()
    build_js(src)
    build_css(src)
    print("source-sha256 :", sha256_hex(src))


if __name__ == "__main__":
    main(sys.argv[1:])
