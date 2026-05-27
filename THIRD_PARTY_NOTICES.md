# Third-Party Notices

This repository depends on upstream open-source software. These notices are provided for attribution and license clarity.

## nanobot-ai

- Package: `nanobot-ai`
- Installed by: `uv pip install --system --no-cache nanobot-ai`
- Source: https://github.com/HKUDS/nanobot
- PyPI: https://pypi.org/project/nanobot-ai/
- License: MIT License
- Copyright: Copyright (c) 2025-present Xubin Ren and the nanobot contributors

`morneven_nanobot` does not vendor the upstream Nanobot source code. It installs `nanobot-ai` as a Python package dependency in the Docker image and applies Morneven-specific runtime integration code around it.

MIT permission notice for upstream `nanobot-ai`:

```text
MIT License

Copyright (c) 2025-present Xubin Ren and the nanobot contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Morneven Runtime Integration

The files in this repository are Morneven-specific integration, dashboard, deployment, and runtime patch files. They are licensed under this repository's `LICENSE`.

This project is not affiliated with, endorsed by, or sponsored by the upstream `nanobot-ai` maintainers unless explicitly stated by those maintainers.
