# Linelink v0.1.4

A lightweight execution engine that monitors Gmail for invoices (PDF or DOCX attachments, or plain email body text), extracts financial data using a local Ollama model, and automatically creates Purchase entries in QuickBooks Online with the source document attached to each entry.

## Pipeline Architecture
`Gmail Inbox` ➔ `PDF/DOCX Attachment, Linked Invoice, or Email Body Parser` ➔ `Ollama (gemma4:e4b)` ➔ `QuickBooks API` ➔ `Source Document Attached to Purchase Record`

## Prerequisites
- Python 3.10+
- [Ollama](https://ollama.com/) running locally with the `gemma4:e4b` model installed (`ollama run gemma4:e4b`)
- Google Cloud OAuth Credentials (`client_data.json`) with Gmail read permissions
- QuickBooks Developer Sandbox App (`quickbooks.json`)
- Google Chrome installed, for the headless-browser fallback used when an invoice link only serves a viewer page (see [Notes on Extraction](#notes-on-extraction))

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
pip install google-api-python-client google-auth-oauthlib intuit-oauth pypdf requests selenium
```

2. Run the pipeline:
```bash
python main.py
```

## Notes on Extraction
- Supported invoice sources per email, checked in order: `.pdf` attachment, `.docx` attachment, then email body text (plain text or HTML) if no attachment yields usable text.
- If the email body contains a URL and no attachment was usable, Linelink follows it to find the invoice: it first tries a direct download, and if the link only serves a viewer page, it falls back to a headless Chrome session that clicks through `Actions` → `Download as PDF`. Downloaded content is validated by its actual file signature, not by the URL or server-reported content type, before it's treated as an invoice.
- Invoices with no discoverable date are skipped rather than defaulted to today's date. Check `errors.log` if fewer purchases are being created than expected.
- Failed Ollama parses and QuickBooks uploads are retried automatically (up to `MAX_RETRIES`, currently 3) before being logged to `errors.log`.
- An error on one invoice (a bad attachment, a failed extraction, etc.) is logged and skipped; it won't stop the rest of the batch from being processed.

## Notes on Attachments
- The source document (the original attachment, or the file downloaded from a link in the email body) is kept on disk only for as long as it's needed, then uploaded and linked to the corresponding QuickBooks Purchase record as an attachment, and deleted locally.
- This applies whenever a local file exists for the invoice; invoices sourced purely from email body text with no linked file have no document to attach.
- If the QuickBooks attachment upload fails, the Purchase record itself is still created. The failure is logged to `errors.log` as a warning rather than counted as a failed invoice, since the financial entry was still successfully recorded.