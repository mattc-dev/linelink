# Linelink v0.1.3

A lightweight execution engine that monitors Gmail for invoices (PDF or DOCX attachments, or plain email body text), extracts financial data using a local Ollama model, and automatically creates Purchase entries in QuickBooks Online.

## Pipeline Architecture
`Gmail Inbox` ➔ `PDF/DOCX Attachment or Email Body Parser` ➔ `Ollama (gemma4:e4b)` ➔ `QuickBooks API`

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

Create `query.txt` in the project root, containing the Gmail search query to run (plain text, no quotes needed):
```
label:TestingLinelink/My Invoices (subject:invoice OR subject:bill OR has:attachment)
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

## Notes on Extraction
- Supported invoice sources per email, checked in order: `.pdf` attachment, `.docx` attachment, then email body text (plain text or HTML) if no attachment yields usable text.
- Invoices with no discoverable date are skipped rather than defaulted to today's date. Check `errors.log` if fewer purchases are being created than expected.
- Failed Ollama parses and QuickBooks uploads are retried automatically (up to `MAX_RETRIES`, currently 3) before being logged to `errors.log`.