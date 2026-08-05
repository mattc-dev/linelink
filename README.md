# Linelink v0.1.0

A lightweight execution engine that monitors Gmail for invoice PDFs, extracts financial data using a local Ollama model, and automatically creates Purchase entries in QuickBooks Online.

## Pipeline Architecture
`Gmail Inbox` ➔ `PDF Attachment Parser` ➔ `Ollama (gemma4:e4b)` ➔ `QuickBooks API`

## Prerequisites
- Python 3.10+
- [Ollama](https://ollama.com/) running locally with the `gemma4:e4b` model installed (`ollama run gemma4:e4b`)
- Google Cloud OAuth Credentials (`client_data.json`) with Gmail read permissions
- QuickBooks Developer Sandbox App (`quickbooks.json`)

## Required Config Files
Create `quickbooks.json` in the project root:
```json
{
  "client_id": "YOUR_QB_CLIENT_ID",
  "client_secret": "YOUR_QB_CLIENT_SECRET",
  "sandbox_id": "YOUR_QB_SANDBOX_COMPANY_ID"
}
```

## Quickstart
1. Install dependencies:
```bash
pip install google-api-python-client google-auth-oauthlib intuit-oauth pypdf requests
```

2. Run the pipeline:
```bash
python main.py
```