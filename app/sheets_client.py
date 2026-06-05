import os
import time
import gspread
from google.oauth2.service_account import Credentials
import logging

logger = logging.getLogger(__name__)

# Master DET Reporting Summary Sheet
MASTER_SHEET_ID = "1RWWWBhkQDSlWgiczJhXwQUxUNYgGep_8VeiTuC_pGFs"

def get_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'credentials.json')
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Credentials file not found at {creds_path}. Please ensure it exists.")
        
    credentials = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(credentials)
    return client

def with_retry(max_retries=5, initial_backoff=1):
    """Decorator to apply exponential backoff retries for Google Sheets API calls."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            backoff = initial_backoff
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    if e.response.status_code == 429: # Rate limit
                        logger.warning(f"Rate limited. Retrying in {backoff} seconds... (Attempt {retries + 1}/{max_retries})")
                        time.sleep(backoff)
                        retries += 1
                        backoff *= 2
                    else:
                        raise e
            raise Exception(f"Max retries exceeded for {func.__name__}")
        return wrapper
    return decorator

class SheetsClient:
    def __init__(self, log_callback=None):
        self.client = get_client()
        self.log_callback = log_callback or (lambda x: logger.info(x))

    def log(self, message):
        self.log_callback(message)

    @with_retry()
    def get_sheet_data(self, sheet_id, worksheet_name=None):
        """Fetches all records from a given sheet."""
        self.log(f"Connecting to Google Sheet ID: {sheet_id}...")
        sheet = self.client.open_by_key(sheet_id)
        if worksheet_name:
            worksheet = sheet.worksheet(worksheet_name)
        else:
            worksheet = sheet.get_worksheet(0)
            
        self.log(f"Reading data from '{worksheet.title}'...")
        return worksheet.get_all_records()

    @with_retry()
    def get_historical_schools_data(self):
        """Fetches all schools that have historically received a device, and all known school names."""
        self.log("Fetching historical school data...")
        sheet = self.client.open_by_key(MASTER_SHEET_ID)
        allocated_schools = set()
        all_known_schools = set()
        
        for ws in sheet.worksheets():
            title = ws.title
            if title in ["Summary", "Summary Data", "ACCREDITED TEMPLATE", "Jext and Neffy TEMPLATE", "Absent staff", "TEMPLATE"]:
                continue
            
            try:
                # Row 2 contains headers
                headers = ws.row_values(2)
                if not headers:
                    continue
                records = ws.get_all_records(expected_headers=headers)
                for row in records:
                    school_name = str(row.get('School Name', '')).strip()
                    if school_name:
                        all_known_schools.add(school_name)
                        
                        device_sent = str(row.get('Device Pack Sent', row.get('Anapen Sent', row.get('EpiPen Sent', '')))).strip().lower()
                        if device_sent in ['yes', 'true', '1']:
                            allocated_schools.add(school_name)
            except Exception as e:
                self.log(f"Warning: Could not parse history from tab {title}: {e}")
                
        return list(all_known_schools), allocated_schools

    @with_retry()
    def append_to_master(self, active_rows, absent_rows, target_tab_name, workshop_type='accredited', adv_metrics=None):
        """Appends the formatted active rows to the Master Sheet.
        Duplicate the correct template tab if the tab doesn't exist."""
        if not active_rows and not absent_rows:
            self.log("No data to append to Master Sheet.")
            return

        master_sheet = self.client.open_by_key(MASTER_SHEET_ID)

        if active_rows:
            if not target_tab_name:
                target_tab_name = f"Session {int(time.time())}"
                
            self.log(f"Preparing tab '{target_tab_name}' in Master Sheet...")
            
            try:
                # Check if it already exists
                main_ws = master_sheet.worksheet(target_tab_name)
                self.log(f"Tab '{target_tab_name}' already exists. Appending to it.")
            except gspread.exceptions.WorksheetNotFound:
                if adv_metrics and 'raw_date' in adv_metrics:
                    date_str = adv_metrics['raw_date']
                else:
                    date_str = target_tab_name
                    import re
                    if re.match(r'^\d{8}$', date_str):
                        date_str = f"{date_str[:2]}/{date_str[2:4]}/{date_str[4:]}"

                if workshop_type == 'jext_neffy':
                    template_name = "Jext and Neffy TEMPLATE"
                    title_prefix = f"Department of Education\nJext and Neffy workshop\n{date_str}"
                    headers = ['First Name', 'Last Name', 'Course', 'Date', 'School Name', 'School Type', 'Device Pack Sent']
                else:
                    template_name = "ACCREDITED TEMPLATE"
                    title_prefix = f"Department of Education\nCW45168 - Delivery of an anaphylaxis training package for Victorian government schools\n{date_str}"
                    headers = ['First Name', 'Last Name', 'Code', 'Course', 'Date', 'School Name', 'School Type', 'Device Pack Sent']
                    
                try:
                    template_ws = master_sheet.worksheet(template_name)
                    main_ws = template_ws.duplicate(new_sheet_name=target_tab_name)
                    # Update A1 with the tab name
                    main_ws.update(range_name='A1', values=[[title_prefix]])
                    self.log(f"Successfully duplicated {template_name} into '{target_tab_name}'.")
                except Exception as e:
                    self.log(f"⚠️ Failed to duplicate {template_name}: {e}. Creating blank tab.")
                    main_ws = master_sheet.add_worksheet(title=target_tab_name, rows="1000", cols="20")
                    main_ws.append_row(headers)
            
            # Format rows as lists of values in the correct order
            if workshop_type == 'jext_neffy':
                headers = ['First Name', 'Last Name', 'Course', 'Date', 'School Name', 'School Type', 'Device Pack Sent']
            else:
                headers = ['First Name', 'Last Name', 'Code', 'Course', 'Date', 'School Name', 'School Type', 'Device Pack Sent']
                
            values = []
            for row in active_rows:
                values.append([row.get(h, '') for h in headers])
                
            try:
                cell = main_ws.find("First Name")
                table_range = f"A{cell.row}"
                
                # Accurately determine where append_rows will start inserting
                col_a_vals = main_ws.col_values(1)
                start_row = cell.row + 1
                for idx in range(cell.row - 1, len(col_a_vals)):
                    if not col_a_vals[idx].strip():
                        start_row = idx + 1
                        break
                else:
                    start_row = len(col_a_vals) + 1
                    
                # Push bottom table down if we are going to overlap it
                try:
                    totals_cell = main_ws.find("Total Devices Sent")
                    if totals_cell and totals_cell.row <= start_row + len(active_rows) + 2:
                        push_down = (start_row + len(active_rows) + 2) - totals_cell.row
                        empty_rows = [[] for _ in range(push_down)]
                        main_ws.insert_rows(empty_rows, row=totals_cell.row)
                        self.log(f"⚠️ Pushed bottom table down by {push_down} rows to avoid overlap.")
                except gspread.exceptions.CellNotFound:
                    pass
                    
                main_ws.append_rows(values, table_range=table_range)
                self.log(f"✅ Success! Loaded {len(active_rows)} rows after headers at {table_range}.")
            except gspread.exceptions.CellNotFound:
                col_a_vals = main_ws.col_values(1)
                start_row = len(col_a_vals) + 1
                main_ws.append_rows(values)
                self.log(f"✅ Success! Loaded {len(active_rows)} rows at bottom.")
                
            # Expand grid if start_row + len(active_rows) > max rows
            # Just in case gspread didn't expand it enough for formatting
            needed_rows = start_row + len(active_rows)
            if needed_rows > main_ws.row_count:
                main_ws.add_rows(needed_rows - main_ws.row_count + 5)
                
            # Highlight Yellow Rows
            from gspread.utils import rowcol_to_a1
            formats = []
            for i, row_dict in enumerate(active_rows):
                if row_dict.get("_highlight_yellow"):
                    # Format cols A to H
                    rng = f"A{start_row + i}:H{start_row + i}"
                    formats.append({
                        "range": rng,
                        "format": {
                            "backgroundColor": {
                                "red": 1.0,
                                "green": 1.0,
                                "blue": 0.0
                            }
                        }
                    })
            if formats:
                main_ws.batch_format(formats)
                self.log(f"✅ Highlighted {len(formats)} rows in yellow.")
                
            # Update the bottom totals table
            self.update_device_totals_table(main_ws, active_rows)

        if absent_rows:
            self.log(f"Appending {len(absent_rows)} rows to Absent staff...")
            try:
                absent_ws = master_sheet.worksheet("Absent staff")
            except gspread.exceptions.WorksheetNotFound:
                try:
                    absent_ws = master_sheet.worksheet("Absent Staff Summary")
                except gspread.exceptions.WorksheetNotFound:
                    self.log("⚠️ 'Absent staff' tab not found. Creating it...")
                    absent_ws = master_sheet.add_worksheet(title="Absent staff", rows="1000", cols="20")
                    headers = list(absent_rows[0].keys())
                    absent_ws.append_row(headers)
                
            # For absent rows, we just dump whatever columns we got from the dict
            values = [list(row.values()) for row in absent_rows]
            absent_ws.append_rows(values)
            self.log(f"✅ Success! Routed {len(absent_rows)} absent rows into Absent staff tab.")

    @with_retry()
    def update_device_totals_table(self, main_ws, active_rows):
        try:
            cell = main_ws.find("Total Devices Sent")
            if not cell:
                return
            
            all_data = main_ws.get_all_values()
            header_idx = -1
            dev_pack_col = -1
            school_type_col = -1
            total_col = -1
            
            for r_idx, r_data in enumerate(all_data):
                if "Total Devices Sent" in r_data:
                    header_idx = r_idx
                    total_col = r_data.index("Total Devices Sent")
                    if "Device Pack" in r_data:
                        dev_pack_col = r_data.index("Device Pack")
                    if "School Type" in r_data:
                        school_type_col = r_data.index("School Type")
                    break
                    
            if header_idx == -1 or dev_pack_col == -1 or school_type_col == -1:
                return
                
            from collections import defaultdict
            pack_counts = defaultdict(int)
            for row in active_rows:
                dp = str(row.get("Device Pack Sent", "")).strip()
                st = str(row.get("School Type", "")).strip()
                if dp and st:
                    pack_counts[(dp.lower(), st.lower())] += 1
                    
            updates = []
            for r_idx in range(header_idx + 1, len(all_data)):
                r_data = all_data[r_idx]
                if len(r_data) <= max(dev_pack_col, school_type_col, total_col):
                    continue
                    
                dp_val = str(r_data[dev_pack_col]).strip()
                st_val = str(r_data[school_type_col]).strip()
                
                if not dp_val or not st_val:
                    break
                    
                count = 0
                for (adp, ast), c in pack_counts.items():
                    if dp_val.lower() in adp or adp in dp_val.lower():
                        if st_val.lower() == ast:
                            count += c
                            
                total_sent = count
                from gspread.utils import rowcol_to_a1
                cell_a1 = rowcol_to_a1(r_idx + 1, total_col + 1)
                updates.append({'range': cell_a1, 'values': [[total_sent]]})
                
            if updates:
                main_ws.batch_update(updates)
                self.log("✅ Successfully updated Device Totals table based on pack multipliers!")
        except Exception as e:
            self.log(f"⚠️ Failed to update Device Totals table: {e}")

    def update_summary_metrics(self, unique_schools_count, school_type_counts, workshop_type='accredited', adv_metrics=None):
        """Updates the Summary dashboard in the Master Sheet."""
        master_sheet = self.client.open_by_key(MASTER_SHEET_ID)
        
        # --- Advanced Summary Data Tab ---
        if adv_metrics:
            try:
                self.log("Updating 'Summary Data' tab...")
                summary_data_ws = master_sheet.worksheet("Summary Data")
                all_data = summary_data_ws.get_all_values()
                
                # Table 1: Date | Delivery mode | # Government Schools trained...
                # Locate Table 1 header ("Date")
                table_1_header_idx = -1
                for r_idx, r_data in enumerate(all_data):
                    if r_data and r_data[0].strip() == "Date":
                        table_1_header_idx = r_idx + 1
                        break
                        
                if table_1_header_idx > 0:
                    target_row_idx = -1
                    insert_needed = False
                    for r_idx in range(table_1_header_idx, len(all_data)):
                        if not all_data[r_idx] or not all_data[r_idx][0].strip():
                            target_row_idx = r_idx + 1
                            break
                        if all_data[r_idx][0].strip() == "Total":
                            target_row_idx = r_idx + 1
                            insert_needed = True
                            break
                            
                    if target_row_idx > 0:
                        new_row = [
                            adv_metrics['formatted_date'],
                            "Virtual",
                            adv_metrics['adv_schools_trained'].get('Government', 0),
                            adv_metrics['adv_staff_trained'].get('Government', 0),
                            adv_metrics['adv_schools_trained'].get('Catholic', 0),
                            adv_metrics['adv_staff_trained'].get('Catholic', 0),
                            adv_metrics['adv_schools_trained'].get('Independent', 0),
                            adv_metrics['adv_staff_trained'].get('Independent', 0)
                        ]
                        
                        if insert_needed:
                            summary_data_ws.insert_row(new_row, index=target_row_idx)
                        else:
                            summary_data_ws.update(f"A{target_row_idx}:H{target_row_idx}", [new_row])
                        
                        # Fix formatting: plain white, not bold, and numbers not dollars
                        summary_data_ws.format(f"A{target_row_idx}:H{target_row_idx}", {
                            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "textFormat": {"bold": False}
                        })
                        summary_data_ws.format(f"A{target_row_idx}", {
                            "horizontalAlignment": "LEFT"
                        })
                        summary_data_ws.format(f"B{target_row_idx}:H{target_row_idx}", {
                            "horizontalAlignment": "CENTER"
                        })
                        summary_data_ws.format(f"C{target_row_idx}:H{target_row_idx}", {
                            "numberFormat": {"type": "NUMBER", "pattern": "0"}
                        })
                        
                        self.log("✅ Successfully added row into Summary Data (Table 1).")
                        
                        # Refetch all data since we modified it
                        all_data = summary_data_ws.get_all_values()
                    else:
                        self.log("⚠️ Could not find target row for Table 1.")
                else:
                    self.log("⚠️ Could not find 'Date' header for Table 1.")
                    
                # Table 2: Course Breakdown
                course_header_idx = -1
                for r_idx, r_data in enumerate(all_data):
                    if r_data and r_data[0].strip() == "Course":
                        course_header_idx = r_idx + 1
                        break
                        
                if course_header_idx > 0:
                    # Find bottom of table 2
                    table_2_end = course_header_idx
                    for r_idx in range(course_header_idx, len(all_data)):
                        if not all_data[r_idx] or not all_data[r_idx][0].strip():
                            table_2_end = r_idx + 1
                            break
                        table_2_end = r_idx + 2
                    
                    course_rows = []
                    # Sort courses alphabetically to ensure Anaphylaxis (22578) is before Verifier (22579)
                    sorted_courses = sorted(adv_metrics['adv_course_breakdown'].items(), key=lambda x: x[0])
                    for course_name, counts in sorted_courses:
                        g_staff = counts.get('Government', 0)
                        c_staff = counts.get('Catholic', 0)
                        i_staff = counts.get('Independent', 0)
                        t_staff = g_staff + c_staff + i_staff
                        if t_staff > 0:
                            course_rows.append([
                                course_name,
                                adv_metrics.get('date_table2', adv_metrics['formatted_date']),
                                g_staff,
                                c_staff,
                                i_staff,
                                t_staff
                            ])
                            
                    if course_rows:
                        # Append rows at table_2_end
                        summary_data_ws.insert_rows(course_rows, row=table_2_end)
                        
                        # Fix formatting
                        end_row = table_2_end + len(course_rows) - 1
                        summary_data_ws.format(f"A{table_2_end}:F{end_row}", {
                            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "textFormat": {"bold": False}
                        })
                        summary_data_ws.format(f"A{table_2_end}:A{end_row}", {
                            "horizontalAlignment": "LEFT"
                        })
                        summary_data_ws.format(f"B{table_2_end}:F{end_row}", {
                            "horizontalAlignment": "CENTER"
                        })
                        summary_data_ws.format(f"C{table_2_end}:F{end_row}", {
                            "numberFormat": {"type": "NUMBER", "pattern": "0"}
                        })
                        
                        self.log(f"✅ Successfully inserted {len(course_rows)} rows into Summary Data (Table 2).")
                else:
                    self.log("⚠️ Could not find 'Course' header for Table 2.")
                    
            except Exception as e:
                self.log(f"⚠️ Failed to update 'Summary Data' tab: {e}")

        # --- Standard Summary Dashboard ---
        try:
            summary_ws = master_sheet.worksheet("Summary")
        except gspread.exceptions.WorksheetNotFound:
            self.log("⚠️ 'Summary' tab not found. Creating it...")
            summary_ws = master_sheet.add_worksheet(title="Summary", rows="100", cols="10")
            summary_ws.append_row(["Metric", "Count"])

        self.log("Updating dynamic summary metrics...")
        
        # We will append or overwrite specific rows. For simplicity, we'll fetch existing,
        # or just rewrite the dashboard. Since we don't know the exact layout, we'll just 
        # append them at the bottom with a timestamp, or find the cells.
        # Assuming we just append to the summary for now:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        type_str = "Jext and Neffy" if workshop_type == 'jext_neffy' else "Accredited"
        
        summary_rows = [
            [f"--- Update Timestamp ({type_str}) ---", now],
            ["Total Distinct Schools Trained", unique_schools_count]
        ]
        
        for stype, count in school_type_counts.items():
            summary_rows.append([f"Total Staff ({stype})", count])
            
        summary_ws.append_rows(summary_rows)
        self.log("✅ Success! Summary metrics updated.")
