# Installation

## Requirements

- **Python 3.11** or higher

______________________________________________________________________

## Install with pip

```
pip install app-reviews
```

## Install with uv

```
uv add app-reviews
```

______________________________________________________________________

## Install from Source

```
git clone https://github.com/0xfirattamur/app-reviews.git
cd app-reviews
uv sync
```

To also install development tools:

```
uv sync --group dev
```

______________________________________________________________________

## Verify the Installation

```
python -c "from app_reviews import AppStoreReviews; print('OK')"
```

______________________________________________________________________

## Dependencies

Two runtime dependencies:

| Package        | Purpose                                  |
| -------------- | ---------------------------------------- |
| `cryptography` | JWT signing for authenticated API access |
| `httpx`        | HTTP transport, sync and async           |
