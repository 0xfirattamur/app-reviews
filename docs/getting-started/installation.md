---
description: Install app-reviews with pip or uv on Python 3.11 and newer.
---

# Installation

## Requirements

- **Python 3.11** or higher

---

## Install with pip

```bash
pip install app-reviews
```

## Install with uv

```bash
uv add app-reviews
```

---

## Install from Source

```bash
git clone https://github.com/0xfirattamur/app-reviews.git
cd app-reviews
uv sync
```

To also install development tools:

```bash
uv sync --group dev
```

---

## Verify the Installation

```bash
python -c "from app_reviews import AppStoreReviews; print('OK')"
```

---

## Dependencies

Two runtime dependencies:

| Package | Purpose |
|---------|---------|
| `cryptography` | JWT signing for authenticated API access |
| `httpx` | HTTP transport, sync and async |
