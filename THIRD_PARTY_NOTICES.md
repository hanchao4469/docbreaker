# Third-party components

The MIT license in this repository applies to project code, not to third-party software, model services, fonts or documents processed by it.

Python dependencies are listed with pinned versions in `requirements.txt`. Their upstream distributions include their respective licenses:

| Component | Upstream license |
|---|---|
| FastAPI | MIT |
| HTTPX | BSD-3-Clause |
| Pydantic | MIT |
| Uvicorn | BSD-3-Clause |
| Pillow | HPND (PIL Software License) |
| Beautiful Soup | MIT |
| markdownify | MIT |

The Docker image installs Debian packages including Poppler utilities/data and Noto CJK fonts. Poppler is GPL-licensed; fonts, character mapping data, Python, Debian packages and transitive dependencies retain their individual licenses and notices. Consult the installed packages' `/usr/share/doc/*/copyright` files and upstream sources when redistributing an image. Licensing the project code under MIT does not relicense those components or remove their redistribution obligations.

No customer PDF fixtures, model weights or proprietary gateway implementation are included. External model use is subject to the provider's terms and fees.
