"""
============================================================================
TWO-WAY SYNCHRONIZATION: GOOGLE SHEETS <-> GOOGLE CONTACTS (LOCAL PYTHON CLIENT)
============================================================================
Candidate: Abhi Pandey
Technology Stack: Python 3, Google Sheets API v4, Google People API v1
============================================================================
This script runs locally on your computer to perform continuous, bidirectional,
autonomous synchronization between a Google Sheet and Google Contacts.
"""

import os
import re
import sys
import time
import argparse
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# OAuth Scopes required for full bidirectional sync
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/contacts'
]

# Sheet Columns Configuration
COL_NAME = 0        # Column A: Full Name
COL_PHONE = 1       # Column B: Phone Number
COL_CONTACT_ID = 2  # Column C: System Resource Name (e.g. people/c12345...)
COL_LAST_SYNC = 3   # Column D: Last Synced
COL_STATUS = 4      # Column E: Status

HEADERS = ['Full Name', 'Phone Number', 'Contact ID (System)', 'Last Synced', 'Sync Status']

SAMPLE_CONTACTS = [
    {'name': 'Aarav Sharma', 'phone': '+1 650 555 0101'},
    {'name': 'Diya Patel', 'phone': '+1 650 555 0102'},
    {'name': 'Rohan Iyer', 'phone': '+1 650 555 0103'},
    {'name': 'Ananya Verma', 'phone': '+1 650 555 0104'},
    {'name': 'Vikram Malhotra', 'phone': '+1 650 555 0105'},
    {'name': 'Pooja Nair', 'phone': '+1 650 555 0106'},
    {'name': 'Siddharth Rao', 'phone': '+1 650 555 0107'},
    {'name': 'Sneha Kulkarni', 'phone': '+1 650 555 0108'},
    {'name': 'Aditya Joshi', 'phone': '+1 650 555 0109'},
    {'name': 'Meera Deshmukh', 'phone': '+1 650 555 0110'}
]


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    symbols = {"INFO": "ℹ️ ", "SUCCESS": "✅", "WARN": "⚠️ ", "ERROR": "❌"}
    prefix = symbols.get(level, "• ")
    print(f"[{timestamp}] {prefix} {msg}")


def normalize_phone(phone_str):
    """Strip all non-digit characters for robust telephone comparison."""
    if not phone_str:
        return ""
    return re.sub(r'\D', '', str(phone_str))


def split_full_name(full_name):
    """Splits full name into givenName and familyName."""
    if not full_name:
        return {'givenName': 'Unnamed', 'familyName': ''}
    parts = full_name.strip().split()
    if len(parts) == 1:
        return {'givenName': parts[0], 'familyName': ''}
    return {'givenName': " ".join(parts[:-1]), 'familyName': parts[-1]}


class GoogleSyncClient:
    def __init__(self, spreadsheet_id=None, credentials_path="credentials.json", token_path="token.json"):
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.creds = None
        self.sheets_service = None
        self.people_service = None

    def authenticate(self):
        """Authenticates with Google OAuth 2.0."""
        if os.path.exists(self.token_path):
            self.creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                log("Refreshing expired OAuth token...", "INFO")
                self.creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_path):
                    log(f"credentials.json not found at '{self.credentials_path}'.", "ERROR")
                    log("Please download OAuth 2.0 Client ID credentials from Google Cloud Console.", "WARN")
                    sys.exit(1)
                log("Launching browser for one-time Google OAuth authorization...", "INFO")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                self.creds = flow.run_local_server(port=0)

            with open(self.token_path, 'w') as token_file:
                token_file.write(self.creds.to_json())
                log("OAuth token saved to token.json.", "SUCCESS")

        self.sheets_service = build('sheets', 'v4', credentials=self.creds)
        self.people_service = build('people', 'v1', credentials=self.creds)
        log("Successfully connected to Google Sheets API & Google People API.", "SUCCESS")

    def initialize_sheet(self, sheet_title="Contacts"):
        """Initializes header rows and columns."""
        log(f"Initializing sheet '{sheet_title}' with headers...", "INFO")
        body = {'values': [HEADERS]}
        range_name = f"{sheet_title}!A1:E1"
        self.sheets_service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        log("Headers initialized: " + ", ".join(HEADERS), "SUCCESS")

    def seed_initial_10_contacts(self, sheet_title="Contacts"):
        """Seeds the 10 initial required contacts into Google Sheet and syncs them."""
        log("Seeding initial 10 contacts into Google Sheet...", "INFO")
        rows = []
        for c in SAMPLE_CONTACTS:
            rows.append([c['name'], c['phone'], '', '', 'Pending Sync'])

        body = {'values': rows}
        range_name = f"{sheet_title}!A2:E11"
        self.sheets_service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()
        log("Successfully wrote 10 contacts to Google Sheet.", "SUCCESS")
        log("Triggering immediate synchronization to Google Contacts...", "INFO")
        self.sync_sheet_to_contacts(sheet_title)

    def get_sheet_rows(self, sheet_title="Contacts"):
        """Reads all rows from the spreadsheet."""
        range_name = f"{sheet_title}!A:E"
        result = self.sheets_service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=range_name
        ).execute()
        return result.get('values', [])

    def sync_sheet_to_contacts(self, sheet_title="Contacts"):
        """Reads rows from Google Sheets and creates/updates contacts in Google Contacts."""
        rows = self.get_sheet_rows(sheet_title)
        if len(rows) <= 1:
            log("No contact rows found in sheet.", "WARN")
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_count = 0
        updated_count = 0

        for idx, row in enumerate(rows[1:], start=2):
            name = row[COL_NAME].strip() if len(row) > COL_NAME else ""
            phone = row[COL_PHONE].strip() if len(row) > COL_PHONE else ""
            contact_id = row[COL_CONTACT_ID].strip() if len(row) > COL_CONTACT_ID else ""
            status = row[COL_STATUS].strip() if len(row) > COL_STATUS else ""

            if not name and not phone:
                continue

            if contact_id and status == "Synced":
                continue

            name_parts = split_full_name(name)

            # CASE 1: Contact doesn't exist -> CREATE
            if not contact_id:
                try:
                    payload = {
                        "names": [{
                            "givenName": name_parts['givenName'],
                            "familyName": name_parts['familyName'],
                            "displayName": name
                        }],
                        "phoneNumbers": [{"value": phone, "type": "mobile"}] if phone else []
                    }
                    created = self.people_service.people().createContact(body=payload).execute()
                    resource_name = created.get('resourceName', '')

                    # Update Sheet with System ID and Status
                    update_range = f"{sheet_title}!C{idx}:E{idx}"
                    update_body = {'values': [[resource_name, now_str, 'Synced']]}
                    self.sheets_service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=update_range,
                        valueInputOption='RAW',
                        body=update_body
                    ).execute()
                    created_count += 1
                    log(f"Created Contact in Google: {name} ({resource_name})", "SUCCESS")
                except Exception as e:
                    log(f"Error creating contact '{name}': {e}", "ERROR")

            # CASE 2: Contact exists -> UPDATE
            else:
                try:
                    existing = self.people_service.people().get(
                        resourceName=contact_id,
                        personFields='names,phoneNumbers,metadata'
                    ).execute()

                    update_payload = {
                        "resourceName": contact_id,
                        "etag": existing.get('etag'),
                        "names": [{
                            "givenName": name_parts['givenName'],
                            "familyName": name_parts['familyName'],
                            "displayName": name
                        }],
                        "phoneNumbers": [{"value": phone, "type": "mobile"}] if phone else []
                    }

                    self.people_service.people().updateContact(
                        resourceName=contact_id,
                        updatePersonFields='names,phoneNumbers',
                        body=update_payload
                    ).execute()

                    update_range = f"{sheet_title}!D{idx}:E{idx}"
                    update_body = {'values': [[now_str, 'Synced']]}
                    self.sheets_service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=update_range,
                        valueInputOption='RAW',
                        body=update_body
                    ).execute()
                    updated_count += 1
                    log(f"Updated Contact in Google: {name} ({contact_id})", "SUCCESS")
                except Exception as e:
                    log(f"Error updating contact '{name}': {e}", "ERROR")

        if created_count or updated_count:
            log(f"Sheet ➔ Contacts Sync: {created_count} created, {updated_count} updated.", "SUCCESS")

    def sync_contacts_to_sheet(self, sheet_title="Contacts"):
        """Fetches contacts from Google Contacts and adds/updates them in Google Sheets."""
        rows = self.get_sheet_rows(sheet_title)
        sheet_contact_map = {}

        for idx, row in enumerate(rows[1:], start=2):
            cid = row[COL_CONTACT_ID].strip() if len(row) > COL_CONTACT_ID else ""
            if cid:
                sheet_contact_map[cid] = {
                    'row': idx,
                    'name': row[COL_NAME].strip() if len(row) > COL_NAME else "",
                    'phone': row[COL_PHONE].strip() if len(row) > COL_PHONE else ""
                }

        # Fetch all connections from People API
        all_connections = []
        next_page_token = None
        while True:
            response = self.people_service.people().connections().list(
                resourceName='people/me',
                pageSize=100,
                personFields='names,phoneNumbers,metadata',
                pageToken=next_page_token,
                sortOrder='LAST_MODIFIED_DESCENDING'
            ).execute()
            connections = response.get('connections', [])
            all_connections.extend(connections)
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_rows = []
        added_count = 0
        updated_count = 0

        for contact in all_connections:
            resource_name = contact.get('resourceName')
            if not resource_name:
                continue

            names = contact.get('names', [])
            display_name = names[0].get('displayName', '') if names else ''
            phones = contact.get('phoneNumbers', [])
            phone_val = phones[0].get('value', '') if phones else ''

            if not display_name and not phone_val:
                continue

            # CASE 1: Contact added from Mobile / Web (NOT in sheet) -> Increment count
            if resource_name not in sheet_contact_map:
                new_rows.append([
                    display_name,
                    phone_val,
                    resource_name,
                    now_str,
                    'Synced (from Contacts)'
                ])
                added_count += 1
                log(f"New Contact from Mobile/Web detected: {display_name} ({phone_val})", "SUCCESS")
            # CASE 2: Contact exists in sheet -> Check for updates made on mobile
            else:
                existing_info = sheet_contact_map[resource_name]
                name_changed = display_name and display_name != existing_info['name']
                phone_changed = phone_val and normalize_phone(phone_val) != normalize_phone(existing_info['phone'])

                if name_changed or phone_changed:
                    row_idx = existing_info['row']
                    update_range = f"{sheet_title}!A{row_idx}:E{row_idx}"
                    update_body = {'values': [[
                        display_name,
                        phone_val,
                        resource_name,
                        now_str,
                        'Synced'
                    ]]}
                    self.sheets_service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=update_range,
                        valueInputOption='RAW',
                        body=update_body
                    ).execute()
                    updated_count += 1
                    log(f"Updated Sheet Row {row_idx} from mobile edit: {display_name}", "SUCCESS")

        if new_rows:
            self.sheets_service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_title}!A:E",
                valueInputOption='USER_ENTERED',
                insertDataOption='INSERT_ROWS',
                body={'values': new_rows}
            ).execute()
            log(f"Appended {len(new_rows)} new contact(s) to Google Sheet. (Count incremented!)", "SUCCESS")

        if added_count or updated_count:
            log(f"Contacts ➔ Sheet Sync: {added_count} added, {updated_count} updated.", "SUCCESS")

    def run_continuous_daemon(self, interval_seconds=15, sheet_title="Contacts"):
        """Continuous polling loop for true two-way synchronization."""
        log(f"Starting continuous bidirectional sync daemon (polling every {interval_seconds}s)...", "INFO")
        log("Press Ctrl+C to terminate the daemon.", "INFO")
        try:
            while True:
                self.sync_sheet_to_contacts(sheet_title)
                self.sync_contacts_to_sheet(sheet_title)
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            log("Sync daemon stopped by user.", "INFO")


def main():
    parser = argparse.ArgumentParser(description="Google Sheets <-> Google Contacts Local Sync Client")
    parser.add_argument("--sheet-id", help="Google Spreadsheet ID from sheet URL", default=os.getenv("SPREADSHEET_ID"))
    parser.add_argument("--init", action="store_true", help="Initialize headers in sheet")
    parser.add_argument("--seed10", action="store_true", help="Seed initial 10 contacts into sheet")
    parser.add_argument("--sync-sheet", action="store_true", help="Run Sheet ➔ Contacts sync once")
    parser.add_argument("--sync-contacts", action="store_true", help="Run Contacts ➔ Sheet sync once")
    parser.add_argument("--sync-all", action="store_true", help="Run full two-way sync once")
    parser.add_argument("--daemon", action="store_true", help="Run continuous background sync daemon")
    parser.add_argument("--interval", type=int, default=15, help="Daemon polling interval in seconds (default: 15)")

    args = parser.parse_args()

    print("=" * 75)
    print("  GOOGLE SHEETS <-> GOOGLE CONTACTS LOCAL SYNC ENGINE")
    print("  Author / Candidate: Abhi Pandey")
    print("=" * 75)

    if not args.sheet_id:
        print("\n⚠️  No Google Spreadsheet ID provided!")
        print("Please provide it via --sheet-id <ID> or set SPREADSHEET_ID environment variable.")
        print("Example: python sync_google_live.py --sheet-id 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms --daemon\n")
        return

    client = GoogleSyncClient(spreadsheet_id=args.sheet_id)
    client.authenticate()

    if args.init:
        client.initialize_sheet()
    if args.seed10:
        client.seed_initial_10_contacts()
    if args.sync_sheet:
        client.sync_sheet_to_contacts()
    if args.sync_contacts:
        client.sync_contacts_to_sheet()
    if args.sync_all:
        client.sync_sheet_to_contacts()
        client.sync_contacts_to_sheet()
    if args.daemon:
        client.run_continuous_daemon(interval_seconds=args.interval)

    if not any([args.init, args.seed10, args.sync_sheet, args.sync_contacts, args.sync_all, args.daemon]):
        log("No action specified. Running full two-way sync once...", "INFO")
        client.sync_sheet_to_contacts()
        client.sync_contacts_to_sheet()


if __name__ == "__main__":
    main()
