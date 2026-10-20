/**
 * ============================================================================
 * TWO-WAY SYNCHRONIZATION: GOOGLE SHEETS <-> GOOGLE CONTACTS
 * ============================================================================
 * Author / Candidate: Abhi Pandey
 * Technology Stack: Google Apps Script (V8), Google People API v1, Google Sheets
 * 
 * CORE FEATURES:
 * 1. Sheet ➔ Contacts: Real-time creation/update of contacts when rows are added
 *    or edited in Google Sheets (via installable onChange & onEdit triggers).
 * 2. Contacts ➔ Sheet: Autonomous 1-minute time trigger detects new contacts
 *    added on Mobile / Web, and appends them to Google Sheets (e.g. 10 ➔ 11).
 * 3. Infinite Loop Prevention: Tracks immutable system resourceNames in Column C.
 * 4. Concurrency Mutex: LockService prevents race conditions between events.
 * 5. Self-Healing & Diagnostics: Built-in health check and visual UI alerts.
 * ============================================================================
 */

// Configuration Constants
const CONFIG = {
  SHEET_NAME: 'Contacts',
  COLUMNS: {
    NAME: 1,           // Col A: Full Name
    PHONE: 2,          // Col B: Phone Number
    CONTACT_ID: 3,     // Col C: System Resource Name (e.g., people/c12345...)
    LAST_SYNC: 4,      // Col D: Last Synced Timestamp
    STATUS: 5          // Col E: Sync Status
  },
  HEADERS: ['Full Name', 'Phone Number', 'Contact ID (System)', 'Last Synced', 'Sync Status'],
  SYNC_LOCK_TIMEOUT_MS: 30000,       // 30 seconds mutex lock
  TIME_TRIGGER_INTERVAL_MINUTES: 1  // Contacts -> Sheet background check interval
};

/**
 * Custom Menu creation on Spreadsheet Open
 */
function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu('🔄 Sync Manager')
    .addItem('1. Initialize / Format Sheet', 'initializeSheet')
    .addItem('2. Populate Initial 10 Contacts', 'seedInitialTenContacts')
    .addItem('3. Install Automatic Triggers', 'installTriggers')
    .addSeparator()
    .addItem('⚡ Sync Sheet ➔ Contacts (Manual Run)', 'syncSheetToContactsWithAlert')
    .addItem('⚡ Sync Contacts ➔ Sheet (Manual Run)', 'syncContactsToSheetWithAlert')
    .addItem('🔄 Full Two-Way Sync Now', 'performFullTwoWaySync')
    .addSeparator()
    .addItem('🔍 System Diagnostic & Health Check', 'runSystemDiagnostics')
    .addItem('🛑 Remove All Triggers (Pause Sync)', 'removeAllTriggers')
    .addToUi();
}

/**
 * 1. Initialize Sheet structure, headers, formatting, and number formatting
 */
function initializeSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  
  if (!sheet) {
    const active = ss.getActiveSheet();
    // If active sheet is blank Sheet1, rename it to Contacts
    if (active.getLastRow() === 0 && active.getLastColumn() === 0 && active.getName() === 'Sheet1') {
      active.setName(CONFIG.SHEET_NAME);
      sheet = active;
    } else {
      sheet = ss.insertSheet(CONFIG.SHEET_NAME);
    }
  }
  
  // Format Header Row
  const headerRange = sheet.getRange(1, 1, 1, CONFIG.HEADERS.length);
  headerRange.setValues([CONFIG.HEADERS]);
  headerRange.setFontWeight('bold');
  headerRange.setBackground('#1A73E8'); // Professional Google Blue
  headerRange.setFontColor('#FFFFFF');
  headerRange.setHorizontalAlignment('center');
  headerRange.setVerticalAlignment('middle');
  sheet.setRowHeight(1, 35);
  
  // Freeze Header Row
  sheet.setFrozenRows(1);

  // Set Column Widths
  sheet.setColumnWidth(CONFIG.COLUMNS.NAME, 200);
  sheet.setColumnWidth(CONFIG.COLUMNS.PHONE, 180);
  sheet.setColumnWidth(CONFIG.COLUMNS.CONTACT_ID, 260);
  sheet.setColumnWidth(CONFIG.COLUMNS.LAST_SYNC, 180);
  sheet.setColumnWidth(CONFIG.COLUMNS.STATUS, 180);

  // Force Phone Column to Plain Text (@) to prevent scientific notation or dropping leading zeros
  sheet.getRange(2, CONFIG.COLUMNS.PHONE, Math.max(1, sheet.getMaxRows() - 1), 1).setNumberFormat('@');
  
  safeToast('Sheet formatted and initialized successfully!', 'Setup Complete', 5);
}

/**
 * 2. Populate Initial 10 Contacts into the Sheet (Requirement 2 & 3)
 */
function seedInitialTenContacts() {
  const sheet = getOrCreateContactsSheet();
  const existingData = sheet.getDataRange().getValues();
  
  if (existingData.length > 1) {
    try {
      const ui = SpreadsheetApp.getUi();
      const response = ui.alert(
        'Existing Contacts Detected',
        'Your sheet already has rows. Would you like to append the 10 initial sample contacts?',
        ui.ButtonSet.YES_NO
      );
      if (response !== ui.Button.YES) return;
    } catch (e) {
      // In headless run, continue
    }
  }

  const sampleContacts = [
    { name: 'Aarav Sharma', phone: '+1 650 555 0101' },
    { name: 'Diya Patel', phone: '+1 650 555 0102' },
    { name: 'Rohan Iyer', phone: '+1 650 555 0103' },
    { name: 'Ananya Verma', phone: '+1 650 555 0104' },
    { name: 'Vikram Malhotra', phone: '+1 650 555 0105' },
    { name: 'Pooja Nair', phone: '+1 650 555 0106' },
    { name: 'Siddharth Rao', phone: '+1 650 555 0107' },
    { name: 'Sneha Kulkarni', phone: '+1 650 555 0108' },
    { name: 'Aditya Joshi', phone: '+1 650 555 0109' },
    { name: 'Meera Deshmukh', phone: '+1 650 555 0110' }
  ];

  const rowsToAdd = sampleContacts.map(c => [
    c.name,
    c.phone,
    '', // Contact ID initially empty, filled on sync
    '', // Last Synced
    'Pending Sync'
  ]);

  const startRow = sheet.getLastRow() + 1;
  sheet.getRange(startRow, 1, rowsToAdd.length, CONFIG.HEADERS.length).setValues(rowsToAdd);
  
  safeToast('Added 10 contacts. Initiating synchronization to Google Contacts...', 'Seeding Data', 4);
  
  // Immediately synchronize these 10 contacts to Google Contacts
  syncSheetToContacts();
}

/**
 * 3. Setup Triggers for Autonomous Two-Way Synchronization
 */
function installTriggers() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  
  // Delete existing triggers for this project to prevent duplicates
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  
  // 1. Installable onChange Trigger: Catches row additions, imports, batch pastes
  ScriptApp.newTrigger('onSpreadsheetChange')
    .forSpreadsheet(ss)
    .onChange()
    .create();

  // 2. Installable onEdit Trigger: Catches real-time individual cell edits
  ScriptApp.newTrigger('onSpreadsheetEdit')
    .forSpreadsheet(ss)
    .onEdit()
    .create();

  // 3. Time-Driven Trigger: Polls Google Contacts every 1 minute for Mobile / Web additions
  ScriptApp.newTrigger('syncContactsToSheet')
    .timeBased()
    .everyMinutes(CONFIG.TIME_TRIGGER_INTERVAL_MINUTES)
    .create();
    
  try {
    const ui = SpreadsheetApp.getUi();
    ui.alert(
      'Two-Way Sync Triggers Installed!',
      'All automated triggers have been installed successfully.\n\n' +
      '• Sheet ➔ Contacts: Real-time (fires whenever you edit or add rows in the sheet)\n' +
      '• Contacts ➔ Sheet: Fully automated (runs in background every 1 minute to pull new contacts created on mobile/web)\n\n' +
      'The synchronization is now running 100% autonomously without manual intervention.',
      ui.ButtonSet.OK
    );
  } catch (e) {
    safeToast('Triggers installed successfully!', 'Active', 5);
  }
}

/**
 * Helper to remove all triggers if user wants to pause synchronization
 */
function removeAllTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  safeToast('All automated triggers have been removed.', 'Sync Paused', 5);
}

/**
 * Trigger Handler: Edit Event (Captures single cell changes in Column A or B)
 */
function onSpreadsheetEdit(e) {
  if (!e || !e.range) return;
  const sheet = e.range.getSheet();
  if (sheet.getName() !== CONFIG.SHEET_NAME) return;
  
  const row = e.range.getRow();
  const col = e.range.getColumn();
  if (row <= 1) return; // Ignore header
  
  // If user edited Name (Col 1) or Phone (Col 2), mark status as Pending Sync
  if (col === CONFIG.COLUMNS.NAME || col === CONFIG.COLUMNS.PHONE) {
    sheet.getRange(row, CONFIG.COLUMNS.STATUS).setValue('Pending Sync');
  }
  
  // Trigger Sheet -> Contacts synchronization
  syncSheetToContacts();
}

/**
 * Trigger Handler: Change Event (Captures row inserts, pastes, formula changes)
 */
function onSpreadsheetChange(e) {
  if (!e) return;
  syncSheetToContacts();
}

/**
 * ============================================================================
 * CORE ENGINE: GOOGLE SHEET ➔ GOOGLE CONTACTS
 * ============================================================================
 * Reads rows from Google Sheets.
 * - Missing Contact ID -> Creates contact via People.People.createContact()
 * - Modified row ('Pending Sync') -> Updates contact via People.People.updateContact()
 * Writes back unique system resourceName and timestamp.
 */
function syncSheetToContacts() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(CONFIG.SYNC_LOCK_TIMEOUT_MS)) {
    console.warn('Sync currently in progress by another event. Skipping duplicate invocation.');
    return { created: 0, updated: 0, skipped: 0 };
  }

  let createdCount = 0;
  let updatedCount = 0;

  try {
    const sheet = getOrCreateContactsSheet();
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return { created: 0, updated: 0, skipped: 0 };

    assertPeopleApiEnabled();

    const nowStr = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');

    for (let i = 1; i < data.length; i++) {
      const rowNum = i + 1;
      const fullName = (data[i][CONFIG.COLUMNS.NAME - 1] || '').toString().trim();
      const phoneNumber = (data[i][CONFIG.COLUMNS.PHONE - 1] || '').toString().trim();
      let contactId = (data[i][CONFIG.COLUMNS.CONTACT_ID - 1] || '').toString().trim();
      const status = (data[i][CONFIG.COLUMNS.STATUS - 1] || '').toString().trim();

      // Skip completely empty rows
      if (!fullName && !phoneNumber) continue;

      // Skip rows already synced and not marked for update
      if (contactId && status === 'Synced') continue;

      const nameParts = splitFullName(fullName);

      // CASE 1: Contact doesn't exist in Google Contacts -> CREATE
      if (!contactId) {
        try {
          const contactPayload = {
            names: [{
              givenName: nameParts.firstName,
              familyName: nameParts.lastName,
              displayName: fullName || 'Unnamed Contact'
            }],
            phoneNumbers: phoneNumber ? [{
              value: phoneNumber,
              type: 'mobile'
            }] : []
          };

          const created = People.People.createContact(contactPayload);
          contactId = created.resourceName;

          sheet.getRange(rowNum, CONFIG.COLUMNS.CONTACT_ID).setValue(contactId);
          sheet.getRange(rowNum, CONFIG.COLUMNS.LAST_SYNC).setValue(nowStr);
          sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue('Synced');
          createdCount++;
          console.log(`Created Google Contact: ${fullName} (${contactId})`);
        } catch (err) {
          console.error(`Error creating contact for row ${rowNum}: ${err.message}`);
          sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue(`Error: ${err.message.substring(0, 35)}`);
        }
      } 
      // CASE 2: Contact exists in Google Contacts -> UPDATE
      else {
        try {
          const existing = People.People.get(contactId, { personFields: 'names,phoneNumbers,metadata' });
          
          const updatePayload = {
            resourceName: contactId,
            etag: existing.etag,
            names: [{
              givenName: nameParts.firstName,
              familyName: nameParts.lastName,
              displayName: fullName || 'Unnamed Contact'
            }],
            phoneNumbers: phoneNumber ? [{
              value: phoneNumber,
              type: 'mobile'
            }] : []
          };

          People.People.updateContact(updatePayload, contactId, {
            updatePersonFields: 'names,phoneNumbers'
          });

          sheet.getRange(rowNum, CONFIG.COLUMNS.LAST_SYNC).setValue(nowStr);
          sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue('Synced');
          updatedCount++;
          console.log(`Updated Google Contact: ${fullName} (${contactId})`);
        } catch (err) {
          // If contact was deleted in Google Contacts remotely, reset contactId to recreate it
          if (err.message && (err.message.includes('404') || err.message.includes('not found'))) {
            console.warn(`Contact ${contactId} was deleted in Google Contacts. Recreating...`);
            sheet.getRange(rowNum, CONFIG.COLUMNS.CONTACT_ID).setValue('');
            sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue('Pending Sync');
          } else {
            console.error(`Error updating contact ${contactId}: ${err.message}`);
            sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue(`Error: ${err.message.substring(0, 35)}`);
          }
        }
      }
    }

    if (createdCount > 0 || updatedCount > 0) {
      safeToast(`Sheet ➔ Contacts: ${createdCount} created, ${updatedCount} updated.`, 'Synced', 4);
    }
    
    return { created: createdCount, updated: updatedCount };
  } finally {
    lock.releaseLock();
  }
}

/**
 * Manual trigger wrapper with UI Alert
 */
function syncSheetToContactsWithAlert() {
  const result = syncSheetToContacts();
  try {
    const ui = SpreadsheetApp.getUi();
    ui.alert(
      'Sheet ➔ Contacts Sync Complete',
      `Processed synchronization:\n• Contacts Created: ${result.created}\n• Contacts Updated: ${result.updated}`,
      ui.ButtonSet.OK
    );
  } catch (e) {
    safeToast(`Sheet ➔ Contacts: ${result.created} created, ${result.updated} updated.`, 'Finished', 5);
  }
}

/**
 * ============================================================================
 * CORE ENGINE: GOOGLE CONTACTS ➔ GOOGLE SHEET
 * ============================================================================
 * Fetches all contacts from Google Contacts (added via Mobile Phone or Web).
 * - If contact resourceName does NOT exist in sheet -> Appends new row.
 * - If contact details changed on mobile -> Updates matching sheet row.
 * Runs automatically every 1 minute via Time Trigger.
 */
function syncContactsToSheet() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(CONFIG.SYNC_LOCK_TIMEOUT_MS)) {
    console.warn('Sync currently in progress by another event. Skipping duplicate invocation.');
    return { added: 0, updated: 0 };
  }

  let addedCount = 0;
  let updatedCount = 0;

  try {
    const sheet = getOrCreateContactsSheet();
    const data = sheet.getDataRange().getValues();
    assertPeopleApiEnabled();

    // Map existing contact resourceNames in sheet to 1-based row index
    const sheetContactMap = new Map();
    for (let i = 1; i < data.length; i++) {
      const contactId = (data[i][CONFIG.COLUMNS.CONTACT_ID - 1] || '').toString().trim();
      if (contactId) {
        sheetContactMap.set(contactId, i + 1);
      }
    }

    // Retrieve all contacts from Google People API with pagination
    let allConnections = [];
    let nextPageToken = null;

    do {
      const options = {
        personFields: 'names,phoneNumbers,metadata',
        pageSize: 100,
        sortOrder: 'LAST_MODIFIED_DESCENDING'
      };
      if (nextPageToken) options.pageToken = nextPageToken;

      const response = People.People.Connections.list('people/me', options);
      if (response.connections && response.connections.length > 0) {
        allConnections = allConnections.concat(response.connections);
      }
      nextPageToken = response.nextPageToken;
    } while (nextPageToken);

    const nowStr = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm:ss');
    const newRowsToAppend = [];

    for (const contact of allConnections) {
      const resourceName = contact.resourceName;
      if (!resourceName) continue;

      // Extract Name
      let displayName = '';
      if (contact.names && contact.names.length > 0) {
        displayName = contact.names[0].displayName || 
                      `${contact.names[0].givenName || ''} ${contact.names[0].familyName || ''}`.trim();
      }

      // Extract Mobile / Primary Phone
      let phoneNumber = '';
      if (contact.phoneNumbers && contact.phoneNumbers.length > 0) {
        phoneNumber = contact.phoneNumbers[0].value || '';
      }

      // Skip entries without name or phone
      if (!displayName && !phoneNumber) continue;

      // CASE 1: Contact added directly from Mobile Phone / Web (NOT present in sheet)
      if (!sheetContactMap.has(resourceName)) {
        newRowsToAppend.push([
          displayName,
          phoneNumber,
          resourceName,
          nowStr,
          'Synced (from Contacts)'
        ]);
        addedCount++;
        console.log(`New contact detected in Google Contacts: ${displayName} (${phoneNumber})`);
      } 
      // CASE 2: Contact exists in sheet -> Check if modified on mobile device
      else {
        const rowNum = sheetContactMap.get(resourceName);
        const currentSheetName = (data[rowNum - 1][CONFIG.COLUMNS.NAME - 1] || '').toString().trim();
        const currentSheetPhone = (data[rowNum - 1][CONFIG.COLUMNS.PHONE - 1] || '').toString().trim();

        // Check if name or phone changed remotely (using normalized phone comparison)
        const nameChanged = displayName && displayName !== currentSheetName;
        const phoneChanged = phoneNumber && normalizePhone(phoneNumber) !== normalizePhone(currentSheetPhone);

        if (nameChanged || phoneChanged) {
          sheet.getRange(rowNum, CONFIG.COLUMNS.NAME).setValue(displayName);
          sheet.getRange(rowNum, CONFIG.COLUMNS.PHONE).setValue(phoneNumber);
          sheet.getRange(rowNum, CONFIG.COLUMNS.LAST_SYNC).setValue(nowStr);
          sheet.getRange(rowNum, CONFIG.COLUMNS.STATUS).setValue('Synced');
          updatedCount++;
          console.log(`Updated sheet row ${rowNum} with changes from mobile: ${displayName}`);
        }
      }
    }

    // Append all new contacts detected
    if (newRowsToAppend.length > 0) {
      const startRow = sheet.getLastRow() + 1;
      sheet.getRange(startRow, 1, newRowsToAppend.length, CONFIG.HEADERS.length).setValues(newRowsToAppend);
      console.log(`Appended ${newRowsToAppend.length} new contact(s) to Google Sheet.`);
    }

    if (addedCount > 0 || updatedCount > 0) {
      safeToast(`Contacts ➔ Sheet: ${addedCount} new added, ${updatedCount} updated.`, 'Synced', 5);
    }
    
    return { added: addedCount, updated: updatedCount };
  } finally {
    lock.releaseLock();
  }
}

/**
 * Manual trigger wrapper with UI Alert
 */
function syncContactsToSheetWithAlert() {
  const result = syncContactsToSheet();
  try {
    const ui = SpreadsheetApp.getUi();
    ui.alert(
      'Contacts ➔ Sheet Sync Complete',
      `Processed synchronization:\n• New Contacts Added to Sheet: ${result.added}\n• Existing Contacts Updated in Sheet: ${result.updated}`,
      ui.ButtonSet.OK
    );
  } catch (e) {
    safeToast(`Contacts ➔ Sheet: ${result.added} added, ${result.updated} updated.`, 'Finished', 5);
  }
}

/**
 * Full Bidirectional Reconciliation Run
 */
function performFullTwoWaySync() {
  safeToast('Starting full two-way synchronization...', 'Sync Running', 4);
  const s2c = syncSheetToContacts();
  const c2s = syncContactsToSheet();
  
  try {
    const ui = SpreadsheetApp.getUi();
    ui.alert(
      'Full Two-Way Sync Complete',
      `Summary:\n` +
      `• Sheet ➔ Contacts: ${s2c.created} created, ${s2c.updated} updated\n` +
      `• Contacts ➔ Sheet: ${c2s.added} added, ${c2s.updated} updated`,
      ui.ButtonSet.OK
    );
  } catch (e) {
    safeToast('Two-way synchronization completed!', 'Sync Finished', 5);
  }
}

/**
 * Comprehensive System Diagnostic & Health Check
 */
function runSystemDiagnostics() {
  const ui = SpreadsheetApp.getUi();
  const report = [];

  // 1. Google People API Connectivity
  try {
    assertPeopleApiEnabled();
    const testRes = People.People.Connections.list('people/me', { pageSize: 1, personFields: 'names' });
    report.push('✅ Google People API: ACTIVE & Authorized');
  } catch (err) {
    report.push(`❌ Google People API Error: ${err.message}`);
  }

  // 2. Automated Triggers Check
  const triggers = ScriptApp.getProjectTriggers();
  report.push(`\n📋 Installed Triggers: ${triggers.length} Active`);
  if (triggers.length === 0) {
    report.push('   ⚠️ Warning: No triggers installed. Click "3. Install Automatic Triggers".');
  } else {
    triggers.forEach(t => {
      report.push(`   • ${t.getHandlerFunction()} (${t.getEventType()})`);
    });
  }

  // 3. Spreadsheet Structure & Count
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (sheet) {
    const totalRows = Math.max(0, sheet.getLastRow() - 1);
    report.push(`\n📊 Sheet "${CONFIG.SHEET_NAME}": OK (${totalRows} contacts listed)`);
  } else {
    report.push(`\n⚠️ Sheet "${CONFIG.SHEET_NAME}": NOT FOUND`);
  }

  ui.alert('System Diagnostic & Health Check', report.join('\n'), ui.ButtonSet.OK);
}

/**
 * Utility: Split Full Name into Given and Family names
 */
function splitFullName(fullName) {
  if (!fullName) return { firstName: '', lastName: '' };
  const parts = fullName.trim().split(/\s+/);
  if (parts.length === 1) return { firstName: parts[0], lastName: '' };
  const firstName = parts.slice(0, -1).join(' ');
  const lastName = parts[parts.length - 1];
  return { firstName, lastName };
}

/**
 * Utility: Normalize phone number for safe comparison (digits only)
 */
function normalizePhone(phone) {
  if (!phone) return '';
  return phone.toString().replace(/\D/g, '');
}

/**
 * Utility: Get or Create Contacts Sheet safely
 */
function getOrCreateContactsSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) {
    initializeSheet();
    sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  }
  return sheet;
}

/**
 * Utility: Toast notifications safe for both UI and background trigger contexts
 */
function safeToast(msg, title, timeoutSeconds) {
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    if (ss) ss.toast(msg, title || 'Sync Manager', timeoutSeconds || 4);
  } catch (e) {
    // Non-fatal if triggered without active UI context
  }
}

/**
 * Verification: Ensure Google People API is enabled in Apps Script Services
 */
function assertPeopleApiEnabled() {
  if (typeof People === 'undefined' || !People.People) {
    const errorMsg = 'Google People API is not enabled in Apps Script Services!\n\n' +
      'To enable it in 5 seconds:\n' +
      '1. In Apps Script Editor, click "+" next to Services in the left sidebar.\n' +
      '2. Select "Google People API" (Version: v1, Identifier: People).\n' +
      '3. Click "Add" and re-run.';
    
    try {
      const ui = SpreadsheetApp.getUi();
      ui.alert('Prerequisite Required', errorMsg, ui.ButtonSet.OK);
    } catch (e) {
      console.error(errorMsg);
    }
    throw new Error('Google People API Advanced Service is not enabled.');
  }
}
