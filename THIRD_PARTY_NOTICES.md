# Optional pairing dependencies

The paired TLS installer downloads these pinned Python packages into the user's
private bridge runtime. They are not vendored in this source checkout or preview
source ZIP. Their package distributions retain their own license notices.

| Package | Version | License |
| --- | --- | --- |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| qrcode | 8.2 | BSD |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.11 | BSD-3-Clause |
| colorama | 0.4.6 | BSD-3-Clause |

Versions and terms were checked against the downloaded package metadata and
included license files. The source ZIP does not contain Windows SSH, Git,
Python, Herdr binaries, or an installed bridge's private state. The original
bridge source has not yet selected a project license; the pairing addition
does not change that licensing decision.
