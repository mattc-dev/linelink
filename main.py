import base64
import http.server
import json
import mimetypes
import os
import random
import re
import shutil
import socketserver
import time
import urllib.parse
import warnings
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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import requests

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
PROCESSED_FILE = "processed.json"
FAILED_FILE = "failed.json"


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


def extract_url_from_text(text: str) -> str | None:
    """Finds the first http(s) URL in a block of plaintext, if any."""
    match = re.search(r'https?://[^\s<>"\')\]]+', text)
    if not match:
        return None
    return match.group(0).rstrip('.,;:')


def _sniff_extension_from_bytes(data: bytes) -> str | None:
    """Identifies a file's real type from its signature bytes, regardless of what a
    URL/Content-Type/filename claims it is."""
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".docx"
    return None


def _download_file_via_request(url: str) -> str | None:
    """Attempts a direct HTTP GET download of the invoice URL. Returns the local filename
    on success, or None if the URL doesn't serve a file directly (e.g. it's a viewer page)."""
    try:
        res = requests.get(url, timeout=20, allow_redirects=True)
        if res.status_code != 200 or not res.content:
            print(f"Failed to download invoice from URL {url}: HTTP {res.status_code}")
            return None

        content = res.content
        # Trust the actual file signature, not the URL/Content-Type header, since a link
        # can redirect to an HTML landing/login/viewer page instead of the real file.
        ext = _sniff_extension_from_bytes(content)
        if not ext:
            print(f"URL {url} did not return a recognizable PDF or DOCX file directly; will try a browser")
            return None

        content_disposition = res.headers.get("Content-Disposition", "")
        filename = None
        cd_match = re.search(r'filename="?([^";]+)"?', content_disposition)
        if cd_match:
            filename = os.path.basename(cd_match.group(1))
        if not filename:
            filename = os.path.basename(urllib.parse.urlparse(url).path) or "downloaded_invoice"
        if os.path.splitext(filename)[1].lower() != ext:
            filename = os.path.splitext(filename)[0] + ext

        with open(filename, "wb") as f:
            f.write(content)
        return filename
    except requests.RequestException as e:
        print(f"Failed to download invoice from URL {url}: {e}")
        return None


def _download_file_via_browser(url: str) -> str | None:
    """Falls back to a headless Chrome session for invoice links that only render a viewer
    page, clicking through 'Actions' -> 'Download as PDF' to trigger the real download."""
    download_dir = os.path.abspath("browser_downloads")
    os.makedirs(download_dir, exist_ok=True)
    existing_files = set(os.listdir(download_dir))

    warnings.filterwarnings("ignore")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True,
    })

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.get(url)

        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, "//*[normalize-space()='Actions']"))
        ).click()
        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, "//*[normalize-space()='Download as PDF']"))
        ).click()

        downloaded_path = None
        for _ in range(20):
            time.sleep(1)
            candidates = [
                f for f in os.listdir(download_dir)
                if f not in existing_files and not f.endswith(".crdownload")
            ]
            if candidates:
                downloaded_path = os.path.join(download_dir, candidates[0])
                break

        if not downloaded_path:
            print(f"Timed out waiting for browser download from {url}")
            return None

        with open(downloaded_path, "rb") as fh:
            header = fh.read(16)
        ext = _sniff_extension_from_bytes(header)
        if not ext:
            print(f"Browser download from {url} was not a recognizable PDF or DOCX file")
            os.remove(downloaded_path)
            return None

        final_name = f"downloaded_invoice{ext}"
        counter = 1
        while os.path.exists(final_name):
            final_name = f"downloaded_invoice_{counter}{ext}"
            counter += 1
        shutil.move(downloaded_path, final_name)
        return final_name
    except Exception as e:
        print(f"Headless browser download failed for {url}: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def download_file_from_url(url: str) -> str | None:
    """Follows a URL found in a plaintext invoice email and downloads the file it points to.
    Tries a direct HTTP GET first; if the URL only serves a viewer page (download requires
    clicking through buttons), falls back to a headless browser. Returns the local filename
    on success, or None on failure."""
    filepath = _download_file_via_request(url)
    if filepath:
        return filepath
    return _download_file_via_browser(url)


def extract_text_from_file(filepath: str) -> str:
    """Extracts text from a local .pdf or .docx file on disk. Returns "" if the file is
    missing, corrupt, or otherwise unreadable, rather than raising."""
    ext = os.path.splitext(filepath)[1].lower()
    text = ""
    try:
        if ext == ".pdf":
            reader = PdfReader(filepath)
            for page in reader.pages:
                text += page.extract_text() or ""
        elif ext == ".docx":
            text = extract_text_from_docx(filepath)
    except Exception as e:
        print(f"Failed to extract text from {filepath}: {e}")
        return ""
    return text


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


def normalize_sender(sender: str) -> str:
    """Extracts and lowercases the email address portion of a From header, dropping the display name."""
    match = re.search(r'<([^>]+)>', sender)
    email = match.group(1) if match else sender
    return email.strip().lower()


def normalize_payee(payee: str) -> str:
    """Normalizes a payee/vendor name: collapses whitespace, strips trailing punctuation, and title-cases it."""
    cleaned = " ".join(payee.split()).strip(" .,-")
    return cleaned.title() if cleaned else cleaned


def load_processed(path: str = PROCESSED_FILE) -> list[dict]:
    """Loads the list of already-processed invoice fingerprints. Returns [] if missing or unreadable."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not read {path} ({e}); starting with empty processed list")
        return []


def save_processed(records: list[dict], path: str = PROCESSED_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def is_duplicate(processed: list[dict], sender: str, txn_date: str, amount: float) -> bool:
    """Checks whether an invoice with the same (sender, txn_date, amount) fingerprint was already processed."""
    norm_sender = normalize_sender(sender)
    rounded_amount = round(float(amount), 2)
    return any(
        r.get("sender") == norm_sender
        and r.get("txn_date") == txn_date
        and r.get("amount") == rounded_amount
        for r in processed
    )


def record_processed(processed: list[dict], sender: str, txn_date: str, amount: float,
                      doc_number: str, payee: str, message_id: str) -> None:
    """Appends a new fingerprint to the in-memory list and persists it to disk immediately."""
    processed.append({
        "sender": normalize_sender(sender),
        "txn_date": txn_date,
        "amount": round(float(amount), 2),
        "doc_number": doc_number,
        "payee": payee,
        "message_id": message_id,
    })
    save_processed(processed)


def load_failed(path: str = FAILED_FILE) -> list[dict]:
    """Loads the list of invoices that previously failed to transfer. Returns [] if missing or unreadable."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not read {path} ({e}); starting with empty failed list")
        return []


def save_failed(records: list[dict], path: str = FAILED_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def record_failed(failed: list[dict], source_label: str, message_id: str, attachment_id: str, reason: str) -> None:
    """Appends a new failure entry to the in-memory list and persists it to disk immediately."""
    failed.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source_label,
        "message_id": message_id,
        "attachment_id": attachment_id,
        "reason": reason,
    })
    save_failed(failed)


def parse_with_ollama(text: str) -> dict | None:
    prompt = f"""
    Analyze the raw invoice text below and extract core financial fields into JSON.

    Required JSON keys:
    - "amount": numerical float (e.g. 250.00)
    - "doc_number": exact invoice or reference number string (e.g. "INV-1092", "84920"). Do NOT extract labels, prepositions, or headers like "From", "To", "Invoice", "No", "Ref".
    - "txn_date": invoice date or due date formatted strictly as YYYY-MM-DD (e.g., convert "Feb 06 2026" or "06/02/2026" to "2026-02-06").
    - "payee": the name of the company or person who should be paid (i.e. the vendor issuing this invoice), e.g. "John Doe" or "Victoria Repairs Ltd". It will often be a name. Do not state an activity like "Decorating". It should be a proper noun. Do NOT extract labels like "Payee", "Vendor", "Bill From", or "Company Name" as the value itself.

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

                        # Payee extraction: ollama -> regex fallback -> generated placeholder (mirrors doc_number)
                        raw_payee = str(parsed.get("payee") or "").strip()
                        payee_blacklist = {"payee", "vendor", "company", "biller", "bill from",
                                            "unknown", "n/a", "na", "none", "null", ""}
                        if raw_payee.lower() in payee_blacklist or len(raw_payee) < 2:
                            payee_match = re.search(
                                r'(?:Payee|Vendor|Pay\s*to|Bill\s*From)\s*[:\-]\s*([A-Za-z0-9&.,\'\- ]{2,60})',
                                text, re.IGNORECASE
                            )
                            raw_payee = payee_match.group(1).strip() if payee_match else f"UNKNOWN-PAYEE-{random.randint(1000, 9999)}"
                        payee = normalize_payee(raw_payee)

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
                            return {"amount": amount, "doc_number": doc_no, "txn_date": txn_date, "payee": payee}
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


def attach_file_to_quickbooks(base_url: str, access_token: str, purchase_id: str,
                               filepath: str, filename: str) -> str | None:
    """Uploads a local file and links it as an attachment on the given QuickBooks Purchase record.
    Returns an error string on failure, or None on success."""
    url = f"{base_url}/upload"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    metadata = {
        "AttachableRef": [{"EntityRef": {"type": "Purchase", "value": purchase_id}}],
        "FileName": filename,
        "ContentType": content_type,
    }
    upload_headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(filepath, "rb") as fh:
                files = {
                    "file_metadata_01": ("attachment.json", json.dumps(metadata), "application/json"),
                    "file_content_01": (filename, fh, content_type),
                }
                res = requests.post(url, headers=upload_headers, files=files, timeout=30)
            if res.status_code == 200:
                return None
            if 400 <= res.status_code < 500:
                return f"QuickBooks attachment HTTP {res.status_code} (Non-retryable): {res.text}"
            last_err = f"HTTP {res.status_code}: {res.text}"
        except (requests.RequestException, OSError) as e:
            last_err = str(e)
        print(f"[QB Attach Retry {attempt}/{MAX_RETRIES}] Failed: {last_err}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return f"QuickBooks attachment upload failed after {MAX_RETRIES} attempts. Last error: {last_err}"


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

processed = load_processed()
failed = load_failed()
print(f"Loaded {len(processed)} previously processed invoice fingerprints")

new_success_count = 0
new_failed_count = 0

for item in messages:
    message_id = item["id"]
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    msg_headers = message["payload"]["headers"]

    subject = next((h["value"] for h in msg_headers if h["name"].lower() == "subject"), "No subject")
    sender = next((h["value"] for h in msg_headers if h["name"].lower() == "from"), "Unknown sender")

    print(f"\nProcessing ID: {message_id}\nFrom: {sender}\nSubject: {subject}")

    # Each entry: (source_label, text_content, attachment_id, local_filepath_to_send_along_or_None)
    processed_payloads = []
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

                text = extract_text_from_file(filename)

                if text.strip():
                    # Keep the file on disk so it can be sent along as a QuickBooks attachment later.
                    processed_payloads.append((filename, text, attachment_id, filename))
                elif os.path.exists(filename):
                    os.remove(filename)

    # Step 2: Fall back to Email Body text if no valid attachments were processed
    if not processed_payloads:
        body_text = get_email_body(message["payload"])
        if body_text.strip():
            downloaded_filename = None
            invoice_url = extract_url_from_text(body_text)
            if invoice_url:
                downloaded_filename = download_file_from_url(invoice_url)

            if downloaded_filename:
                downloaded_text = extract_text_from_file(downloaded_filename)
                if downloaded_text.strip():
                    processed_payloads.append((downloaded_filename, downloaded_text, "N/A", downloaded_filename))
                else:
                    if os.path.exists(downloaded_filename):
                        os.remove(downloaded_filename)
                    processed_payloads.append(("Email Body", body_text, "N/A", None))
            else:
                processed_payloads.append(("Email Body", body_text, "N/A", None))

    # Step 3: Run extracted text through Ollama and upload to QuickBooks
    for source_label, text_content, attachment_id, filepath in processed_payloads:
        print(f"--- Processing Source: {source_label} ---")
        try:
            parsed_data = parse_with_ollama(text_content)

            if not parsed_data:
                err_msg = f"Failed to extract valid invoice fields from {source_label} after {MAX_RETRIES} attempts"
                print(f"SKIP: {source_label} - {err_msg}")
                log_error(source_label, message_id, attachment_id, err_msg)
                record_failed(failed, source_label, message_id, attachment_id, err_msg)
                new_failed_count += 1
                continue

            print(f"Extracted -> Date: {parsed_data['txn_date']}, Doc#: {parsed_data['doc_number']}, "
                  f"Amount: ${parsed_data['amount']}, Payee: {parsed_data['payee']}")

            if is_duplicate(processed, sender, parsed_data["txn_date"], parsed_data["amount"]):
                print(f"SKIP: {source_label} - duplicate invoice already processed "
                      f"(sender={normalize_sender(sender)}, date={parsed_data['txn_date']}, amount={parsed_data['amount']})")
                continue

            payload = {
                "PaymentType": "Cash",
                "TxnDate": parsed_data["txn_date"],
                "DocNumber": parsed_data["doc_number"],
                "AccountRef": {"value": bank_acc_id},
                "Line": [{
                    "Amount": parsed_data["amount"],
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Description": f"Parsed Invoice {parsed_data['doc_number']} from {parsed_data['payee']} via Linelink ({source_label})",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": expense_acc_id}
                    }
                }]
            }

            qb_res, qb_err = upload_to_quickbooks(base_url, headers, payload)
            if qb_err:
                print(f"SKIP: {source_label} - {qb_err}")
                log_error(source_label, message_id, attachment_id, qb_err)
                record_failed(failed, source_label, message_id, attachment_id, qb_err)
                new_failed_count += 1
                continue

            try:
                purchase_id = qb_res.json()["Purchase"]["Id"]
                print(f"SUCCESS: Created QB Purchase ID: {purchase_id}")
                record_processed(processed, sender, parsed_data["txn_date"], parsed_data["amount"],
                                  parsed_data["doc_number"], parsed_data["payee"], message_id)
                new_success_count += 1

                # Send the source document along as an attachment on the new Purchase record.
                if filepath and os.path.exists(filepath):
                    attach_err = attach_file_to_quickbooks(
                        base_url, auth_client.access_token, purchase_id,
                        filepath, os.path.basename(filepath)
                    )
                    if attach_err:
                        print(f"WARNING: Failed to attach {filepath} to Purchase {purchase_id}: {attach_err}")
                        log_error(source_label, message_id, attachment_id, f"Attachment upload failed: {attach_err}")
                    else:
                        print(f"Attached {os.path.basename(filepath)} to Purchase {purchase_id}")
            except (KeyError, ValueError) as e:
                unparseable_reason = f"QB returned 200 but response was unparseable: {e}"
                log_error(source_label, message_id, attachment_id, unparseable_reason)
                record_failed(failed, source_label, message_id, attachment_id, unparseable_reason)
                new_failed_count += 1
        except Exception as e:
            # Safety net: an unexpected error on one invoice (e.g. while attaching its file)
            # should not stop the rest of the batch from being processed.
            err_msg = f"Unexpected error while processing {source_label}: {e}"
            print(f"SKIP: {source_label} - {err_msg}")
            log_error(source_label, message_id, attachment_id, err_msg)
            record_failed(failed, source_label, message_id, attachment_id, err_msg)
            new_failed_count += 1
        finally:
            # Clean up the local copy only after we're done trying to send it along.
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

print(f"\nRun complete: {new_success_count} new invoice(s) processed and uploaded, "
      f"{new_failed_count} failed to transfer.")
print(f"Successful invoices recorded in {PROCESSED_FILE}; failures recorded in {FAILED_FILE} and errors.log.")