import base64
import http.server
import json
import os
import random
import re
import socketserver
import time
import urllib.parse
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from pypdf import PdfReader
import requests

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text = []

    def handle_data(self, data):
        self._text.append(data)

    def get_text(self) -> str:
        return " ".join("".join(self._text).split())


def extract_text_from_html(html_str: str) -> str:
    """Strips HTML tags and extracts plain text content."""
    parser = HTMLTextExtractor()
    parser.feed(html_str)
    return parser.get_text()


def extract_text_from_docx(filepath: str) -> str:
    """Extracts plain text directly from a .docx file using standard library zip/xml."""
    try:
        with zipfile.ZipFile(filepath) as z:
            xml_content = z.read("word/document.xml")
        tree = ET.fromstring(xml_content)
        texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
        return " ".join(texts)
    except Exception as e:
        print(f"Failed to extract DOCX text from {filepath}: {e}")
        return ""


def get_email_body(payload: dict) -> str:
    """Recursively walks Gmail payload to extract plain text or HTML body text."""
    if "parts" in payload:
        for part in payload["parts"]:
            body = get_email_body(part)
            if body:
                return body
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if body_data:
        decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        if mime_type == "text/plain":
            return decoded
        elif mime_type == "text/html":
            return extract_text_from_html(decoded)
    return ""


def log_error(filename: str, message_id: str, attachment_id: str, reason: str):
    """Appends error details with a timestamp to errors.log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = (
        f"[{timestamp}] Msg ID: {message_id} | "
        f"Attachment ID: {attachment_id} | "
        f"File: {filename} | "
        f"Reason: {reason}\n"
    )
    with open("errors.log", "a", encoding="utf-8") as f:
        f.write(log_entry)


def parse_with_ollama(text: str) -> dict | None:
    prompt = f"""
    Analyze the raw invoice text below and extract core financial fields into JSON.

    Required JSON keys:
    - "amount": numerical float (e.g. 250.00)
    - "doc_number": exact invoice or reference number string (e.g. "INV-1092", "84920"). Do NOT extract labels, prepositions, or headers like "From", "To", "Invoice", "No", "Ref".
    - "txn_date": invoice date or due date formatted strictly as YYYY-MM-DD (e.g., convert "Feb 06 2026" or "06/02/2026" to "2026-02-06").

    If a value cannot be found in the text, set that field to null. Return strictly JSON.

    Raw Invoice Text:
    {text}
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": "gemma4:e4b", "prompt": prompt, "format": "json", "stream": False},
                timeout=120
            )
            if response.status_code == 200:
                parsed = json.loads(response.json()["response"])
                raw_amount = parsed.get("amount")
                if raw_amount is None:
                    last_err = "Ollama returned null amount"
                else:
                    try:
                        amount = float(raw_amount)
                    except (ValueError, TypeError):
                        last_err = f"Invalid amount format: {raw_amount}"
                        amount = None

                    if amount is not None:
                        doc_no = str(parsed.get("doc_number") or "").strip()
                        blacklist = {"from", "to", "invoice", "inv", "bill", "no", "number", "ref", "none", "null", ""}
                        if doc_no.lower() in blacklist or len(doc_no) < 2:
                            alt_match = re.search(r'(?:INV|INV-|\b#)\s*([A-Za-z0-9-]+)', text, re.IGNORECASE)
                            doc_no = alt_match.group(1) if alt_match else f"INV-{random.randint(1000, 9999)}"

                        # Multi-format date extraction without ungrounded fallback
                        raw_date = str(parsed.get("txn_date") or "").strip()
                        txn_date = None
                        if raw_date and raw_date.lower() != "null":
                            for date_fmt in ("%Y-%m-%d", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%d/%m/%Y"):
                                try:
                                    dt = datetime.strptime(raw_date.replace(",", ""), date_fmt)
                                    txn_date = dt.strftime("%Y-%m-%d")
                                    break
                                except ValueError:
                                    pass

                        if not txn_date:
                            date_patterns = [
                                (r'\b(\d{4}-\d{2}-\d{2})\b', "%Y-%m-%d"),
                                (r'\b(\d{1,2}/\d{1,2}/\d{4})\b', "%m/%d/%Y"),
                                (r'\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b', "%b %d %Y"),
                                (r'\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b', "%d %b %Y"),
                            ]
                            for pat, fmt in date_patterns:
                                m = re.search(pat, text, re.IGNORECASE)
                                if m:
                                    d_str = m.group(1).replace(",", "")
                                    for f in (fmt, fmt.replace("%b", "%B")):
                                        try:
                                            dt = datetime.strptime(d_str, f)
                                            txn_date = dt.strftime("%Y-%m-%d")
                                            break
                                        except ValueError:
                                            pass
                                    if txn_date:
                                        break

                        if not txn_date:
                            last_err = "Missing or unparseable transaction/due date"
                        else:
                            return {"amount": amount, "doc_number": doc_no, "txn_date": txn_date}
            else:
                last_err = f"Ollama HTTP {response.status_code}: {response.text}"
        except Exception as e:
            last_err = str(e)
        print(f"[Ollama Retry {attempt}/{MAX_RETRIES}] Failed: {last_err}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def upload_to_quickbooks(base_url: str, headers: dict, payload: dict) -> tuple[requests.Response | None, str | None]:
    url = f"{base_url}/purchase"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                return res, None
            if 400 <= res.status_code < 500:
                return res, f"QuickBooks HTTP {res.status_code} (Non-retryable): {res.text}"
            last_err = f"HTTP {res.status_code}: {res.text}"
        except requests.RequestException as e:
            last_err = str(e)
        print(f"[QB Retry {attempt}/{MAX_RETRIES}] Failed: {last_err}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None, f"QuickBooks upload failed after {MAX_RETRIES} attempts. Last error: {last_err}"


# QuickBooks Authentication & Setup
with open("quickbooks.json", "r") as f:
    qb_config = json.load(f)

auth_client = AuthClient(
    client_id=qb_config["client_id"],
    client_secret=qb_config["client_secret"],
    environment="sandbox",
    redirect_uri="http://localhost:8000/callback"
)

qb_tokens_file = "qb_tokens.json"
if os.path.exists(qb_tokens_file):
    with open(qb_tokens_file, "r") as f:
        qb_tokens = json.load(f)
        if qb_tokens.get("refresh_token"):
            try:
                auth_client.refresh(refresh_token=qb_tokens["refresh_token"])
            except Exception as e:
                print(f"QuickBooks token refresh failed, re-authenticating... ({e})")

if not auth_client.access_token:
    class CallbackHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            code = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("code", [None])[0]
            self.server.auth_code = code
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Quickbooks Authenticated")

    webbrowser.open(auth_client.get_authorization_url([Scopes.ACCOUNTING]))
    with socketserver.TCPServer(("localhost", 8000), CallbackHandler) as httpd:
        httpd.auth_code = None
        httpd.handle_request()
        auth_code = httpd.auth_code

    auth_client.get_bearer_token(auth_code, realm_id=str(qb_config["sandbox_id"]))

with open(qb_tokens_file, "w") as f:
    json.dump({
        "access_token": auth_client.access_token,
        "refresh_token": auth_client.refresh_token
    }, f)

realm_id = qb_config["sandbox_id"]
base_url = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{realm_id}"
headers = {
    "Authorization": f"Bearer {auth_client.access_token}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

bank_q = urllib.parse.quote("select * from Account where AccountType='Bank' maxresults 1")
bank_res = requests.get(f"{base_url}/query?query={bank_q}", headers=headers).json()
bank_acc_id = bank_res["QueryResponse"]["Account"][0]["Id"]

exp_q = urllib.parse.quote("select * from Account where AccountType='Expense' maxresults 1")
exp_res = requests.get(f"{base_url}/query?query={exp_q}", headers=headers).json()
expense_acc_id = exp_res["QueryResponse"]["Account"][0]["Id"]

print("QuickBooks Setup Complete & Ready.")

# Gmail Setup
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
credentials = Credentials.from_authorized_user_file("token.json", SCOPES) if os.path.exists("token.json") else None

if not credentials or not credentials.valid:
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file("client_data.json", SCOPES)
        credentials = flow.run_local_server(port=0)
    with open("token.json", "w") as token_file:
        token_file.write(credentials.to_json())

service = build("gmail", "v1", credentials=credentials)

with open("query.txt", "r") as f:
    query = f.read()
    
print(f"Query: {query}")
response = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
messages = response.get("messages", [])
print(f"Found {len(messages)} messages")

for item in messages:
    message_id = item["id"]
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    msg_headers = message["payload"]["headers"]

    subject = next((h["value"] for h in msg_headers if h["name"].lower() == "subject"), "No subject")
    sender = next((h["value"] for h in msg_headers if h["name"].lower() == "from"), "Unknown sender")

    print(f"\nProcessing ID: {message_id}\nFrom: {sender}\nSubject: {subject}")

    processed_payloads = []  # list of tuples: (source_label, text_content, attachment_id)
    parts = message.get("payload", {}).get("parts", [])

    # Step 1: Check for supported file attachments (.pdf, .docx)
    for part in parts:
        filename = part.get("filename", "")
        attachment_id = part.get("body", {}).get("attachmentId")

        if filename and attachment_id:
            ext = os.path.splitext(filename)[1].lower()
            if ext in [".pdf", ".docx"]:
                attachment = service.users().messages().attachments().get(
                    userId="me", messageId=message_id, id=attachment_id
                ).execute()

                file_data = base64.urlsafe_b64decode(attachment["data"])
                with open(filename, "wb") as f:
                    f.write(file_data)

                text = ""
                try:
                    if ext == ".pdf":
                        reader = PdfReader(filename)
                        for page in reader.pages:
                            text += page.extract_text() or ""
                    elif ext == ".docx":
                        text = extract_text_from_docx(filename)
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)

                if text.strip():
                    processed_payloads.append((filename, text, attachment_id))

    # Step 2: Fall back to Email Body text if no valid attachments were processed
    if not processed_payloads:
        body_text = get_email_body(message["payload"])
        if body_text.strip():
            processed_payloads.append(("Email Body", body_text, "N/A"))

    # Step 3: Run extracted text through Ollama and upload to QuickBooks
    for source_label, text_content, attachment_id in processed_payloads:
        print(f"--- Processing Source: {source_label} ---")
        parsed_data = parse_with_ollama(text_content)

        if not parsed_data:
            err_msg = f"Failed to extract valid invoice fields from {source_label} after {MAX_RETRIES} attempts"
            print(f"SKIP: {source_label} - {err_msg}")
            log_error(source_label, message_id, attachment_id, err_msg)
            continue

        print(f"Extracted -> Date: {parsed_data['txn_date']}, Doc#: {parsed_data['doc_number']}, Amount: ${parsed_data['amount']}")

        payload = {
            "PaymentType": "Cash",
            "TxnDate": parsed_data["txn_date"],
            "DocNumber": parsed_data["doc_number"],
            "AccountRef": {"value": bank_acc_id},
            "Line": [{
                "Amount": parsed_data["amount"],
                "DetailType": "AccountBasedExpenseLineDetail",
                "Description": f"Parsed Invoice {parsed_data['doc_number']} via Linelink ({source_label})",
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": expense_acc_id}
                }
            }]
        }

        qb_res, qb_err = upload_to_quickbooks(base_url, headers, payload)
        if qb_err:
            print(f"SKIP: {source_label} - {qb_err}")
            log_error(source_label, message_id, attachment_id, qb_err)
            continue

        try:
            print(f"SUCCESS: Created QB Purchase ID: {qb_res.json()['Purchase']['Id']}")
        except (KeyError, ValueError) as e:
            log_error(source_label, message_id, attachment_id, f"QB returned 200 but response was unparseable: {e}")