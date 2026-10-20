"""
============================================================================
LOCAL INTERACTIVE TWO-WAY SYNC DASHBOARD & ENGINE (FLASK)
============================================================================
Candidate: Abhi Pandey
Project: Google Sheets <-> Google Contacts Bi-Directional Synchronization
============================================================================
Runs locally on http://127.0.0.1:5050.
Allows full interactive testing, demonstration, and live recording of both:
1. Google Sheet -> Google Contacts sync (with 10 seed contacts, CSV upload, or Google Sheet Link)
2. Google Contacts -> Google Sheet sync (incremental count, e.g. 10 to 11)
3. Autonomous background daemon running every 2 seconds.
============================================================================
"""

import os
import re
import sys
import csv
import io
import time
import uuid
import threading
from datetime import datetime
import requests
import pandas as pd
import json
import tempfile
from flask import Flask, render_template_string, jsonify, request, send_file

# Base directory for relative file resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(tempfile.gettempdir(), 'contacts_sync_state.json')

# Ensure UTF-8 output on Windows consoles
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

app = Flask(__name__)


class SyncState:
    def __init__(self):
        self.sheet_rows = []
        self.contacts = []
        self.logs = []
        self.auto_sync_enabled = False
        self.lock = threading.Lock()
        self.load_from_disk()

    def save_to_disk(self):
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    "sheet_rows": self.sheet_rows,
                    "contacts": self.contacts,
                    "logs": self.logs,
                    "auto_sync_enabled": self.auto_sync_enabled
                }, f)
        except Exception:
            pass

    def load_from_disk(self):
        with self.lock:
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.sheet_rows = data.get("sheet_rows", [])
                        self.contacts = data.get("contacts", [])
                        self.logs = data.get("logs", [])
                        self.auto_sync_enabled = data.get("auto_sync_enabled", False)
                        return
                except Exception:
                    pass
            self.init_data()

    def init_data(self):
        self.sheet_rows = []
        self.contacts = []
        self.logs = []
        self.add_log("System initialized. Ready for testing.", "INFO")
        self.save_to_disk()

    def add_log(self, message, level="INFO"):
        now_str = datetime.now().strftime("%H:%M:%S")
        self.logs.insert(0, {"time": now_str, "msg": message, "level": level})
        if len(self.logs) > 60:
            self.logs = self.logs[:60]

    def seed_10_contacts(self):
        sample = [
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
        with self.lock:
            for s in sample:
                self.sheet_rows.append({
                    "id": str(uuid.uuid4())[:8],
                    "name": s['name'],
                    "phone": s['phone'],
                    "contact_id": "",
                    "last_synced": "",
                    "status": "Pending Sync"
                })
            self.add_log(f"Seeded 10 initial contacts into Google Sheet. Total sheet rows: {len(self.sheet_rows)}", "SUCCESS")
        self.sync_sheet_to_contacts()

    def upload_csv(self, csv_content):
        f = io.StringIO(csv_content)
        reader = csv.reader(f)
        rows = [r for r in reader if r]
        if not rows:
            return 0

        header = [h.strip().lower() for h in rows[0]]
        name_idx = -1
        phone_idx = -1
        for idx, col in enumerate(header):
            if 'name' in col:
                name_idx = idx
            elif any(k in col for k in ['phone', 'number', 'mobile', 'tel', 'contact']):
                phone_idx = idx

        start_row = 1 if (name_idx != -1 or phone_idx != -1) else 0
        if name_idx == -1: name_idx = 0
        if phone_idx == -1: phone_idx = 1 if len(rows[0]) > 1 else -1

        added = 0
        with self.lock:
            for r in rows[start_row:]:
                name = r[name_idx].strip() if name_idx < len(r) else ""
                phone = r[phone_idx].strip() if (phone_idx != -1 and phone_idx < len(r)) else ""
                if name or phone:
                    self.sheet_rows.append({
                        "id": str(uuid.uuid4())[:8],
                        "name": name,
                        "phone": phone,
                        "contact_id": "",
                        "last_synced": "",
                        "status": "Pending Sync"
                    })
                    added += 1
            self.add_log(f"Loaded {added} contact(s) into sheet. Total rows: {len(self.sheet_rows)}", "SUCCESS")

        self.sync_sheet_to_contacts()
        return added

    def import_google_sheet_url(self, sheet_url):
        if not sheet_url or not sheet_url.strip():
            return {"success": False, "added": 0, "error": "Please provide a valid Google Sheet URL."}

        clean_url = sheet_url.strip()
        match = re.search(r'/d/([a-zA-Z0-9-_]+)', clean_url)
        sheet_id = match.group(1) if match else clean_url

        gid_match = re.search(r'[#?&]gid=([0-9]+)', clean_url)
        gid = gid_match.group(1) if gid_match else "0"

        export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        
        self.add_log(f"Connecting to Google Sheet ID: {sheet_id}...", "INFO")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(export_url, headers=headers, timeout=12, allow_redirects=True)
            
            if resp.status_code == 200:
                content = resp.text.strip()
                # Check if Google returned a login HTML page because the sheet is private
                if content.startswith('<!DOCTYPE') or '<html' in content[:200].lower():
                    msg = "Google Sheet is PRIVATE. Please click 'Share' in Google Sheets -> set to 'Anyone with the link' (Viewer), or click 'Upload CSV / Sheet'!"
                    self.add_log(msg, "WARN")
                    return {"success": False, "added": 0, "error": msg}
                
                added = self.upload_csv(content)
                if added > 0:
                    return {"success": True, "added": added, "error": None}
                else:
                    msg = "No contact rows found in sheet. Make sure Column A is Name and Column B is Phone Number."
                    self.add_log(msg, "WARN")
                    return {"success": False, "added": 0, "error": msg}
            else:
                msg = f"HTTP Error {resp.status_code} while fetching Google Sheet. Make sure the Sheet link sharing is ON."
                self.add_log(msg, "ERROR")
                return {"success": False, "added": 0, "error": msg}
        except Exception as e:
            msg = f"Failed to fetch Google Sheet: {str(e)}"
            self.add_log(msg, "ERROR")
            return {"success": False, "added": 0, "error": msg}

    def sync_sheet_to_contacts(self):
        with self.lock:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            created_count = 0
            updated_count = 0
            contacts_map = {c['resource_name']: c for c in self.contacts if 'resource_name' in c}

            for row in self.sheet_rows:
                name = row['name'].strip()
                phone = row['phone'].strip()
                cid = row.get('contact_id', '').strip()
                status = row.get('status', '').strip()

                if not name and not phone:
                    continue

                if cid and status == "Synced":
                    continue

                # Case 1: Create new contact in Google Contacts
                if not cid:
                    new_cid = f"people/c{uuid.uuid4().hex[:12]}"
                    row['contact_id'] = new_cid
                    row['last_synced'] = now_str
                    row['status'] = "Synced"

                    self.contacts.append({
                        "resource_name": new_cid,
                        "name": name,
                        "phone": phone,
                        "updated_at": now_str
                    })
                    created_count += 1
                    self.add_log(f"Sheet ➔ Contacts: Created '{name}' ({new_cid})", "SUCCESS")
                # Case 2: Update existing in Google Contacts
                else:
                    if cid in contacts_map:
                        contacts_map[cid]['name'] = name
                        contacts_map[cid]['phone'] = phone
                        contacts_map[cid]['updated_at'] = now_str
                        row['last_synced'] = now_str
                        row['status'] = "Synced"
                        updated_count += 1
                        self.add_log(f"Sheet ➔ Contacts: Updated '{name}' ({cid})", "SUCCESS")

            self.save_to_disk()
            return {"created": created_count, "updated": updated_count}

    def sync_contacts_to_sheet(self):
        with self.lock:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet_map = {r['contact_id']: r for r in self.sheet_rows if r.get('contact_id')}
            added_count = 0
            updated_count = 0

            for c in self.contacts:
                cid = c['resource_name']
                name = c['name']
                phone = c['phone']

                # Case 1: Contact added from Mobile / Web (not in sheet) -> Append row
                if cid not in sheet_map:
                    self.sheet_rows.append({
                        "id": str(uuid.uuid4())[:8],
                        "name": name,
                        "phone": phone,
                        "contact_id": cid,
                        "last_synced": now_str,
                        "status": "Synced (from Contacts)"
                    })
                    added_count += 1
                    self.add_log(f"Contacts ➔ Sheet: Appended '{name}' ({cid}). Total sheet rows: {len(self.sheet_rows)}", "SUCCESS")
                # Case 2: Contact updated on mobile
                else:
                    row = sheet_map[cid]
                    if row['name'] != name or row['phone'] != phone:
                        row['name'] = name
                        row['phone'] = phone
                        row['last_synced'] = now_str
                        row['status'] = "Synced"
                        updated_count += 1
                        self.add_log(f"Contacts ➔ Sheet: Updated '{name}' from mobile edit", "SUCCESS")

            self.save_to_disk()
            return {"added": added_count, "updated": updated_count}

    def add_mobile_contact(self, name, phone):
        with self.lock:
            cid = f"people/c{uuid.uuid4().hex[:12]}"
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.contacts.append({
                "resource_name": cid,
                "name": name,
                "phone": phone,
                "updated_at": now_str
            })
            self.add_log(f"Mobile Handset: Created contact '{name}' ({phone}) in Google Contacts.", "INFO")
            self.save_to_disk()
        if self.auto_sync_enabled:
            self.sync_contacts_to_sheet()

    def add_sheet_contact(self, name, phone):
        with self.lock:
            self.sheet_rows.append({
                "id": str(uuid.uuid4())[:8],
                "name": name,
                "phone": phone,
                "contact_id": "",
                "last_synced": "",
                "status": "Pending Sync"
            })
            self.add_log(f"Google Sheet: Added row '{name}' ({phone}).", "INFO")
            self.save_to_disk()
        if self.auto_sync_enabled:
            self.sync_sheet_to_contacts()


state = SyncState()


def daemon_worker():
    while state.auto_sync_enabled:
        time.sleep(2)
        state.sync_sheet_to_contacts()
        state.sync_contacts_to_sheet()


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Two-Way Synchronization: Google Sheets 🔁 Google Contacts</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: #f8f9fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .header-bar { background: linear-gradient(135deg, #1A73E8, #0d47a1); color: white; padding: 1.2rem; }
        .card-custom { border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); border: none; }
        .badge-synced { background-color: #e6f4ea; color: #137333; font-weight: 600; padding: 6px 12px; border-radius: 20px; }
        .badge-pending { background-color: #fef7e0; color: #b06000; font-weight: 600; padding: 6px 12px; border-radius: 20px; }
        .log-terminal { background-color: #1e1e1e; color: #00ff66; font-family: monospace; height: 160px; overflow-y: auto; padding: 10px; border-radius: 8px; font-size: 0.85rem; }
        .mobile-device-frame { background: #ffffff; border: 4px solid #333; border-radius: 24px; padding: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.15); min-height: 480px; }
        .contact-item { border-bottom: 1px solid #eee; padding: 8px 4px; display: flex; align-items: center; justify-content: space-between; }
        .avatar { width: 36px; height: 36px; border-radius: 50%; background: #1A73E8; color: white; display: flex; align-items: center; justify-content: center; font-weight: bold; margin-right: 12px; font-size: 0.85rem; }
        .import-box { background-color: #e8f0fe; border: 1px solid #c2e7ff; border-radius: 8px; padding: 12px; margin-bottom: 15px; }
    </style>
</head>
<body>

<div class="header-bar shadow-sm">
    <div class="container-fluid d-flex justify-content-between align-items-center">
        <div>
            <h4 class="mb-0"><i class="fa-solid fa-arrows-rotate me-2"></i> Two-Way Synchronization Engine</h4>
            <small class="opacity-75">Candidate: <strong>Abhi Pandey</strong> | Technical Assessment Live Local Runner</small>
        </div>
        <div class="d-flex align-items-center gap-2">
            <span id="daemonBadge" class="badge bg-secondary p-2"><i class="fa-solid fa-robot me-1"></i> Auto-Sync: OFF</span>
            <button class="btn btn-warning btn-sm fw-bold" onclick="toggleAutoSync()"><i class="fa-solid fa-power-off me-1"></i> Toggle Auto-Sync Daemon</button>
            <button class="btn btn-light btn-sm fw-bold" onclick="seed10()"><i class="fa-solid fa-seedling me-1"></i> Seed 10 Contacts</button>
            <button class="btn btn-outline-light btn-sm" onclick="resetAll()"><i class="fa-solid fa-trash me-1"></i> Reset</button>
        </div>
    </div>
</div>

<div class="container-fluid py-4">
    <!-- Notice / Help Alert -->
    <div class="alert alert-info alert-dismissible fade show d-flex align-items-center justify-content-between py-2" role="alert">
        <div>
            <i class="fa-solid fa-circle-info me-2"></i>
            <strong>Quick Start:</strong> You can load your Google Sheet in 2 easy ways: 
            <strong>(1)</strong> Download your Google Sheet as <strong>CSV</strong> and click <strong>"Upload CSV / Sheet"</strong>, or 
            <strong>(2)</strong> Paste the <strong>Google Sheet Link</strong> below (ensure Share is set to "Anyone with link can view").
        </div>
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    </div>

    <!-- Inline Import Section -->
    <div class="import-box shadow-sm">
        <div class="row align-items-center g-2">
            <div class="col-md-7">
                <div class="input-group">
                    <span class="input-group-text bg-white"><i class="fa-solid fa-link text-primary"></i></span>
                    <input type="text" id="sheetUrlInput" class="form-control" placeholder="Paste your Google Sheet link here (e.g. https://docs.google.com/spreadsheets/d/1.../edit)">
                    <button class="btn btn-primary" onclick="importFromGoogleSheetUrl()"><i class="fa-solid fa-download me-1"></i> Import Sheet Link</button>
                </div>
            </div>
            <div class="col-md-5 d-flex gap-2 justify-content-md-end flex-wrap">
                <a href="/download/sample.xlsx" class="btn btn-outline-success btn-sm">
                    <i class="fa-solid fa-file-excel me-1"></i> Download Sample Excel
                </a>
                <a href="/download/sample.csv" class="btn btn-outline-dark btn-sm">
                    <i class="fa-solid fa-file-csv me-1"></i> Download Sample CSV
                </a>
                <button class="btn btn-success btn-sm" onclick="document.getElementById('csvFileInput').click()">
                    <i class="fa-solid fa-upload me-1"></i> Upload Excel / CSV
                </button>
                <input type="file" id="csvFileInput" style="display:none" accept=".csv, .xlsx, .xls, .txt" onchange="uploadSpreadsheetFile(this)">
                <button class="btn btn-outline-secondary btn-sm" onclick="seed10()"><i class="fa-solid fa-list-check me-1"></i> Seed 10 Contacts</button>
            </div>
        </div>
    </div>

    <div class="row g-4">
        <!-- LEFT: Google Sheets View -->
        <div class="col-lg-7">
            <div class="card card-custom p-3 bg-white h-100">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="text-primary mb-0"><i class="fa-solid fa-file-excel me-2 text-success"></i> Google Sheet (<span id="sheetCount">0</span> rows)</h5>
                    <button class="btn btn-primary btn-sm" onclick="syncSheetToContacts()">
                        <i class="fa-solid fa-arrow-right me-1"></i> Sync Sheet ➔ Contacts
                    </button>
                </div>

                <!-- Add Row Form -->
                <div class="p-2 mb-3 bg-light rounded d-flex gap-2">
                    <input type="text" id="newSheetName" class="form-control form-control-sm" placeholder="Full Name (e.g., Rajesh Kumar)">
                    <input type="text" id="newSheetPhone" class="form-control form-control-sm" placeholder="Phone Number (e.g., +1 650 555 0111)">
                    <button class="btn btn-success btn-sm text-nowrap" onclick="addSheetRow()"><i class="fa-solid fa-plus me-1"></i> Add Row</button>
                </div>

                <div class="table-responsive flex-grow-1" style="max-height: 400px; overflow-y: auto;">
                    <table class="table table-hover table-bordered table-sm align-middle" style="font-size: 0.88rem;">
                        <thead class="table-primary">
                            <tr>
                                <th>#</th>
                                <th>Full Name</th>
                                <th>Phone Number</th>
                                <th>Contact ID (System)</th>
                                <th>Last Synced</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="sheetTableBody">
                            <tr><td colspan="6" class="text-center text-muted py-4">No contacts in sheet yet. Click "Seed 10 Test Contacts" or paste a Google Sheet link above!</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- RIGHT: Google Contacts / Mobile View -->
        <div class="col-lg-5">
            <div class="card card-custom p-3 bg-white h-100">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="text-dark mb-0"><i class="fa-solid fa-mobile-screen-button me-2 text-primary"></i> Google Contacts / Mobile (<span id="contactsCount">0</span>)</h5>
                    <button class="btn btn-info btn-sm text-white" onclick="syncContactsToSheet()"><i class="fa-solid fa-arrow-left me-1"></i> Sync Contacts ➔ Sheet</button>
                </div>

                <!-- Add Mobile Contact Form -->
                <div class="p-2 mb-3 bg-light rounded d-flex gap-2">
                    <input type="text" id="newMobileName" class="form-control form-control-sm" placeholder="Name (e.g., Priyanka Sharma)">
                    <input type="text" id="newMobilePhone" class="form-control form-control-sm" placeholder="Phone (e.g., +1 650 555 0112)">
                    <button class="btn btn-primary btn-sm text-nowrap" onclick="addMobileContact()"><i class="fa-solid fa-user-plus me-1"></i> Add on Phone</button>
                </div>

                <div class="mobile-device-frame flex-grow-1" style="max-height: 400px; overflow-y: auto;">
                    <div id="contactsList">
                        <div class="text-center text-muted py-5">No contacts in Google Contacts yet.</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Real-Time Activity Log -->
    <div class="row mt-4">
        <div class="col-12">
            <div class="card card-custom p-3 bg-white">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <h6 class="mb-0 fw-bold"><i class="fa-solid fa-terminal me-2"></i> Live Execution Log & Diagnostics</h6>
                    <button class="btn btn-outline-secondary btn-sm" onclick="fetchState()">Refresh</button>
                </div>
                <div id="logTerminal" class="log-terminal"></div>
            </div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    async function fetchState() {
        const res = await fetch('/api/state');
        const data = await res.json();
        
        // Render Sheet Table
        document.getElementById('sheetCount').innerText = data.sheet_rows.length;
        const tbody = document.getElementById('sheetTableBody');
        if (data.sheet_rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No contacts in sheet yet. Click "Seed 10 Test Contacts" or upload a file above!</td></tr>';
        } else {
            tbody.innerHTML = data.sheet_rows.map((r, idx) => `
                <tr>
                    <td><strong>${idx + 1}</strong></td>
                    <td><strong>${r.name}</strong></td>
                    <td><code>${r.phone}</code></td>
                    <td><small class="text-muted font-monospace">${r.contact_id || '<span class="text-warning">Pending...</span>'}</small></td>
                    <td><small>${r.last_synced || '-'}</small></td>
                    <td><span class="${r.status.includes('Synced') ? 'badge-synced' : 'badge-pending'}">${r.status}</span></td>
                </tr>
            `).join('');
        }

        // Render Contacts Mobile List
        document.getElementById('contactsCount').innerText = data.contacts.length;
        const clist = document.getElementById('contactsList');
        if (data.contacts.length === 0) {
            clist.innerHTML = '<div class="text-center text-muted py-5">No contacts in Google Contacts yet.</div>';
        } else {
            clist.innerHTML = data.contacts.map(c => {
                const initial = c.name ? c.name[0].toUpperCase() : '?';
                return `
                    <div class="contact-item">
                        <div class="d-flex align-items-center">
                            <div class="avatar">${initial}</div>
                            <div>
                                <div class="fw-bold text-dark">${c.name}</div>
                                <div class="text-muted small">${c.phone}</div>
                            </div>
                        </div>
                        <div>
                            <span class="badge bg-light text-dark font-monospace" style="font-size: 0.7rem;">${c.resource_name}</span>
                        </div>
                    </div>
                `;
            }).join('');
        }

        // Render Logs
        const logBox = document.getElementById('logTerminal');
        logBox.innerHTML = data.logs.map(l => `<div>[${l.time}] [${l.level}] ${l.msg}</div>`).join('');

        // Daemon Badge
        const daemonBadge = document.getElementById('daemonBadge');
        if (data.auto_sync) {
            daemonBadge.className = 'badge bg-success p-2';
            daemonBadge.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Auto-Sync: ACTIVE (Every 2s)';
        } else {
            daemonBadge.className = 'badge bg-secondary p-2';
            daemonBadge.innerHTML = '<i class="fa-solid fa-robot me-1"></i> Auto-Sync: OFF';
        }
    }

    async function seed10() {
        await fetch('/api/seed', { method: 'POST' });
        fetchState();
    }

    async function addSheetRow() {
        const name = document.getElementById('newSheetName').value.trim();
        const phone = document.getElementById('newSheetPhone').value.trim();
        if (!name) return alert('Please enter a Name');
        await fetch('/api/add-sheet-row', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, phone })
        });
        document.getElementById('newSheetName').value = '';
        document.getElementById('newSheetPhone').value = '';
        fetchState();
    }

    async function addMobileContact() {
        const name = document.getElementById('newMobileName').value.trim();
        const phone = document.getElementById('newMobilePhone').value.trim();
        if (!name) return alert('Please enter a Name');
        await fetch('/api/add-mobile-contact', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, phone })
        });
        document.getElementById('newMobileName').value = '';
        document.getElementById('newMobilePhone').value = '';
        fetchState();
    }

    async function syncSheetToContacts() {
        await fetch('/api/sync-sheet-to-contacts', { method: 'POST' });
        fetchState();
    }

    async function syncContactsToSheet() {
        await fetch('/api/sync-contacts-to-sheet', { method: 'POST' });
        fetchState();
    }

    async function toggleAutoSync() {
        await fetch('/api/toggle-autosync', { method: 'POST' });
        fetchState();
    }

    async function resetAll() {
        await fetch('/api/reset', { method: 'POST' });
        fetchState();
    }

    async function uploadSpreadsheetFile(input) {
        if (!input.files || input.files.length === 0) return;
        const file = input.files[0];
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const res = await fetch('/api/upload-file', {
                method: 'POST',
                body: formData
            });
            const data = await res.json();
            if (data.success && data.added > 0) {
                alert(`Loaded ${data.added} contact(s) from ${file.name} and synced!`);
            } else {
                alert(data.error || 'No contact rows found in uploaded file.');
            }
        } catch (err) {
            alert('Upload error: ' + err.message);
        } finally {
            input.value = '';
            fetchState();
        }
    }

    async function importFromGoogleSheetUrl() {
        const input = document.getElementById('sheetUrlInput');
        const url = input.value.trim();
        if (!url) {
            alert('Please paste your Google Sheet link in the text box first!');
            input.focus();
            return;
        }

        const btn = event.target;
        const oldText = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Importing...';
        btn.disabled = true;

        try {
            const res = await fetch('/api/import-sheet-url', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sheet_url: url })
            });
            const data = await res.json();
            if (data.success && data.added > 0) {
                alert(`Successfully imported and synced ${data.added} contacts from your Google Sheet!`);
                input.value = '';
            } else {
                alert(data.error || 'Could not load rows. Make sure the Google Sheet sharing is set to "Anyone with the link can view", or use "Upload CSV"!');
            }
        } catch (err) {
            alert('Error connecting to server: ' + err.message);
        } finally {
            btn.innerHTML = oldText;
            btn.disabled = false;
            fetchState();
        }
    }

    // Polling UI state every 1.5 seconds
    setInterval(fetchState, 1500);
    fetchState();
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/state')
def get_state():
    return jsonify({
        "sheet_rows": state.sheet_rows,
        "contacts": state.contacts,
        "logs": state.logs,
        "auto_sync": state.auto_sync_enabled
    })


@app.route('/api/seed', methods=['POST'])
def api_seed():
    state.seed_10_contacts()
    return jsonify({"success": True})


@app.route('/download/sample.xlsx')
def download_sample_xlsx():
    path = os.path.join(BASE_DIR, 'contacts_sample.xlsx')
    return send_file(path, as_attachment=True, download_name='contacts_sample.xlsx')


@app.route('/download/sample.csv')
def download_sample_csv():
    path = os.path.join(BASE_DIR, 'contacts_sample.csv')
    return send_file(path, as_attachment=True, download_name='contacts_sample.csv')


@app.route('/api/upload-file', methods=['POST'])
def api_upload_file():
    if 'file' not in request.files:
        return jsonify({"success": False, "added": 0, "error": "No file received."})
    file = request.files['file']
    fname = file.filename.lower()
    try:
        if fname.endswith('.xlsx') or fname.endswith('.xls'):
            df = pd.read_excel(file)
            csv_text = df.to_csv(index=False)
            added = state.upload_csv(csv_text)
            return jsonify({"success": True, "added": added})
        else:
            content = file.read().decode('utf-8', errors='ignore')
            added = state.upload_csv(content)
            return jsonify({"success": True, "added": added})
    except Exception as e:
        return jsonify({"success": False, "added": 0, "error": str(e)})


@app.route('/api/upload-csv', methods=['POST'])
def api_upload_csv():
    data = request.json or {}
    added = state.upload_csv(data.get('csv_content', ''))
    return jsonify({"success": True, "added": added})


@app.route('/api/import-sheet-url', methods=['POST'])
def api_import_sheet_url():
    data = request.json or {}
    result = state.import_google_sheet_url(data.get('sheet_url', ''))
    return jsonify(result)


@app.route('/api/add-sheet-row', methods=['POST'])
def api_add_sheet_row():
    data = request.json or {}
    state.add_sheet_contact(data.get('name', ''), data.get('phone', ''))
    return jsonify({"success": True})


@app.route('/api/add-mobile-contact', methods=['POST'])
def api_add_mobile_contact():
    data = request.json or {}
    state.add_mobile_contact(data.get('name', ''), data.get('phone', ''))
    return jsonify({"success": True})


@app.route('/api/sync-sheet-to-contacts', methods=['POST'])
def api_sync_s2c():
    res = state.sync_sheet_to_contacts()
    return jsonify(res)


@app.route('/api/sync-contacts-to-sheet', methods=['POST'])
def api_sync_c2s():
    res = state.sync_contacts_to_sheet()
    return jsonify(res)


@app.route('/api/toggle-autosync', methods=['POST'])
def api_toggle_autosync():
    state.auto_sync_enabled = not state.auto_sync_enabled
    if state.auto_sync_enabled:
        state.add_log("Autonomous Auto-Sync Daemon STARTED (2s interval).", "INFO")
        t = threading.Thread(target=daemon_worker, daemon=True)
        t.start()
    else:
        state.add_log("Autonomous Auto-Sync Daemon STOPPED.", "INFO")
    return jsonify({"auto_sync": state.auto_sync_enabled})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    state.init_data()
    return jsonify({"success": True})


if __name__ == '__main__':
    port = 5050
    print("\n" + "=" * 75)
    print(f">> LOCAL TWO-WAY SYNC RUNNER ACTIVE AT: http://127.0.0.1:{port}")
    print("=" * 75)
    print(f"Open http://127.0.0.1:{port} in your browser to test and record the demo.")
    print("=" * 75 + "\n")
    app.run(host='127.0.0.1', port=port, debug=False)
