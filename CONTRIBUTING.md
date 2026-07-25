# Contributing

Thanks for taking the time. Bug reports, ideas, and pull requests are all welcome.

## Local development

Prerequisites: Python 3.12+, Node 22+, and Docker (optional, for PostgreSQL).

```pwsh
# Backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
Copy-Item .env.example .env      # change the bootstrap password
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # run from .\backend

# Frontend (separate terminal)
npm --prefix frontend install
npm --prefix frontend run dev    # http://127.0.0.1:5173
```

With no `DATABASE_URL` the app creates SQLite at `.data/azureops.db`. That is the supported local
setup and needs no other services.

To run the whole thing exactly as the cloud does — one container, PostgreSQL, SPA served by the
backend:

```pwsh
docker compose up --build        # http://localhost:8000
```

## Tests

```pwsh
.\.venv\Scripts\python.exe -m pytest backend/tests -q
npm --prefix frontend run build  # tsc -b + vite; catches types the dev server tolerates
npm --prefix frontend run lint
```

The suite runs against in-memory SQLite by default. Before changing anything that touches the
database, run it against PostgreSQL too — that is what the Azure deployment uses:

```pwsh
docker run -d --name pgtest -e POSTGRES_USER=azureops -e POSTGRES_PASSWORD=azureops `
  -e POSTGRES_DB=azureops -p 55432:5432 postgres:16-alpine
$env:TEST_DATABASE_URL='postgresql+asyncpg://azureops:azureops@127.0.0.1:55432/azureops'
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Both engines must pass.

## House rules

- **Light mode only.** Do not add a dark theme or dark-mode variants.
- **Keep scheduling single-replica** while SQLite and the in-process scheduler are in use.
- **Never weaken a safety gate by default.** Real starts and real stops stay off unless an operator
  turns them on, and each remains ANDed with the per-tenant permission.
- Comments should explain *why*, not restate the code.
- Prefer editing existing files over adding new ones; update `README.md` rather than adding docs.

## Changing the deployment template

`deploy/main.json` is generated — never edit it by hand:

```pwsh
az bicep build --file deploy\main.bicep --outfile deploy\main.json
```

Commit both files together, since the Deploy to Azure button reads the JSON.
