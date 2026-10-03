# 076 — Dependency licensing risk

## Verdict

No GPL, AGPL, or LGPL license is evident in the declared Python dependencies or the ordinary Python-package dependency chains reviewed here. Commercial use and shipping are generally allowed; the notable current obligation is `certifi`'s MPL-2.0 license (pulled in by Requests), plus the usual copyright/license notice requirements for permissive licenses. This is not a complete artifact-level clearance: there is no lockfile, and downloaded browser/native binaries can have their own license inventory.

## Findings

### S2 — Resolve and record the full dependency/artifact license set before shipping
- **Where:** `requirements.txt:1-3`; the brief confirms there is no lockfile or transitive pins (`docs/audits/BRIEF.md:43`).
- **Breaks:** The three top-level pins do not freeze the transitive versions, and platform-specific wheels can bundle native libraries not represented as Python `Requires-Dist` dependencies. A license conclusion from package names alone is therefore not a reproducible bill of materials for a shipped image or installer.
- **Trigger:** A release installs the same requirements on another date or platform, or adds `playwright install`/compiled data-science wheels, and packages different transitive or bundled files than the audit reviewed.
- **Fix:** Lock and hash the resolved environment per supported platform, generate an SBOM including wheel/browser/native payloads, and publish the corresponding third-party notices with every distributed artifact.
- **Current declared graph:** `requests==2.32.3` is Apache-2.0; its required dependencies are `charset-normalizer` (MIT), `idna` (BSD-3-Clause), `urllib3` (MIT), and `certifi` (MPL-2.0). `beautifulsoup4==4.12.3` is MIT and requires `soupsieve` (MIT). `lxml==5.2.2` is BSD-licensed; its bundled `libxml2` and `libxslt` are MIT-licensed. No GPL/AGPL/LGPL package appears in these declared Python runtime dependencies. [Requests 2.32.3 metadata](https://pypi.org/pypi/requests/2.32.3/json), [Beautiful Soup 4.12.3 metadata](https://pypi.org/pypi/beautifulsoup4/4.12.3/json), [lxml 5.2 license notes](https://lxml.de/5.2/), [certifi license](https://github.com/certifi/python-certifi/blob/master/LICENSE).

### S3 — `certifi` is weak-copyleft, but does not require opening the whole product
- **Where:** `requirements.txt:1` (through Requests' required dependency `certifi`).
- **Breaks:** Omitting the MPL license/attribution from a redistributed binary or failing to provide source for modifications to MPL-covered certifi files can breach its terms. This is the one current transitive license in the copyleft family broadly relevant to this audit; it is MPL-2.0, not GPL/AGPL/LGPL.
- **Trigger:** Bundle the environment in a customer-installed executable/container, and either strip certifi's license notices or modify its CA bundle/code without making the covered modifications available as required by MPL-2.0.
- **Fix:** Preserve the included license/copyright notices and provide the source for any changes to MPL-covered files when distributing them. Keep the rest of the proprietary application closed if desired; MPL file-level copyleft does not generally require publishing unrelated application source.
- **Commercial shipping:** MPL-2.0 permits commercial use and distribution. It imposes obligations on covered files and modifications, not a product-wide source disclosure requirement. [certifi PyPI license](https://pypi.org/project/certifi/), [MPL-2.0 text](https://www.mozilla.org/en-US/MPL/2.0/).

## Candidate dependencies

| Candidate | Main/runtime license picture | Commercial shipping guidance |
|---|---|---|
| `playwright` | Python package is Apache-2.0; its Python dependencies (notably `greenlet` and `pyee`) are permissively licensed. Browser binaries are a separate download, not merely Python package dependencies. | Commercial use is allowed. If browsers are included in a product/container, inventory that exact Chromium/Firefox/WebKit build and include its third-party notices; do not infer the browser payload's full license set from Playwright's Apache license. Playwright documents separate versioned browser downloads. [PyPI](https://pypi.org/project/playwright/), [browser installation docs](https://playwright.dev/python/docs/browsers). |
| `pandas` | BSD-3-Clause; core required Python dependencies include NumPy (BSD-3-Clause), python-dateutil (BSD-3-Clause), and timezone data/package dependencies with their own permissive licenses. | Commercial use and closed-source integration are allowed with retained notices. Since NumPy is a compiled/native package, review the licenses in the exact platform wheel/native libraries rather than relying only on NumPy's top-level BSD license. [pandas](https://pypi.org/project/pandas/), [NumPy license](https://numpy.org/doc/stable/license.html). |
| `pyarrow` | Apache-2.0 for PyArrow/Apache Arrow; the distributed native package may include third-party components with separate notices. | Commercial use is allowed. Retain Apache-2.0 and bundled third-party notices from the exact wheel; verify the packaged artifact. [PyArrow](https://pypi.org/project/pyarrow/), [Apache Arrow license](https://github.com/apache/arrow/blob/main/LICENSE.txt). |
| `phonenumbers` | Apache-2.0; no material runtime dependency chain is declared. | Commercial use is allowed; retain copyright/license notices when redistributing. [PyPI](https://pypi.org/project/phonenumbers/), [license](https://github.com/daviddrysdale/python-phonenumbers/blob/dev/LICENSE). |

No GPL/AGPL/LGPL obligation was identified in the candidates' ordinary Python dependency metadata. That is not a blanket clearance for Playwright's downloaded browsers or compiled/platform-specific payloads: review their included license/NOTICE files and any system packages shipped with the deployment. The candidates are not version-pinned here, so their exact dependency sets are not fixed.

## What can and cannot ship

- **Can ship commercially:** The current script, its CSV output, and a proprietary commercial product using these dependencies. No reviewed GPL/AGPL component creates a copyleft source-release trigger, including for a network service; the reviewed package licenses do not impose a general requirement to open the application's source code. Merely generating CSVs with the libraries does not make the CSVs subject to those software licenses.
- **Must ship with a redistributed runtime:** Applicable copyright/license texts and attribution notices (notably Apache-2.0/BSD/MIT notices and certifi's MPL notice; include bundled component notices too).
- **Must do if modifying MPL-covered certifi files and distributing them:** Provide the source for those covered modifications under MPL-2.0 terms. This does not extend to unrelated proprietary files.
- **Cannot safely claim yet:** That every file in a release image, installer, browser cache, or platform wheel has been cleared; those exact artifacts and dependencies are not locked or inventoried in this repository.

## Recommended order of work

1. Decide whether Beautiful Soup and lxml are needed (they are declared in `requirements.txt:2-3`); removing unused packages narrows the distribution inventory.
2. Add a reproducible lock with hashes for each supported platform and generate an SBOM from the resolved install, not just `requirements.txt`.
3. For product releases, collect wheel and browser/native-component license files into a third-party notices bundle; rerun the scan whenever dependency or browser versions change.

## References

- Repository dependency pins: [`requirements.txt`](../../requirements.txt)
- Requests 2.32.3 exact dependency metadata: https://pypi.org/pypi/requests/2.32.3/json
- Beautiful Soup 4.12.3 exact dependency metadata: https://pypi.org/pypi/beautifulsoup4/4.12.3/json
- lxml 5.2 licensing: https://lxml.de/5.2/
- certifi license: https://github.com/certifi/python-certifi/blob/master/LICENSE
- Playwright browser binaries: https://playwright.dev/python/docs/browsers
